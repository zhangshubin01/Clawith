"""Task service - 任务业务逻辑层.

本模块封装任务相关的核心业务逻辑，与数据库操作解耦，
提供统一的 CRUD、分页、搜索、排序等功能.

架构分层:
    API Layer (routes) → Service Layer (this file) → ORM Layer (models)
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models.task import Task, TaskLog
from app.schemas.schemas import TaskCreate, TaskLogCreate, TaskOut, TaskUpdate


class TaskService:
    """任务服务 - 封装所有任务相关业务逻辑."""

    # 枚举值定义 - 确保与数据库一致
    TASK_TYPES = {"todo", "supervision"}
    TASK_STATUSES = {"pending", "doing", "done"}
    TASK_PRIORITIES = {"low", "medium", "high", "urgent"}
    SORT_FIELDS = {
        "created_at": Task.created_at,
        "updated_at": Task.updated_at,
        "due_date": Task.due_date,
        "priority": Task.priority,
        "title": Task.title,
    }

    def __init__(self, db: AsyncSession):
        self.db = db

    # ─── 查询方法 ──────────────────────────────────────────

    async def get_task_by_id(self, task_id: uuid.UUID, agent_id: uuid.UUID) -> Task | None:
        """根据 ID 获取单个任务."""
        result = await self.db.execute(
            select(Task)
            .options(joinedload(Task.creator))
            .where(Task.id == task_id, Task.agent_id == agent_id)
        )
        return result.scalar_one_or_none()

    async def list_tasks(
        self,
        agent_id: uuid.UUID,
        *,
        status_filter: str | None = None,
        type_filter: str | None = None,
        search_keyword: str | None = None,
        priority_filter: str | None = None,
        sort_by: str = "created_at",
        sort_order: str = "desc",
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Task], int]:
        """分页查询任务列表.

        Args:
            agent_id: 所属 Agent ID
            status_filter: 按状态过滤
            type_filter: 按类型过滤
            search_keyword: 搜索关键词（标题/描述）
            priority_filter: 按优先级过滤
            sort_by: 排序字段
            sort_order: 排序方向 asc/desc
            page: 页码
            page_size: 每页数量

        Returns:
            (任务列表, 总数)
        """
        # 基础查询
        query = select(Task).options(joinedload(Task.creator)).where(Task.agent_id == agent_id)

        # 应用过滤条件
        query = self._apply_filters(
            query,
            status_filter=status_filter,
            type_filter=type_filter,
            priority_filter=priority_filter,
            search_keyword=search_keyword,
        )

        # 计算总数（在排序和分页前）
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await self.db.execute(count_query)
        total = total_result.scalar_one()

        # 应用排序
        query = self._apply_sorting(query, sort_by, sort_order)

        # 应用分页
        offset = (page - 1) * page_size
        query = query.offset(offset).limit(page_size)

        # 执行查询
        result = await self.db.execute(query)
        tasks = result.scalars().unique().all()

        return list(tasks), total

    # ─── 写入方法 ──────────────────────────────────────────

    async def create_task(
        self,
        agent_id: uuid.UUID,
        data: TaskCreate,
        created_by: uuid.UUID,
    ) -> Task:
        """创建新任务."""
        # 参数校验
        self._validate_enum("type", data.type, self.TASK_TYPES)
        self._validate_enum("priority", data.priority, self.TASK_PRIORITIES)

        task = Task(
            agent_id=agent_id,
            title=data.title,
            description=data.description,
            type=data.type,
            priority=data.priority,
            due_date=data.due_date,
            created_by=created_by,
            supervision_target_name=data.supervision_target_name,
            supervision_channel=data.supervision_channel,
            remind_schedule=data.remind_schedule,
        )
        self.db.add(task)
        await self.db.flush()
        return task

    async def update_task(
        self,
        task: Task,
        data: TaskUpdate,
    ) -> Task:
        """更新任务."""
        # 参数校验
        if data.status is not None:
            self._validate_enum("status", data.status, self.TASK_STATUSES)
        if data.priority is not None:
            self._validate_enum("priority", data.priority, self.TASK_PRIORITIES)

        # 更新字段
        update_data = data.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(task, field, value)

        # 如果标记为完成，设置完成时间
        if data.status == "done" and task.completed_at is None:
            task.completed_at = func.now()

        await self.db.flush()
        return task

    async def delete_task(self, task: Task) -> None:
        """删除任务."""
        await self.db.delete(task)
        await self.db.flush()

    # ─── 任务日志方法 ───────────────────────────────────────

    async def get_task_logs(self, task_id: uuid.UUID) -> list[TaskLog]:
        """获取任务的所有日志."""
        result = await self.db.execute(
            select(TaskLog)
            .where(TaskLog.task_id == task_id)
            .order_by(TaskLog.created_at.asc())
        )
        return list(result.scalars().all())

    async def add_task_log(
        self,
        task_id: uuid.UUID,
        data: TaskLogCreate,
    ) -> TaskLog:
        """添加任务日志."""
        log = TaskLog(task_id=task_id, content=data.content)
        self.db.add(log)
        await self.db.flush()
        return log

    # ─── 统计方法 ──────────────────────────────────────────

    async def get_task_statistics(self, agent_id: uuid.UUID) -> dict[str, Any]:
        """获取任务统计信息."""
        # 按状态统计
        status_query = select(Task.status, func.count(Task.id)).where(
            Task.agent_id == agent_id
        ).group_by(Task.status)
        status_result = await self.db.execute(status_query)
        status_counts = dict(status_result.all())

        # 按优先级统计
        priority_query = select(Task.priority, func.count(Task.id)).where(
            Task.agent_id == agent_id
        ).group_by(Task.priority)
        priority_result = await self.db.execute(priority_query)
        priority_counts = dict(priority_result.all())

        # 即将到期的任务（未来7天内）
        from datetime import timedelta
        upcoming_deadline = datetime.utcnow() + timedelta(days=7)
        upcoming_query = select(func.count(Task.id)).where(
            Task.agent_id == agent_id,
            Task.status != "done",
            Task.due_date.is_not(None),
            Task.due_date <= upcoming_deadline,
        )
        upcoming_result = await self.db.execute(upcoming_query)
        upcoming_count = upcoming_result.scalar_one()

        return {
            "by_status": {
                "pending": status_counts.get("pending", 0),
                "doing": status_counts.get("doing", 0),
                "done": status_counts.get("done", 0),
            },
            "by_priority": {
                "low": priority_counts.get("low", 0),
                "medium": priority_counts.get("medium", 0),
                "high": priority_counts.get("high", 0),
                "urgent": priority_counts.get("urgent", 0),
            },
            "upcoming_deadline_count": upcoming_count,
            "total": sum(status_counts.values()),
        }

    # ─── 转换方法 ──────────────────────────────────────────

    def to_task_out(self, task: Task) -> TaskOut:
        """将 Task ORM 模型转换为 TaskOut Schema."""
        task_out = TaskOut.model_validate(task)
        if task.creator:
            task_out.creator_username = task.creator.username
        return task_out

    def batch_to_task_out(self, tasks: list[Task]) -> list[TaskOut]:
        """批量转换 Task 列表为 TaskOut 列表."""
        return [self.to_task_out(task) for task in tasks]

    # ─── 私有辅助方法 ───────────────────────────────────────

    def _apply_filters(
        self,
        query: Select,
        *,
        status_filter: str | None,
        type_filter: str | None,
        priority_filter: str | None,
        search_keyword: str | None,
    ) -> Select:
        """应用查询过滤条件."""
        conditions = []

        if status_filter:
            self._validate_enum("status", status_filter, self.TASK_STATUSES)
            conditions.append(Task.status == status_filter)

        if type_filter:
            self._validate_enum("type", type_filter, self.TASK_TYPES)
            conditions.append(Task.type == type_filter)

        if priority_filter:
            self._validate_enum("priority", priority_filter, self.TASK_PRIORITIES)
            conditions.append(Task.priority == priority_filter)

        if search_keyword:
            search_pattern = f"%{search_keyword}%"
            conditions.append(
                or_(
                    Task.title.ilike(search_pattern),
                    Task.description.ilike(search_pattern),
                )
            )

        if conditions:
            query = query.where(and_(*conditions))

        return query

    def _apply_sorting(self, query: Select, sort_by: str, sort_order: str) -> Select:
        """应用排序条件."""
        if sort_by not in self.SORT_FIELDS:
            raise ValueError(
                f"Invalid sort_by: {sort_by}. "
                f"Must be one of: {', '.join(self.SORT_FIELDS.keys())}"
            )

        sort_column = self.SORT_FIELDS[sort_by]

        if sort_order.lower() == "asc":
            query = query.order_by(sort_column.asc())
        elif sort_order.lower() == "desc":
            query = query.order_by(sort_column.desc())
        else:
            raise ValueError("sort_order must be 'asc' or 'desc'")

        return query

    def _validate_enum(self, field_name: str, value: str, allowed_values: set[str]) -> None:
        """校验枚举值."""
        if value not in allowed_values:
            raise ValueError(
                f"Invalid {field_name}: {value}. "
                f"Must be one of: {', '.join(sorted(allowed_values))}"
            )
