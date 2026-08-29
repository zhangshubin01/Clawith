"""Idempotent heartbeat activity projection from terminal Runtime checkpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from collections.abc import Mapping
from typing import Callable
import logging
import uuid

from sqlalchemy import select

from app.models.activity_log import AgentActivityLog
from app.models.agent_run import AgentRun
from app.models.notification import Notification
from app.services.agent_runtime.command_worker import (
    CheckpointObservation,
    RuntimeRunRecord,
    RuntimeSessionFactory,
)
from app.services.focus_service import (
    complete_focus_item,
    list_focus_items,
    slugify_focus_key,
    upsert_focus_item,
)
from app.services.storage import get_storage_backend, normalize_storage_key

logger = logging.getLogger(__name__)


class HeartbeatRuntimeCompletionError(RuntimeError):
    """A completed heartbeat Run cannot be projected safely."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_BACKGROUND_MODES = frozenset({"heartbeat", "schedule", "oneshot"})
_NEXT_CYCLE_SEEDS_SECTION = "Next Cycle Seeds"


def _effect_id(run_id: uuid.UUID, checkpoint_id: str, mode: str) -> uuid.UUID:
    return uuid.uuid5(run_id, f"{mode}-terminal:{checkpoint_id}")


def _is_heartbeat_ok(answer: str) -> bool:
    return "HEARTBEAT_OK" in answer.upper().replace(" ", "_")


def _mode(checkpoint: CheckpointObservation) -> str:
    initial_input = checkpoint.state["snapshots"].initial_input
    mode = initial_input.get("background_mode", "heartbeat")
    if not isinstance(mode, str) or mode not in _BACKGROUND_MODES:
        raise HeartbeatRuntimeCompletionError(
            "background_mode_invalid",
            "heartbeat-source Run has an unsupported background mode",
        )
    return str(mode)


def _answer(checkpoint: CheckpointObservation, *, mode: str) -> str:
    answer = checkpoint.state["lifecycle"].get("final_answer")
    if not isinstance(answer, str) or not answer.strip():
        raise HeartbeatRuntimeCompletionError(
            f"missing_{mode}_result",
            f"completed {mode} checkpoint has no final answer",
        )
    return answer.strip()


def _failure_code(checkpoint: CheckpointObservation) -> str:
    lifecycle = checkpoint.state["lifecycle"]
    error = lifecycle.get("error")
    if isinstance(error, Mapping):
        code = error.get("code")
        if isinstance(code, str) and code.strip():
            return code.strip()
    reason = lifecycle.get("reason")
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    return str(lifecycle["status"])


def _require_source(
    stored_run: AgentRun | None,
    *,
    mode: str,
    agent_id: uuid.UUID,
    initial_input: Mapping[str, object],
) -> uuid.UUID | None:
    if stored_run is None or stored_run.source_execution_id is None:
        raise HeartbeatRuntimeCompletionError(
            f"{mode}_source_mismatch",
            f"terminal {mode} Run has inconsistent source identity",
        )
    related_id = None
    if mode == "heartbeat":
        valid = (
            stored_run.source_id == str(agent_id)
            and stored_run.source_execution_id.startswith(f"heartbeat:{agent_id}:")
        )
    elif mode == "oneshot":
        valid = (
            stored_run.source_id == str(agent_id)
            and stored_run.source_execution_id.startswith(f"oneshot:{agent_id}:")
        )
    else:
        raw_schedule_id = initial_input.get("schedule_id")
        try:
            related_id = uuid.UUID(str(raw_schedule_id))
        except (TypeError, ValueError):
            valid = False
        else:
            valid = (
                stored_run.source_id == str(related_id)
                and stored_run.source_execution_id.startswith(
                    f"schedule:{related_id}:"
                )
            )
    if not valid:
        raise HeartbeatRuntimeCompletionError(
            f"{mode}_source_mismatch",
            f"terminal {mode} Run has inconsistent source identity",
        )
    return related_id


class HeartbeatRuntimeCompletionHandler:
    """Project heartbeat-source background modes from terminal checkpoints."""

    def __init__(
        self,
        *,
        session_factory: RuntimeSessionFactory,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    async def handle(
        self,
        *,
        run: RuntimeRunRecord,
        checkpoint: CheckpointObservation,
    ) -> None:
        if run.source_type != "heartbeat":
            return
        lifecycle = checkpoint.state["lifecycle"]
        status = lifecycle["status"]
        if status not in _TERMINAL_STATUSES:
            return
        mode = _mode(checkpoint)
        if mode in {"heartbeat", "schedule"} and status != "completed":
            return
        answer = _answer(checkpoint, mode=mode) if status == "completed" else None
        if mode == "heartbeat" and answer is not None and _is_heartbeat_ok(answer):
            return
        initial_input = checkpoint.state["snapshots"].initial_input
        if mode == "oneshot" and status == "completed":
            return
        raw_triggered_by = initial_input.get("triggered_by_user_id")
        if mode == "oneshot" and raw_triggered_by is None:
            return
        try:
            agent_id = uuid.UUID(run.agent_id or "")
        except ValueError as exc:
            raise HeartbeatRuntimeCompletionError(
                "invalid_heartbeat_agent",
                "heartbeat Run has no valid Agent identity",
            ) from exc

        effect_id = _effect_id(run.run_id, checkpoint.checkpoint_id, mode)
        async with self._session_factory() as db:
            async with db.begin():
                run_result = await db.execute(
                    select(AgentRun).where(
                        AgentRun.tenant_id == run.tenant_id,
                        AgentRun.id == run.run_id,
                        AgentRun.source_type == "heartbeat",
                    )
                )
                stored_run = run_result.scalar_one_or_none()
                if stored_run is None or stored_run.agent_id != agent_id:
                    raise HeartbeatRuntimeCompletionError(
                        f"{mode}_source_mismatch",
                        f"terminal {mode} Run has inconsistent Agent identity",
                    )
                related_id = _require_source(
                    stored_run,
                    mode=mode,
                    agent_id=agent_id,
                    initial_input=initial_input,
                )

                if mode == "oneshot":
                    try:
                        triggered_by = uuid.UUID(str(raw_triggered_by))
                    except (TypeError, ValueError) as exc:
                        raise HeartbeatRuntimeCompletionError(
                            "oneshot_user_invalid",
                            "terminal oneshot Run has no valid triggering user",
                        ) from exc
                    receipt_result = await db.execute(
                        select(Notification.id).where(Notification.id == effect_id)
                    )
                    if receipt_result.scalar_one_or_none() is not None:
                        return
                    agent_name = initial_input.get("agent_name")
                    safe_agent_name = (
                        agent_name.strip()
                        if isinstance(agent_name, str) and agent_name.strip()
                        else "Agent"
                    )
                    db.add(
                        Notification(
                            id=effect_id,
                            user_id=triggered_by,
                            type="system",
                            title=f"{safe_agent_name} task failed",
                            body=f"任务执行未完成（{_failure_code(checkpoint)}）",
                            link=f"/agents/{agent_id}#chat",
                            ref_id=agent_id,
                            sender_name=safe_agent_name,
                        )
                    )
                    await db.flush()
                    return

                receipt_result = await db.execute(
                    select(AgentActivityLog.id).where(
                        AgentActivityLog.id == effect_id
                    )
                )
                if receipt_result.scalar_one_or_none() is not None:
                    return

                assert answer is not None
                if mode == "schedule":
                    instruction = initial_input.get("schedule_instruction")
                    safe_instruction = (
                        instruction.strip()
                        if isinstance(instruction, str) and instruction.strip()
                        else stored_run.goal
                    )
                    action_type = "schedule_run"
                    summary = f"定时任务执行: {safe_instruction[:60]}"
                    detail = {
                        "schedule_id": str(related_id),
                        "instruction": safe_instruction,
                        "reply": answer[:500],
                    }
                else:
                    action_type = "heartbeat"
                    summary = f"Heartbeat: {answer[:80]}"
                    detail = {"reply": answer[:500]}
                    related_id = run.run_id
                db.add(
                    AgentActivityLog(
                        id=effect_id,
                        agent_id=agent_id,
                        action_type=action_type,
                        summary=summary,
                        detail_json=detail,
                        related_id=related_id,
                        created_at=self._clock(),
                    )
                )
                await db.flush()


def _extract_seed_lines(content: str) -> list[str] | None:
    """Extract the Next Cycle Seeds entries from a reflections.md body.

    ``None`` means "no signal": the section is absent (or the content is not a
    usable reflections body), so callers must not project anything. An empty
    list means the section exists but holds no seeds — the agent deliberately
    cleared it, which *is* a signal.
    """
    if not content.strip():
        return None
    section_found = False
    in_section = False
    seeds: list[str] = []
    for raw_line in content.split("\n"):
        if raw_line.startswith("## "):
            if in_section:
                break
            if raw_line[3:].strip() == _NEXT_CYCLE_SEEDS_SECTION:
                in_section = True
                section_found = True
            continue
        if not in_section:
            continue
        stripped = raw_line.strip()
        if stripped.startswith("- "):
            text = stripped[2:].strip()
            if text:
                seeds.append(text)
    if not section_found:
        return None
    return seeds


class HeartbeatSeedFocusHandler:
    """Project a completed heartbeat's Next Cycle Seeds into Focus items.

    Complementary to ``HeartbeatRuntimeCompletionHandler`` (which projects
    terminal heartbeats into activity/notifications and skips HEARTBEAT_OK
    answers): this handler takes every completed heartbeat's seed list, upserts
    it into the Focus table as ``source="heartbeat"`` items, and retires
    heartbeat-source items the converge dropped. Best-effort by design — any
    failure is logged, never raised, so a projection problem can neither fail
    the heartbeat nor block the other terminal handlers.
    """

    def __init__(self, *, session_factory: RuntimeSessionFactory) -> None:
        # Kept for terminal-handler construction symmetry; the projection uses
        # the Focus service, which manages its own sessions.
        self._session_factory = session_factory

    async def handle(
        self,
        *,
        run: RuntimeRunRecord,
        checkpoint: CheckpointObservation,
    ) -> None:
        if run.source_type != "heartbeat":
            return
        try:
            status = checkpoint.state["lifecycle"].get("status")
            if status != "completed":
                return
            if _mode(checkpoint) != "heartbeat":
                return
            agent_id = uuid.UUID(run.agent_id or "")
        except Exception:
            return
        try:
            await self._project_seeds(agent_id)
        except Exception as exc:
            logger.warning(
                "Heartbeat seed→Focus projection failed for agent %s: %s",
                agent_id,
                exc,
            )

    async def _project_seeds(self, agent_id: uuid.UUID) -> None:
        storage = get_storage_backend()
        reflections_key = normalize_storage_key(
            f"{agent_id}/memory/reflections.md"
        )
        try:
            if not await storage.exists(reflections_key) or not await storage.is_file(
                reflections_key
            ):
                return
            content = await storage.read_text(
                reflections_key,
                encoding="utf-8",
                errors="replace",
            )
        except Exception:
            return
        seeds = _extract_seed_lines(content)
        if seeds is None:
            return

        seed_keys = {slugify_focus_key(text) for text in seeds}
        items = await list_focus_items(agent_id, include_completed=True)
        heartbeat_keys = {
            item["key"] for item in items if item.get("source") == "heartbeat"
        }

        for text in seeds:
            await upsert_focus_item(
                agent_id,
                key=slugify_focus_key(text),
                title=text[:200],
                description=text,
                status="in_progress",
                kind="normal",
                source="heartbeat",
            )
        for stale_key in heartbeat_keys - seed_keys:
            await complete_focus_item(agent_id, key=stale_key)


__all__ = [
    "HeartbeatRuntimeCompletionError",
    "HeartbeatRuntimeCompletionHandler",
    "HeartbeatSeedFocusHandler",
]
