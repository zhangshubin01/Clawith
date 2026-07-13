"""Background task executor — runs LLM to complete tasks automatically.

Uses the same agent context (soul, memory, skills, relationships, tools)
as the chat dialog. Supports tool-calling loop for autonomous execution.
"""

import uuid
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.database import async_session
from app.models.agent import Agent
from app.models.task import Task, TaskLog
from app.services.agent_runtime.adapter import TransactionalAgentRuntimeAdapter
from app.services.agent_runtime.config import decide_runtime_v2
from app.services.agent_runtime.contracts import RunHandle, StartRunCommand

settings = get_settings()


class TaskRuntimeIntakeError(RuntimeError):
    """A Task selected for Runtime v2 cannot be registered safely."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _task_goal(task: Task) -> str:
    goal = f"[任务执行] {task.title}"
    if task.description:
        goal += f"\n任务描述: {task.description}"
    return goal + "\n\n请认真完成此任务，给出详细的执行结果。"


async def enqueue_task_runtime(
    db: AsyncSession,
    *,
    task: Task,
    agent: Agent,
    settings_override: Settings | None = None,
) -> RunHandle | None:
    """Register one todo execution in the caller transaction when v2 is selected."""
    runtime_settings = settings_override or settings
    if task.type != "todo":
        return None
    decision = decide_runtime_v2(
        agent_id=agent.id,
        source_type="task",
        settings=runtime_settings,
    )
    if not decision.use_v2:
        return None
    if task.agent_id != agent.id:
        raise TaskRuntimeIntakeError(
            "task_agent_mismatch",
            "Task does not belong to the requested Agent",
        )
    if agent.tenant_id is None:
        raise TaskRuntimeIntakeError(
            "agent_tenant_missing",
            "Runtime Task Agent has no tenant",
        )
    model_id = agent.primary_model_id or agent.fallback_model_id
    if model_id is None:
        raise TaskRuntimeIntakeError(
            "agent_model_missing",
            "Runtime Task Agent has no configured model",
        )

    handle = await TransactionalAgentRuntimeAdapter(
        db,
        settings=runtime_settings,
    ).start_run(
        StartRunCommand(
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            source_type="task",
            source_id=str(task.id),
            source_execution_id=f"task:{task.id}",
            goal=_task_goal(task),
            run_kind="background",
            model_id=model_id,
            delivery_status="not_required",
            idempotency_key=f"start:task:{task.id}",
            payload={
                "task_id": str(task.id),
                "task_type": task.type,
                "title": task.title,
                "description": task.description,
            },
            origin_user_id=task.created_by,
            actor_user_id=task.created_by,
        )
    )
    task.status = "doing"
    if handle.created:
        db.add(
            TaskLog(
                task_id=task.id,
                content=f"🤖 已进入持久化执行队列（Run {handle.run_id}）",
            )
        )
    return handle


async def _try_enqueue_runtime_task(
    task_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> RunHandle | None:
    async with async_session() as db:
        async with db.begin():
            task_result = await db.execute(
                select(Task).where(
                    Task.id == task_id,
                    Task.agent_id == agent_id,
                )
            )
            task = task_result.scalar_one_or_none()
            if task is None:
                raise TaskRuntimeIntakeError(
                    "task_not_found",
                    "Task does not exist for the requested Agent",
                )
            agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = agent_result.scalar_one_or_none()
            if agent is None:
                raise TaskRuntimeIntakeError(
                    "agent_not_found",
                    "Task Agent does not exist",
                )
            return await enqueue_task_runtime(
                db,
                task=task,
                agent=agent,
            )


async def execute_task(task_id: uuid.UUID, agent_id: uuid.UUID) -> None:
    """Execute a task using the agent's configured LLM with full context.

    Uses the same context as chat dialog: build_agent_context for system prompt,
    agent tools for tool-calling, and a multi-round tool loop.

    Flow:
      - todo tasks: pending → doing → done
      - supervision tasks: pending → doing → pending (stays active, just logs result)
    """
    logger.info(f"[TaskExec] Starting task {task_id} for agent {agent_id}")

    try:
        runtime_handle = await _try_enqueue_runtime_task(task_id, agent_id)
    except TaskRuntimeIntakeError as exc:
        if exc.code == "task_not_found":
            logger.warning(f"[TaskExec] Task {task_id} not found")
            return
        logger.error(f"[TaskExec] Runtime intake failed ({exc.code}): {exc}")
        await _log_error(task_id, f"持久化执行登记失败: {exc.code}")
        return
    except Exception as exc:
        error_code = getattr(exc, "code", type(exc).__name__)
        logger.error(f"[TaskExec] Runtime intake failed ({error_code}): {exc}")
        await _log_error(task_id, f"持久化执行登记失败: {error_code}")
        return
    if runtime_handle is not None:
        logger.info(
            f"[TaskExec] Task {task_id} queued as Runtime Run {runtime_handle.run_id}"
        )
        return

    # Step 1: Mark as doing
    async with async_session() as db:
        result = await db.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if not task:
            logger.warning(f"[TaskExec] Task {task_id} not found")
            return

        task.status = "doing"
        db.add(TaskLog(task_id=task_id, content="🤖 开始执行任务..."))
        await db.commit()
        task_title = task.title
        task_description = task.description or ""
        task_type = task.type  # 'todo' or 'supervision'
        supervision_target = task.supervision_target_name or ""

    # Step 2: Load agent
    async with async_session() as db:
        agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
        agent = agent_result.scalar_one_or_none()
        if not agent:
            await _log_error(task_id, "数字员工未找到")
            if task_type == 'supervision':
                await _restore_supervision_status(task_id)
            return
        agent_name = agent.name

    # Step 3: Build full agent context (same as chat dialog)
    from app.services.agent_context import build_agent_context
    static_prompt, dynamic_prompt = await build_agent_context(agent_id, agent_name, agent.role_description or "")

    # Add task-execution-specific instructions
    task_addendum = """

## Task Execution Mode

You are now in TASK EXECUTION MODE (not a conversation). A task has been assigned to you.
- Focus on completing the task as thoroughly as possible.
- Break down complex tasks into steps and execute each step.
- Use your tools actively to gather information, send messages, read/write files, etc.
- Provide a detailed execution report at the end.
- If the task involves contacting someone, use `send_feishu_message` to reach them.
- If the task requires data or information, use your tools to fetch it.
- Do NOT ask the user follow-up questions — take initiative and complete the task autonomously.
"""
    dynamic_prompt += task_addendum
    system_prompt = f"{static_prompt}\n\n{dynamic_prompt}"

    # Build user prompt
    if task_type == 'supervision':
        user_prompt = f"[督办任务] {task_title}"
        if task_description:
            user_prompt += f"\n任务描述: {task_description}"
        if supervision_target:
            user_prompt += f"\n督办对象: {supervision_target}"
        user_prompt += "\n\n请执行此督办任务：联系督办对象，了解进展，并汇报结果。"
    else:
        user_prompt = f"[任务执行] {task_title}"
        if task_description:
            user_prompt += f"\n任务描述: {task_description}"
        user_prompt += "\n\n请认真完成此任务，给出详细的执行结果。"

    # Step 4: Call LLM with unified failover support
    from app.services.llm import call_agent_llm_with_tools
    
    try:
        logger.info(f"[TaskExec] Calling LLM with tools for task: {task_title}")
        
        async with async_session() as db:
            reply = await call_agent_llm_with_tools(
                db=db,
                agent_id=agent_id,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_rounds=50,
                session_id=str(task_id),
            )
            
        logger.info(f"[TaskExec] LLM reply: {reply[:80]}")
    except Exception as e:
        error_msg = str(e) or repr(e)
        logger.error(f"[TaskExec] Error: {error_msg}")
        await _log_error(task_id, f"执行出错: {error_msg[:150]}")
        if task_type == 'supervision':
            await _restore_supervision_status(task_id)
        return

    # Step 5: Save result and update status
    async with async_session() as db:
        result = await db.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if task:
            if task_type == 'supervision':
                # Supervision tasks stay active; just log the result
                task.status = "pending"
                db.add(TaskLog(task_id=task_id, content=f"✅ 督办执行完成\n\n{reply}"))
            else:
                task.status = "done"
                task.completed_at = datetime.now(timezone.utc)
                db.add(TaskLog(task_id=task_id, content=f"✅ 任务完成\n\n{reply}"))
            await db.commit()
            logger.info(f"[TaskExec] Task {task_id} {'logged' if task_type == 'supervision' else 'completed'}!")

    # Log activity
    from app.services.activity_logger import log_activity
    await log_activity(
        agent_id, "task_updated",
        f"{'督办' if task_type == 'supervision' else '任务'}执行: {task_title[:60]}",
        detail={"task_id": str(task_id), "task_type": task_type, "title": task_title, "reply": reply[:500]},
        related_id=task_id,
    )


async def _log_error(task_id: uuid.UUID, message: str) -> None:
    """Add an error log to the task."""
    logger.error(f"[TaskExec] Error for {task_id}: {message}")
    async with async_session() as db:
        db.add(TaskLog(task_id=task_id, content=f"❌ {message}"))
        await db.commit()


async def _restore_supervision_status(task_id: uuid.UUID) -> None:
    """Restore supervision task status back to pending after a failed execution."""
    async with async_session() as db:
        result = await db.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()
        if task and task.status == "doing":
            task.status = "pending"
            await db.commit()
