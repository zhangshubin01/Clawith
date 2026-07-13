"""Transaction-scoped heartbeat intake for the durable Agent Runtime."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models.agent import Agent
from app.services.agent_runtime.adapter import TransactionalAgentRuntimeAdapter
from app.services.agent_runtime.config import decide_runtime_v2
from app.services.agent_runtime.contracts import RunHandle, StartRunCommand


class HeartbeatRuntimeIntakeError(RuntimeError):
    """A heartbeat selected for Runtime v2 cannot be registered safely."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def heartbeat_source_execution_id(
    agent_id: uuid.UUID,
    occurrence_at: datetime,
) -> str:
    """Build a stable identity from the atomically claimed heartbeat slot."""
    if occurrence_at.tzinfo is None or occurrence_at.utcoffset() is None:
        raise HeartbeatRuntimeIntakeError(
            "invalid_heartbeat_occurrence",
            "Heartbeat occurrence timestamp must be timezone-aware",
        )
    timestamp = (
        occurrence_at.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    return f"heartbeat:{agent_id}:{timestamp}"


async def enqueue_heartbeat_runtime(
    db: AsyncSession,
    *,
    agent: Agent,
    occurrence_at: datetime,
    instruction: str,
    settings_override: Settings | None = None,
) -> RunHandle | None:
    """Register one claimed heartbeat in the caller transaction when v2 is selected."""
    runtime_settings = settings_override or get_settings()
    decision = decide_runtime_v2(
        agent_id=agent.id,
        source_type="heartbeat",
        settings=runtime_settings,
    )
    if not decision.use_v2:
        return None
    if agent.tenant_id is None:
        raise HeartbeatRuntimeIntakeError(
            "agent_tenant_missing",
            "Runtime Heartbeat Agent has no tenant",
        )
    if agent.primary_model_id is None:
        raise HeartbeatRuntimeIntakeError(
            "agent_model_missing",
            "Runtime Heartbeat Agent has no primary model",
        )
    if agent.is_expired or agent.status not in {"running", "idle"}:
        raise HeartbeatRuntimeIntakeError(
            "agent_unavailable",
            "Runtime Heartbeat Agent is unavailable",
        )
    normalized_instruction = instruction.strip()
    if not normalized_instruction:
        raise HeartbeatRuntimeIntakeError(
            "heartbeat_instruction_missing",
            "Runtime Heartbeat instruction is empty",
        )

    source_execution_id = heartbeat_source_execution_id(agent.id, occurrence_at)
    return await TransactionalAgentRuntimeAdapter(
        db,
        settings=runtime_settings,
    ).start_run(
        StartRunCommand(
            tenant_id=agent.tenant_id,
            agent_id=agent.id,
            source_type="heartbeat",
            source_id=str(agent.id),
            source_execution_id=source_execution_id,
            goal=normalized_instruction,
            run_kind="background",
            model_id=agent.primary_model_id,
            delivery_status="not_required",
            idempotency_key=f"start:{source_execution_id}",
            payload={
                "heartbeat_occurrence_at": occurrence_at.astimezone(UTC).isoformat(),
                "heartbeat_instruction": normalized_instruction,
            },
            origin_user_id=agent.creator_id,
        )
    )


__all__ = [
    "HeartbeatRuntimeIntakeError",
    "enqueue_heartbeat_runtime",
    "heartbeat_source_execution_id",
]
