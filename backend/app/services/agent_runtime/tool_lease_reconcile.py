"""Durable recovery for Tool executions orphaned by a dead executor lease.

An in-flight Tool execution owns a short lease on its ledger receipt
(agent_tool_executions.status = "started"). When the process executing the
Tool dies -- e.g. a backend container restart mid-run -- the receipt stays
"started" until its lease expires, and the resumed Run parks itself in
"waiting_external" (reason "tool_execution_started") instead of re-running
the side effect. That park has no executor left to wake it, so the Run can
wait forever and the Web Chat stream starves out.

This scheduler is the missing wake-up:

* Safe reads (effect=read + retry_policy=safe) self-heal on a re-drive
  (retry or result-store probe), so it only enqueues the timer resume.
* User-reconcilable receipts (conditional write_file and image generation)
  are settled "unknown" -- never re-executed -- and surface through the
  existing human confirmation flow after the re-drive.
* Every other orphaned receipt is settled "failed" with an unconfirmed-outcome
  summary, so the model sees the interruption and decides whether to verify or
  retry as a new tool call. The original receipt is never replayed.

Declared async operations ("runtime_async_pending") and Group Workspace
mutations are excluded: the AsyncToolPollScheduler and the
RuntimeProductReconciler already own those receipts.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
import uuid

from sqlalchemy import false, func, or_, select

from app.models.agent_tool_execution import AgentToolExecution
from app.services.agent_runtime.command_worker import RuntimeSessionFactory
from app.services.agent_runtime.group_runtime_tools import (
    GROUP_WORKSPACE_EXECUTION_MUTATION_TOOL_NAMES,
)
from app.services.agent_runtime.persistence import enqueue_resume
from app.services.agent_runtime.tool_execution import (
    is_user_reconcilable_unknown_execution,
    mark_tool_execution_failed,
    mark_tool_execution_unknown,
    takeover_tool_execution_for_reconciliation,
)

ToolLeaseReconcileStatus = Literal["idle", "scheduled", "settled"]

_ORPHAN_UNKNOWN_SUMMARY = (
    "The tool executor's lease expired before this execution settled; "
    "its outcome is unknown and requires confirmation."
)

_ORPHAN_FAILED_SUMMARY = (
    "The tool executor's lease expired before this execution settled. "
    "Its outcome is unconfirmed: the operation may or may not have taken "
    "effect. Verify the current state before repeating this operation."
)


@dataclass(frozen=True, slots=True)
class ToolLeaseReconcileResult:
    """One bounded scheduler iteration."""

    status: ToolLeaseReconcileStatus
    execution_id: uuid.UUID | None = None
    run_id: uuid.UUID | None = None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _waiting_correlation(run_id: uuid.UUID, tool_call_id: str) -> str:
    """The exact correlation the Runtime parks waiting_external on.

    Mirrors tool_step_service._waiting_request.
    """
    return str(uuid.uuid5(run_id, "tool-reconcile:" + tool_call_id))


class ToolLeaseReconcileScheduler:
    """Settle orphaned started receipts and wake their parked Runs."""

    def __init__(
        self,
        *,
        session_factory: RuntimeSessionFactory,
        lease_owner: str | None = None,
        lease_ttl_seconds: int = 300,
        scan_batch_size: int = 32,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if lease_ttl_seconds <= 0:
            raise ValueError("lease_ttl_seconds must be positive")
        if scan_batch_size <= 0:
            raise ValueError("scan_batch_size must be positive")
        self._session_factory = session_factory
        self._lease_owner = lease_owner or "tool-lease-reconcile:" + str(uuid.uuid4())
        self._lease_ttl_seconds = lease_ttl_seconds
        self._scan_batch_size = scan_batch_size
        self._clock = clock or _utc_now

    async def run_once(self) -> ToolLeaseReconcileResult:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError(
                "tool lease reconcile clock must return a timezone-aware datetime"
            )
        now = now.astimezone(UTC)
        async with self._session_factory() as db:
            async with db.begin():
                result = await db.execute(
                    select(AgentToolExecution)
                    .where(
                        AgentToolExecution.status == "started",
                        or_(
                            AgentToolExecution.lease_expires_at.is_(None),
                            AgentToolExecution.lease_expires_at <= now,
                        ),
                        func.coalesce(
                            AgentToolExecution.result_metadata[
                                "runtime_async_pending"
                            ].as_boolean(),
                            false(),
                        ).is_(False),
                        AgentToolExecution.tool_name.notin_(
                            GROUP_WORKSPACE_EXECUTION_MUTATION_TOOL_NAMES
                        ),
                    )
                    .order_by(
                        AgentToolExecution.lease_expires_at.asc().nulls_first(),
                        AgentToolExecution.started_at,
                        AgentToolExecution.id,
                    )
                    .limit(self._scan_batch_size)
                    .with_for_update(skip_locked=True)
                )
                candidates = list(result.scalars().all())
                if not candidates:
                    return ToolLeaseReconcileResult(status="idle")
                execution = candidates[0]

                settled = False
                if not (
                    execution.effect == "read"
                    and execution.retry_policy == "safe"
                ):
                    takeover = await takeover_tool_execution_for_reconciliation(
                        db,
                        tenant_id=execution.tenant_id,
                        execution_id=execution.id,
                        lease_owner=self._lease_owner,
                        lease_ttl_seconds=self._lease_ttl_seconds,
                    )
                    if not takeover.acquired:
                        # A concurrent reconciler already took ownership.
                        return ToolLeaseReconcileResult(status="idle")
                    if is_user_reconcilable_unknown_execution(execution):
                        execution = await mark_tool_execution_unknown(
                            db,
                            tenant_id=execution.tenant_id,
                            execution_id=execution.id,
                            lease_owner=self._lease_owner,
                            result_summary=_ORPHAN_UNKNOWN_SUMMARY,
                            error_code="tool_execution_lease_expired",
                        )
                    else:
                        execution = await mark_tool_execution_failed(
                            db,
                            tenant_id=execution.tenant_id,
                            execution_id=execution.id,
                            lease_owner=self._lease_owner,
                            result_summary=_ORPHAN_FAILED_SUMMARY,
                            error_code="tool_execution_lease_expired",
                            retryable=False,
                        )
                    settled = True

                enqueued = await enqueue_resume(
                    db,
                    tenant_id=execution.tenant_id,
                    run_id=execution.run_id,
                    payload={
                        "resume_type": "timer",
                        "correlation_id": _waiting_correlation(
                            execution.run_id,
                            execution.tool_call_id,
                        ),
                        "payload": {
                            "reason": "tool_execution_lease_expired",
                            "tool_call_id": execution.tool_call_id,
                        },
                    },
                    idempotency_key="tool-lease-reconcile:" + str(execution.id),
                )
                if not getattr(enqueued, "created", True):
                    # The resume for this receipt already exists; there is
                    # nothing new to schedule, and a rejected resume command
                    # is never retried through the same idempotency key.
                    return ToolLeaseReconcileResult(
                        status="idle",
                        execution_id=execution.id,
                        run_id=execution.run_id,
                    )
        return ToolLeaseReconcileResult(
            status="settled" if settled else "scheduled",
            execution_id=execution.id,
            run_id=execution.run_id,
        )


__all__ = [
    "ToolLeaseReconcileResult",
    "ToolLeaseReconcileScheduler",
]
