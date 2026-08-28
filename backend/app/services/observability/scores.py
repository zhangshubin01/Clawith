"""First-party native scores written at a Run's terminal settle (ticket 03).

Native Score 是应用在 run 终态自己记录的业务事实，与 evaluator/judge 事后
推断分轨（同源会失去交叉校验价值，见 CONTEXT.md §Observability）：

- ``run_outcome``    categorical：succeeded / failed / cancelled（与结算状态一致）
- ``attempt_count``  numeric：终态命令结算时点的已消耗业务恢复尝试数
- ``implicit_negative`` categorical（仅检出时写）：
  explicit_cancel（显式取消/停止命令）、same_goal_retry（同 session 同 goal
  重复发起，此前已有终态 run）、negation_retry（用户文本含否定/纠正 marker
  且同 session 此前已有终态 run）。中性继续对话不命中（窄口径）。

``cost_usd`` 有意不写：结算链无 run 级 token/成本台账（DailyTokenUsage 是
agent/日粒度，checkpoint lifecycle 无 usage 累计，亦无计价表）。成本快照由
Langfuse 从 generation usage_details 原生计算；如需第一方 cost_usd，另立小票
在 lifecycle 累加 run 级 TokenUsage。

trace 归属：LangGraph driver 在 graph 执行时把 SDK 当前 trace id 注入
checkpoint metadata（``clawith_trace_id``）——结算链运行在 observe_run 上下文
之外，唯一可靠的第一方事实就是 checkpoint 里那条 metadata。cancel 结算不执行
graph，读到的即被取消 run 最后一次 graph 执行的 trace。cancel-before-start
（无 checkpoint）无 trace，no-op。

所有写入 best-effort：observability disabled、无 trace 上下文或 SDK 内部
失败时 no-op 且绝不抛出（不打断结算链事务）。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_run import AgentRun
from app.models.agent_run_command import AgentRunCommand
from app.models.agent_run_event import AgentRunEvent
from app.services.agent_runtime.command_worker import (
    CheckpointObservation,
    RuntimeCommandRecord,
    RuntimeRunRecord,
)
from app.services.observability.tracing import _get_client

__all__ = ["record_terminal_scores"]

_SCORE_RUN_OUTCOME = "run_outcome"
_SCORE_ATTEMPT_COUNT = "attempt_count"
_SCORE_IMPLICIT_NEGATIVE = "implicit_negative"

_RUN_OUTCOME_BY_STATUS = {
    "completed": "succeeded",
    "failed": "failed",
    "cancelled": "cancelled",
}

_TERMINAL_EVENT_TYPES = ("run_completed", "run_failed", "run_cancelled")

# 窄口径否定/纠正 marker（中性继续对话不命中）。中文避开「是不是」类疑问词，
# 全部为否定断言或明确的重新/纠正指令。
_NEGATION_RETRY_MARKERS: tuple[str, ...] = (
    # 否定断言
    "不对",
    "不行",
    "错了",
    "错误",
    "不正确",
    "不是这样",
    "不是这个",
    # 纠正/重做指令
    "重新",
    "重做",
    "重来",
    "再来",
    "重试",
    "纠正",
    "修正",
    "改一下",
    "改回",
    "换一种",
    "从头",
    # 英文
    "no,",
    "nope",
    "wrong",
    "incorrect",
    "redo",
    "retry",
    "start over",
    "try again",
    "do it again",
    "instead of",
    "not that",
    "fix it",
)


def _text_field(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _checkpoint_trace_id(checkpoint: CheckpointObservation | None) -> str | None:
    if checkpoint is None:
        return None
    return _text_field(checkpoint.metadata.get("clawith_trace_id"))


def _score_id(run_id: uuid.UUID, name: str) -> str:
    """Deterministic score id — Langfuse upserts by id, so replayed settles
    (reconciler / claim retry) converge on one score instead of duplicating."""
    return f"{run_id}:{name}"


async def _settled_attempt_count(
    db: AsyncSession,
    run: RuntimeRunRecord,
    command: RuntimeCommandRecord,
) -> int:
    """The terminal command's consumed recovery attempts at settle time.

    The in-memory command record snapshots the count at claim (before
    ``begin_command_attempt`` increments), so prefer the settled DB value.
    """
    try:
        result = await db.execute(
            select(AgentRunCommand.attempt_count).where(
                AgentRunCommand.tenant_id == run.tenant_id,
                AgentRunCommand.run_id == run.run_id,
                AgentRunCommand.id == command.id,
            )
        )
        value = result.scalar_one_or_none()
        if isinstance(value, int) and value >= 0:
            return value
    except Exception:  # noqa: BLE001 — best-effort first-party scoring
        pass
    return max(int(command.attempt_count), 0)


def _retry_user_text(run: RuntimeRunRecord, command: RuntimeCommandRecord) -> str | None:
    """The user text that (re)triggered this command: start goal or resume content."""
    if command.command_type == "start":
        return _text_field(run.goal)
    if command.command_type == "resume":
        payload = command.payload.get("payload")
        if isinstance(payload, Mapping):
            return _text_field(payload.get("content"))
    return None


def _has_negation_marker(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _NEGATION_RETRY_MARKERS)


async def _prior_same_goal_run_id(
    db: AsyncSession,
    run: RuntimeRunRecord,
    session_id: uuid.UUID,
) -> Any:
    """An earlier terminal Run in the same session with the identical goal.

    Session lanes serialize runs, so any terminal same-goal Run that is not the
    current one must have settled before it — no created_at comparison needed.
    """
    result = await db.execute(
        select(AgentRun.id)
        .where(
            AgentRun.tenant_id == run.tenant_id,
            AgentRun.session_id == session_id,
            AgentRun.goal == run.goal,
            AgentRun.id != run.run_id,
            select(AgentRunEvent.id)
            .where(
                AgentRunEvent.tenant_id == run.tenant_id,
                AgentRunEvent.run_id == AgentRun.id,
                AgentRunEvent.event_type.in_(_TERMINAL_EVENT_TYPES),
            )
            .exists(),
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _prior_terminal_run_id(
    db: AsyncSession,
    run: RuntimeRunRecord,
    session_id: uuid.UUID,
) -> Any:
    """An earlier terminal Run in the same session (any goal)."""
    result = await db.execute(
        select(AgentRun.id)
        .where(
            AgentRun.tenant_id == run.tenant_id,
            AgentRun.session_id == session_id,
            AgentRun.id != run.run_id,
            select(AgentRunEvent.id)
            .where(
                AgentRunEvent.tenant_id == run.tenant_id,
                AgentRunEvent.run_id == AgentRun.id,
                AgentRunEvent.event_type.in_(_TERMINAL_EVENT_TYPES),
            )
            .exists(),
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _implicit_negative(
    db: AsyncSession,
    run: RuntimeRunRecord,
    command: RuntimeCommandRecord,
) -> str | None:
    """Narrow-scope implicit negative signal, priority-ordered.

    explicit_cancel > same_goal_retry > negation_retry; one categorical value
    per settle. Non-user-initiated Runs (trigger/heartbeat 等 actor 为空) and
    session-less Runs are excluded — periodic re-issues are by design, not
    dissatisfaction.
    """
    if command.command_type == "cancel":
        return "explicit_cancel"
    if run.session_id is None or command.actor_user_id is None:
        return None
    try:
        session_id = uuid.UUID(run.session_id)
    except (TypeError, ValueError):
        return None

    if await _prior_same_goal_run_id(db, run, session_id) is not None:
        return "same_goal_retry"

    text = _retry_user_text(run, command)
    if text is not None and _has_negation_marker(text):
        if await _prior_terminal_run_id(db, run, session_id) is not None:
            return "negation_retry"
    return None


async def record_terminal_scores(
    db: AsyncSession,
    *,
    run: RuntimeRunRecord,
    command: RuntimeCommandRecord,
    checkpoint: CheckpointObservation | None,
    status: str,
) -> None:
    """Write first-party scores for a settled terminal Run (best-effort no-op-safe).

    Mounted at ``_record_lifecycle_events`` — the settlement chain's single
    terminal-event exit. ``status`` is the settled lifecycle status
    (completed/failed/cancelled), i.e. the same source the terminal event uses.
    """
    try:
        trace_id = _checkpoint_trace_id(checkpoint)
        if trace_id is None:
            return  # 无 trace 上下文（disabled / cancel-before-start）— no-op
        client = _get_client(str(run.tenant_id) if run.tenant_id is not None else None)
        if client is None:
            return

        outcome = _RUN_OUTCOME_BY_STATUS.get(status)
        if outcome is None:
            return

        client.create_score(
            name=_SCORE_RUN_OUTCOME,
            value=outcome,
            data_type="CATEGORICAL",
            trace_id=trace_id,
            score_id=_score_id(run.run_id, _SCORE_RUN_OUTCOME),
        )
        attempt_count = await _settled_attempt_count(db, run, command)
        client.create_score(
            name=_SCORE_ATTEMPT_COUNT,
            value=float(attempt_count),
            data_type="NUMERIC",
            trace_id=trace_id,
            score_id=_score_id(run.run_id, _SCORE_ATTEMPT_COUNT),
        )
        negative = await _implicit_negative(db, run, command)
        if negative is not None:
            client.create_score(
                name=_SCORE_IMPLICIT_NEGATIVE,
                value=negative,
                data_type="CATEGORICAL",
                trace_id=trace_id,
                score_id=_score_id(run.run_id, _SCORE_IMPLICIT_NEGATIVE),
            )
    except Exception as exc:  # noqa: BLE001 — tracing must never break settlement
        logger.warning(
            "[Observability] native score write failed for run {}: {}",
            run.run_id,
            exc,
        )
