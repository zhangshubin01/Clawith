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

import asyncio
import uuid
from fastapi import APIRouter, Depends, HTTPException, status

# 模块级后台任务集合，保存 create_task 返回值防止 GC 回收
_bg_tasks: set[asyncio.Task] = set()
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.schemas import (
    ListTasksQuery,
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
    query: ListTasksQuery = Depends(),
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
            status_filter=query.status_filter,
            type_filter=query.type_filter,
            priority_filter=query.priority_filter,
            search_keyword=query.search_keyword,
            sort_by=query.sort_by,
            sort_order=query.sort_order,
            page=query.page,
            page_size=query.page_size,
        )

        statistics = None
        if query.include_stats:
            statistics_data = await task_service.get_task_statistics(agent_id)
            statistics = TaskStatisticsOut(**statistics_data)

        return TaskPaginatedResponse(
            items=task_service.batch_to_task_out(tasks),
            total=total,
            page=query.page,
            page_size=query.page_size,
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
        _t = asyncio.create_task(execute_task(task.id, agent_id))
        _bg_tasks.add(_t)
        _t.add_done_callback(_bg_tasks.discard)

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

    from app.services.task_executor import execute_task
    _t = asyncio.create_task(execute_task(task.id, agent_id))
    _bg_tasks.add(_t)
    _t.add_done_callback(_bg_tasks.discard)

    return {"status": "triggered", "task_id": str(task_id)}
