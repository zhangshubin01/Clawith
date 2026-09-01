"""Polling stream over stable product events, never checkpoint internals."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
import asyncio
from copy import deepcopy
from datetime import UTC, datetime
import math
import time as time_module
from typing import Literal, cast
import uuid

from loguru import logger
from sqlalchemy import and_, exists, or_, select, tuple_
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_run import AgentRun
from app.models.agent_run_command import AgentRunCommand
from app.models.agent_run_event import AgentRunEvent
from app.models.agent_tool_execution import AgentToolExecution
from app.services.agent_runtime.command_worker import RuntimeSessionFactory
from app.services.agent_runtime.contracts import (
    RunHandle,
    RuntimeEvent,
    RuntimeEventCursor,
    RuntimeEventType,
)
from app.services.agent_runtime.state import JsonObject, JsonValue


_TERMINAL_EVENT_TYPES = frozenset({"run_completed", "run_failed", "run_cancelled"})
_DELIVERY_EVENT_TYPES = frozenset({"delivery_succeeded", "delivery_failed"})
_SETTLED_DELIVERY_STATUSES = frozenset({"not_required", "delivered", "failed"})
_LANE_PROBE_RETRY_DELAYS = (0.5, 0.5, 0.5)
_EVENT_TYPES = frozenset(
    {
        "run_created",
        "status_changed",
        "waiting_started",
        "resumed",
        "evidence_added",
        "verification_updated",
        *_TERMINAL_EVENT_TYPES,
        *_DELIVERY_EVENT_TYPES,
    }
)


class RuntimeEventStreamError(RuntimeError):
    """A stable Run event stream cannot be opened or decoded safely."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _event_statement(
    handle: RunHandle,
    *,
    after: RuntimeEventCursor | None,
    batch_size: int,
):
    statement = select(AgentRunEvent).where(
        AgentRunEvent.tenant_id == handle.tenant_id,
        AgentRunEvent.run_id == handle.run_id,
    )
    if after is not None:
        statement = statement.where(
            or_(
                AgentRunEvent.created_at > after.created_at,
                and_(
                    AgentRunEvent.created_at == after.created_at,
                    AgentRunEvent.id > after.event_id,
                ),
            )
        )
    return statement.order_by(AgentRunEvent.created_at.asc(), AgentRunEvent.id.asc()).limit(
        batch_size
    )


def _json_value(value: object, *, field: str) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return deepcopy(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RuntimeEventStreamError(
                "invalid_runtime_event",
                f"{field} contains a non-finite number",
            )
        return value
    if isinstance(value, Mapping):
        copied: dict[str, JsonValue] = {}
        for key, nested in value.items():
            if not isinstance(key, str):
                raise RuntimeEventStreamError(
                    "invalid_runtime_event",
                    f"{field} contains a non-string key",
                )
            copied[key] = _json_value(nested, field=field)
        return copied
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item, field=field) for item in value]
    raise RuntimeEventStreamError(
        "invalid_runtime_event",
        f"{field} is not JSON serializable",
    )


def _runtime_event(row: AgentRunEvent) -> RuntimeEvent:
    if row.event_type not in _EVENT_TYPES:
        raise RuntimeEventStreamError(
            "invalid_runtime_event_type",
            f"unsupported Runtime event type {row.event_type!r}",
        )
    payload = _json_value(row.payload, field="payload")
    artifact_refs = _json_value(row.artifact_refs, field="artifact_refs")
    if not isinstance(payload, dict) or not isinstance(artifact_refs, list):
        raise RuntimeEventStreamError(
            "invalid_runtime_event",
            "Runtime event payload or artifact_refs has the wrong shape",
        )
    enriched: JsonObject = {
        **payload,
        "summary": row.summary,
        "artifact_refs": artifact_refs,
    }
    return RuntimeEvent(
        tenant_id=row.tenant_id,
        run_id=row.run_id,
        event_id=row.id,
        event_type=cast(RuntimeEventType, row.event_type),
        payload=enriched,
        checkpoint_id=row.source_checkpoint_id,
        created_at=row.created_at,
    )


async def _latest_lifecycle_row(
    db: AsyncSession,
    handle: RunHandle,
    *columns: object,
):
    """Newest non-delivery event row for a Run, or None.

    The keep-alive probe and the waiting-boundary probe share this exact
    shape — same tenant/run filter, same delivery exclusion, same
    ``created_at desc, id desc`` ordering — with different column sets.
    """
    return (
        await db.execute(
            select(*columns)
            .where(
                AgentRunEvent.tenant_id == handle.tenant_id,
                AgentRunEvent.run_id == handle.run_id,
                AgentRunEvent.event_type.notin_(_DELIVERY_EVENT_TYPES),
            )
            .order_by(
                AgentRunEvent.created_at.desc(),
                AgentRunEvent.id.desc(),
            )
            .limit(1)
        )
    ).one_or_none()


async def _lane_admission_once(
    *,
    session_factory: RuntimeSessionFactory,
    handle: RunHandle,
) -> tuple[Literal["stream", "defer"], str | None]:
    """One scheduling-lane read; see ``current_lane_admission``.

    The worker's claim-eligibility query (persistence.py) stays the single
    authority on when a start command may claim the lane; this probe reads the
    same durable facts — the lane holder, the lane queue order — to decide
    whether an attached stream would sit silently behind an unclaimable start.
    It deliberately does not inspect the Run's own command rows: admission is
    a best-effort delivery filter, not an execution decision.
    """
    async with session_factory() as db:
        run_result = await db.execute(
            select(AgentRun).where(
                AgentRun.tenant_id == handle.tenant_id,
                AgentRun.id == handle.run_id,
            )
        )
        run = run_result.scalar_one_or_none()
        if (
            run is None
            or run.runtime_thread_id != handle.thread_id
            or run.lane_held
            or run.scheduling_lane_key is None
        ):
            # Unidentifiable, already the holder, or lane-free: never gate.
            return ("stream", None)
        lifecycle_row = await _latest_lifecycle_row(
            db, handle, AgentRunEvent.event_type
        )
        if (
            lifecycle_row is not None
            and lifecycle_row.event_type in _TERMINAL_EVENT_TYPES
        ):
            # A terminal Run needs no lane: stream its replay now instead of
            # parking it behind a neighbour that may itself be parked forever.
            return ("stream", None)
        holder_result = await db.execute(
            select(AgentRun.id)
            .where(
                AgentRun.scheduling_lane_key == run.scheduling_lane_key,
                AgentRun.id != run.id,
                AgentRun.lane_held.is_(True),
            )
            .limit(1)
        )
        holder_id = holder_result.scalar_one_or_none()
        if holder_id is not None:
            return ("defer", str(holder_id))
        earlier_result = await db.execute(
            select(AgentRun.id)
            .join(AgentRunCommand, AgentRunCommand.run_id == AgentRun.id)
            .where(
                AgentRun.scheduling_lane_key == run.scheduling_lane_key,
                AgentRun.id != run.id,
                AgentRun.lane_held.is_(False),
                AgentRunCommand.command_type == "start",
                AgentRunCommand.status.in_(("pending", "claimed")),
                tuple_(
                    AgentRun.scheduling_position_created_at,
                    AgentRun.scheduling_position_id,
                    AgentRun.created_at,
                    AgentRun.id,
                )
                < tuple_(
                    run.scheduling_position_created_at,  # type: ignore[arg-type]
                    run.scheduling_position_id,  # type: ignore[arg-type]
                    run.created_at,  # type: ignore[arg-type]
                    run.id,  # type: ignore[arg-type]
                ),
            )
            .order_by(
                AgentRun.scheduling_position_created_at,
                AgentRun.scheduling_position_id,
                AgentRun.created_at,
                AgentRun.id,
            )
            .limit(1)
        )
        earlier_id = earlier_result.scalar_one_or_none()
        if earlier_id is not None:
            return ("defer", str(earlier_id))
        return ("stream", None)


async def current_lane_admission(
    *,
    session_factory: RuntimeSessionFactory,
    handle: RunHandle,
) -> tuple[Literal["stream", "defer"], str | None]:
    """Decide whether a fresh intake may stream now, or must wait for its lane.

    Runs queued behind a shared scheduling lane cannot start while another Run
    holds the lane — and a holder parked at a user waiting boundary never
    releases on its own, so a stream started for a queued intake would sit
    silently behind its unclaimable start command. Probing the lane before
    pumping lets the WebSocket layer park the intake as "queued" (and re-probe
    later) instead of hanging the socket.

    Returns ``("stream", None)`` when the Run is safe to stream now,
    including every case the probe cannot read confidently: a missing or
    thread-mismatched Run row, and transient connectivity errors that exhaust
    the retry budget. Admission is best-effort and fails open. Returns
    ``("defer", blocking_run_id)`` when another Run holds the lane, or an
    earlier-positioned neighbour still owns a pending/claimed start command:
    the caller should park the intake and re-probe.

    Only ``OperationalError`` is retried — three times with a short fixed
    backoff, absorbing transient blips — before failing open; any other
    exception propagates to the caller, whose outer fail-open wrapper decides.
    """
    last_error: OperationalError | None = None
    for delay in (*_LANE_PROBE_RETRY_DELAYS, None):
        try:
            return await _lane_admission_once(
                session_factory=session_factory,
                handle=handle,
            )
        except OperationalError as exc:
            last_error = exc
            if delay is None:
                break
            await asyncio.sleep(delay)
    logger.warning(
        "stream_gate: lane admission probe failed after {} attempts for run "
        "{}; failing open (last error: {!r})",
        1 + len(_LANE_PROBE_RETRY_DELAYS),
        handle.run_id,
        last_error,
    )
    return ("stream", None)


async def run_is_terminal(
    *,
    session_factory: RuntimeSessionFactory,
    handle: RunHandle,
) -> bool:
    """True when the Run's newest non-delivery lifecycle event is terminal."""
    async with session_factory() as db:
        row = await _latest_lifecycle_row(db, handle, AgentRunEvent.event_type)
    return row is not None and row.event_type in _TERMINAL_EVENT_TYPES


async def current_start_command_status(
    *,
    session_factory: RuntimeSessionFactory,
    run_id: uuid.UUID,
) -> str | None:
    """Newest start-command status for a Run, or None without a start command."""
    async with session_factory() as db:
        result = await db.execute(
            select(AgentRunCommand.status)
            .where(
                AgentRunCommand.run_id == run_id,
                AgentRunCommand.command_type == "start",
            )
            .order_by(
                AgentRunCommand.created_at.desc(),
                AgentRunCommand.id.desc(),
            )
            .limit(1)
        )
        return result.scalar_one_or_none()


class DatabaseRuntimeEventStream:
    """Yield ordered AgentRunEvents through short-lived read sessions."""

    def __init__(
        self,
        *,
        session_factory: RuntimeSessionFactory,
        poll_interval_seconds: float = 0.25,
        batch_size: int = 100,
        idle_timeout_seconds: float | None = None,
    ) -> None:
        if poll_interval_seconds <= 0 or batch_size <= 0:
            raise ValueError("event stream polling settings must be positive")
        if idle_timeout_seconds is not None and idle_timeout_seconds <= 0:
            raise ValueError("idle_timeout_seconds must be positive")
        self._session_factory = session_factory
        self._poll_interval_seconds = poll_interval_seconds
        self._batch_size = batch_size
        self._idle_timeout_seconds = idle_timeout_seconds

    @staticmethod
    def _validate_handle(handle: RunHandle) -> None:
        if handle.runtime_type != "langgraph" or not handle.thread_id.strip():
            raise RuntimeEventStreamError(
                "runtime_identity_mismatch",
                "event stream handle is not a valid LangGraph Run identity",
            )

    async def _require_run(self, handle: RunHandle) -> AgentRun:
        async with self._session_factory() as db:
            result = await db.execute(
                select(AgentRun).where(
                    AgentRun.tenant_id == handle.tenant_id,
                    AgentRun.id == handle.run_id,
                )
            )
            run = result.scalar_one_or_none()
        if run is None:
            raise RuntimeEventStreamError(
                "run_not_found",
                "event stream Run does not exist in its tenant",
            )
        if (
            run.runtime_type != "langgraph"
            or run.runtime_thread_id != handle.thread_id
        ):
            raise RuntimeEventStreamError(
                "runtime_identity_mismatch",
                "event stream handle does not match the stored LangGraph Run identity",
            )
        return run

    async def _worker_alive(self, db: AsyncSession, handle: RunHandle) -> bool:
        """Check durable liveness signals before declaring a silent Run dead.

        The command worker renews its claim every
        ``AGENT_RUNTIME_COMMAND_CLAIM_RENEW_SECONDS`` for the whole Run, and the
        tool step service renews tool execution leases while a long-running
        tool handler is active. Long tools stream their output out-of-band
        without persisting events, so an idle event table alone does not mean
        the worker stopped processing the Run. Only when every liveness signal
        has gone cold is the Run truly unattended.

        A Run parked at a waiting boundary (LangGraph interrupt) is a third,
        legitimate resting state: its command is applied with no claim, no
        tool lease is open, and it stays parked until external input resumes
        it. Such a Run is idle by design, not dead. It is kept alive with a
        warning trace so a worker that actually died while parked remains
        forensically visible (repeated keep-alive logs with no recovery).
        """
        now = datetime.now(UTC)
        command_result = await db.execute(
            select(
                AgentRunCommand.status,
                AgentRunCommand.claim_expires_at,
            ).where(
                AgentRunCommand.tenant_id == handle.tenant_id,
                AgentRunCommand.id == handle.command_id,
            )
        )
        command_row = command_result.one_or_none()
        if command_row is not None:
            if (
                command_row.claim_expires_at is not None
                and command_row.claim_expires_at > now
            ):
                return True
            if command_row.status == "pending":
                # Queued but not yet claimed; keep the stream waiting.
                return True
        lease_result = await db.execute(
            select(
                exists().where(
                    AgentToolExecution.tenant_id == handle.tenant_id,
                    AgentToolExecution.run_id == handle.run_id,
                    AgentToolExecution.status == "started",
                    AgentToolExecution.lease_expires_at > now,
                )
            )
        )
        if lease_result.scalar():
            return True
        lifecycle_row = await _latest_lifecycle_row(
            db, handle, AgentRunEvent.event_type, AgentRunEvent.payload
        )
        if lifecycle_row is not None and lifecycle_row.event_type == "waiting_started":
            payload = (
                lifecycle_row.payload if isinstance(lifecycle_row.payload, dict) else {}
            )
            logger.warning(
                "Run {} is parked at a waiting boundary; keeping its idle "
                "event stream alive (waiting_type={})",
                handle.run_id,
                payload.get("waiting_type"),
            )
            return True
        return False

    async def current_waiting_boundary(self, handle: RunHandle) -> RuntimeEvent | None:
        """Return the Run's open user waiting boundary when it is safely parked.

        Short-circuit signal for a stream attachment whose client cursor is
        already at or past the boundary: a Run parked at a user-facing wait
        with no resume command in its history is definitively parked. Any
        resume command row disqualifies the short-circuit — the Run resumed
        at least once and may be mid-execution or re-waiting, and the
        ``resumed`` event can lag the checkpoint boundary by a long time, so
        the event order is not a reliable signal there. Returns None unless
        the attachment may end at the boundary immediately.
        """
        async with self._session_factory() as db:
            row = await _latest_lifecycle_row(
                db,
                handle,
                AgentRunEvent.tenant_id,
                AgentRunEvent.run_id,
                AgentRunEvent.event_type,
                AgentRunEvent.payload,
                AgentRunEvent.created_at,
                AgentRunEvent.id,
                AgentRunEvent.summary,
                AgentRunEvent.artifact_refs,
                AgentRunEvent.source_checkpoint_id,
            )
            if row is None or row.event_type != "waiting_started":
                return None
            payload = row.payload if isinstance(row.payload, dict) else {}
            if payload.get("waiting_type") not in {"user", "waiting_user"}:
                return None
            resume_result = await db.execute(
                select(
                    exists().where(
                        AgentRunCommand.tenant_id == handle.tenant_id,
                        AgentRunCommand.run_id == handle.run_id,
                        AgentRunCommand.command_type == "resume",
                    )
                )
            )
            if resume_result.scalar():
                return None
        return _runtime_event(row)

    async def stream_run(
        self,
        handle: RunHandle,
        *,
        after: RuntimeEventCursor | None = None,
    ) -> AsyncIterator[RuntimeEvent]:
        self._validate_handle(handle)
        await self._require_run(handle)
        cursor = after
        terminal_seen = False
        last_event_time = time_module.monotonic()

        while True:
            async with self._session_factory() as db:
                events_result = await db.execute(
                    _event_statement(
                        handle,
                        after=cursor,
                        batch_size=self._batch_size,
                    )
                )
                rows = list(events_result.scalars().all())
                status_result = await db.execute(
                    select(AgentRun.delivery_status).where(
                        AgentRun.tenant_id == handle.tenant_id,
                        AgentRun.id == handle.run_id,
                    )
                )
                delivery_status = status_result.scalar_one_or_none()
            if delivery_status is None:
                raise RuntimeEventStreamError(
                    "run_not_found",
                    "event stream Run disappeared from its tenant",
                )

            delivery_event_seen = False
            for row in rows:
                event = _runtime_event(row)
                if event.created_at is None or event.event_id is None:
                    raise RuntimeEventStreamError(
                        "invalid_runtime_event_position",
                        "persisted Runtime event has no reconnect position",
                    )
                cursor = RuntimeEventCursor(event.created_at, event.event_id)
                terminal_seen = terminal_seen or event.event_type in _TERMINAL_EVENT_TYPES
                delivery_event_seen = delivery_event_seen or event.event_type in _DELIVERY_EVENT_TYPES
                last_event_time = time_module.monotonic()
                yield event

            if terminal_seen and (
                delivery_event_seen or delivery_status in _SETTLED_DELIVERY_STATUSES
            ):
                return

            if (
                self._idle_timeout_seconds is not None
                and time_module.monotonic() - last_event_time > self._idle_timeout_seconds
            ):
                async with self._session_factory() as db:
                    worker_alive = await self._worker_alive(db, handle)
                if worker_alive:
                    # A live worker can be legitimately busy without new
                    # events (long tools stream output out-of-band). Restart
                    # the idle clock instead of killing the stream.
                    last_event_time = time_module.monotonic()
                    continue
                raise RuntimeEventStreamError(
                    "runtime_event_stream_idle_timeout",
                    f"No Runtime events received for {self._idle_timeout_seconds:.0f}s "
                    "and the Run's command worker shows no liveness signal",
                )

            await asyncio.sleep(self._poll_interval_seconds)


__all__ = [
    "DatabaseRuntimeEventStream",
    "RuntimeEventStreamError",
]
