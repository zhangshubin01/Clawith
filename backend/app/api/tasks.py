"""Task management API routes - 重构版本.

架构分层:
    API Layer (this file) → Service Layer (TaskService) → ORM Layer (models)

新增功能:
    - 分页支持
    - 多维度过滤（状态、类型、优先级）
    - 关键词搜索（标题/描述）
    - 多字段排序
    - 任务统计接口
    - 删除任务接口
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.schemas import (
    TaskCreate,
    TaskLogCreate,
    TaskLogOut,
    TaskOut,
    TaskPaginatedResponse,
    TaskStatisticsOut,
    TaskUpdate,
)
from app.services.task_service import TaskService

router = APIRouter(prefix="/agents/{agent_id}/tasks", tags=["tasks"])


# ─── 依赖注入 ────────────────────────────────────────────

async def get_task_service(db: AsyncSession = Depends(get_db)) -> TaskService:
    """获取 TaskService 实例."""
    return TaskService(db)


# ─── 查询接口 ────────────────────────────────────────────

@router.get("/", response_model=TaskPaginatedResponse)
async def list_tasks(
    agent_id: uuid.UUID,
    # 过滤参数
    status_filter: Annotated[str | None, Query(description="按状态过滤: pending/doing/done")] = None,
    type_filter: Annotated[str | None, Query(description="按类型过滤: todo/supervision")] = None,
    priority_filter: Annotated[str | None, Query(description="按优先级过滤: low/medium/high/urgent")] = None,
    search_keyword: Annotated[str | None, Query(description="搜索关键词（标题/描述）")] = None,
    # 排序参数
    sort_by: Annotated[str, Query(description="排序字段: created_at/updated_at/due_date/priority/title")] = "created_at",
    sort_order: Annotated[str, Query(description="排序方向: asc/desc")] = "desc",
    # 分页参数
    page: Annotated[int, Query(ge=1, description="页码")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="每页数量")] = 20,
    # 认证和服务
    include_stats: Annotated[bool, Query(description="是否包含统计信息")] = False,
    current_user: User = Depends(get_current_user),
    task_service: TaskService = Depends(get_task_service),
):
    """分页查询任务列表.

    支持:
    - 按状态、类型、优先级过滤
    - 按标题/描述关键词搜索
    - 多字段排序
    - 分页返回
    - 可选包含统计信息
    """
    await check_agent_access(task_service.db, current_user, agent_id)

    try:
        tasks, total = await task_service.list_tasks(
            agent_id=agent_id,
            status_filter=status_filter,
            type_filter=type_filter,
            priority_filter=priority_filter,
            search_keyword=search_keyword,
            sort_by=sort_by,
            sort_order=sort_order,
            page=page,
            page_size=page_size,
        )

        statistics = None
        if include_stats:
            statistics_data = await task_service.get_task_statistics(agent_id)
            statistics = TaskStatisticsOut(**statistics_data)

        return TaskPaginatedResponse(
            items=task_service.batch_to_task_out(tasks),
            total=total,
            page=page,
            page_size=page_size,
            statistics=statistics,
        )

    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get("/statistics", response_model=TaskStatisticsOut)
async def get_task_statistics(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    task_service: TaskService = Depends(get_task_service),
):
    """获取任务统计信息."""
    await check_agent_access(task_service.db, current_user, agent_id)

    statistics = await task_service.get_task_statistics(agent_id)
    return TaskStatisticsOut(**statistics)


@router.get("/{task_id}", response_model=TaskOut)
async def get_task(
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    task_service: TaskService = Depends(get_task_service),
):
    """获取单个任务详情."""
    await check_agent_access(task_service.db, current_user, agent_id)

    task = await task_service.get_task_by_id(task_id, agent_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    return task_service.to_task_out(task)


# ─── 写入接口 ────────────────────────────────────────────

@router.post("/", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
async def create_task(
    agent_id: uuid.UUID,
    data: TaskCreate,
    current_user: User = Depends(get_current_user),
    task_service: TaskService = Depends(get_task_service),
):
    """创建新任务.

    创建完成后自动触发后台执行（对于 todo 类型）.
    """
    await check_agent_access(task_service.db, current_user, agent_id)

    try:
        task = await task_service.create_task(
            agent_id=agent_id,
            data=data,
            created_by=current_user.id,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    task_out = task_service.to_task_out(task)

    # 提交事务，确保后台执行能看到数据
    await task_service.db.commit()

    # 触发后台执行
    if data.type == "todo":
        import asyncio
        from app.services.task_executor import execute_task
        asyncio.create_task(execute_task(task.id, agent_id))

    return task_out


@router.patch("/{task_id}", response_model=TaskOut)
async def update_task(
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    data: TaskUpdate,
    current_user: User = Depends(get_current_user),
    task_service: TaskService = Depends(get_task_service),
):
    """更新任务."""
    await check_agent_access(task_service.db, current_user, agent_id)

    task = await task_service.get_task_by_id(task_id, agent_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    try:
        task = await task_service.update_task(task, data)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    return task_service.to_task_out(task)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    task_service: TaskService = Depends(get_task_service),
):
    """删除任务."""
    await check_agent_access(task_service.db, current_user, agent_id)

    task = await task_service.get_task_by_id(task_id, agent_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    await task_service.delete_task(task)
    await task_service.db.commit()


# ─── 任务日志接口 ────────────────────────────────────────

@router.get("/{task_id}/logs", response_model=list[TaskLogOut])
async def get_task_logs(
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    task_service: TaskService = Depends(get_task_service),
):
    """获取任务的进度日志."""
    await check_agent_access(task_service.db, current_user, agent_id)

    # 验证任务存在
    task = await task_service.get_task_by_id(task_id, agent_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    logs = await task_service.get_task_logs(task_id)
    return [TaskLogOut.model_validate(log) for log in logs]


@router.post("/{task_id}/logs", response_model=TaskLogOut, status_code=status.HTTP_201_CREATED)
async def add_task_log(
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    data: TaskLogCreate,
    current_user: User = Depends(get_current_user),
    task_service: TaskService = Depends(get_task_service),
):
    """添加任务进度日志."""
    await check_agent_access(task_service.db, current_user, agent_id)

    # 验证任务存在
    task = await task_service.get_task_by_id(task_id, agent_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    log = await task_service.add_task_log(task_id, data)
    await task_service.db.commit()
    return TaskLogOut.model_validate(log)


# ─── 触发执行接口 ────────────────────────────────────────

@router.post("/{task_id}/trigger")
async def trigger_task(
    agent_id: uuid.UUID,
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    task_service: TaskService = Depends(get_task_service),
):
    """手动触发任务执行（主要用于调试）."""
    from app.core.permissions import is_agent_expired

    agent, _ = await check_agent_access(task_service.db, current_user, agent_id)
    if is_agent_expired(agent):
        raise HTTPException(status_code=403, detail="Agent has expired")

    task = await task_service.get_task_by_id(task_id, agent_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    import asyncio
    from app.services.task_executor import execute_task
    asyncio.create_task(execute_task(task.id, agent_id))

    return {"status": "triggered", "task_id": str(task_id)}
