"""Build immutable Run inputs and model-facing Runtime Context sections.

The builder accepts only checkpoint contracts from ``state.py``.  It never
loads a mutable Run ORM row and therefore cannot accidentally use a query
projection as execution state.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass
import math
from typing import Any
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.services.agent_runtime.session_context_service import (
    SessionContextService,
    SessionContextSnapshot,
)
from app.services.agent_runtime.state import (
    JsonObject,
    JsonValue,
    RunInputSnapshots,
    RuntimeGraphState,
)
from app.services.agent_runtime.tool_exchange import (
    Ledger,
    TokenCounter,
    ToolExchangeCompactionSummary,
    build_recent_tool_safe_window,
)


class ContextBuildError(RuntimeError):
    """Checkpoint input cannot be assembled into safe model context."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class RuntimeContextBuild:
    """Structured context plus directives produced by Tool Exchange selection."""

    session_context_snapshot: JsonObject
    current_run: JsonObject
    related_run_summaries: tuple[JsonObject, ...]
    recent_session_messages_snapshot: tuple[JsonObject, ...]
    recent_run_messages: tuple[JsonObject, ...]
    initial_input: JsonObject
    resume_input: JsonValue | None
    omitted_tool_exchanges: tuple[ToolExchangeCompactionSummary, ...]
    retry_model: bool
    blocked: bool
    requires_confirmation: bool

    def to_json(self) -> JsonObject:
        """Return the serializable prompt sections without control metadata loss."""
        return {
            "session_context_snapshot": deepcopy(self.session_context_snapshot),
            "current_run": deepcopy(self.current_run),
            "related_run_summaries": [deepcopy(summary) for summary in self.related_run_summaries],
            "recent_session_messages_snapshot": [
                deepcopy(message) for message in self.recent_session_messages_snapshot
            ],
            "recent_run_messages": [deepcopy(message) for message in self.recent_run_messages],
            "initial_input": deepcopy(self.initial_input),
            "resume_input": deepcopy(self.resume_input),
            "omitted_tool_exchanges": [_tool_summary_to_json(summary) for summary in self.omitted_tool_exchanges],
            "retry_model": self.retry_model,
            "blocked": self.blocked,
            "requires_confirmation": self.requires_confirmation,
        }


def _json_value(value: object, *, field: str) -> JsonValue:
    if value is None or isinstance(value, (str, int, bool)):
        return deepcopy(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContextBuildError(
                "invalid_runtime_context",
                f"{field} contains a non-finite number",
            )
        return value
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ContextBuildError(
                    "invalid_runtime_context",
                    f"{field} contains a non-string object key",
                )
            result[key] = _json_value(nested, field=field)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(nested, field=field) for nested in value]
    raise ContextBuildError(
        "invalid_runtime_context",
        f"{field} contains a value that is not JSON serializable",
    )


def _json_object(value: object, *, field: str) -> JsonObject:
    copied = _json_value(value, field=field)
    if not isinstance(copied, dict):
        raise ContextBuildError(
            "invalid_runtime_context",
            f"{field} must be an object",
        )
    return copied


def _json_objects(value: object, *, field: str) -> tuple[JsonObject, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ContextBuildError(
            "invalid_runtime_context",
            f"{field} must be an array",
        )
    return tuple(_json_object(item, field=f"{field}[{index}]") for index, item in enumerate(value))


def _tool_summary_to_json(summary: ToolExchangeCompactionSummary) -> JsonObject:
    return _json_object(asdict(summary), field="omitted_tool_exchange")


def _empty_session_snapshot() -> JsonObject:
    return SessionContextSnapshot.empty().to_json()


def _current_run_section(state: RuntimeGraphState) -> JsonObject:
    registry = state["registry"]
    lifecycle = state["lifecycle"]
    return _json_object(
        {
            "run_id": registry.run_id,
            "tenant_id": registry.tenant_id,
            "agent_id": registry.agent_id,
            "session_id": registry.session_id,
            "goal": registry.goal,
            "run_kind": registry.run_kind,
            "source_type": registry.source_type,
            "model_id": registry.model_id,
            "graph_name": registry.graph_name,
            "graph_version": registry.graph_version,
            "system_role": registry.system_role,
            "parent_run_id": registry.parent_run_id,
            "root_run_id": registry.root_run_id,
            "lifecycle_status": lifecycle["status"],
            "next_route": lifecycle["next_route"],
            "reason": lifecycle.get("reason"),
            "run_summary": lifecycle.get("run_summary"),
            "pending_tool_calls": lifecycle.get("pending_tool_calls", []),
            "waiting_request": lifecycle.get("waiting_request"),
            "verification_result": lifecycle.get("verification_result"),
        },
        field="current_run",
    )


def _validate_session_messages(messages: Sequence[Mapping[str, Any]]) -> None:
    for message in messages:
        if message.get("role") not in {"user", "assistant"}:
            raise ContextBuildError(
                "invalid_session_message",
                "recent Session messages must contain only user-visible roles",
            )
        message_id = message.get("id")
        if not isinstance(message_id, str) or not message_id:
            raise ContextBuildError(
                "invalid_session_message",
                "recent Session messages require stable IDs",
            )


class ContextBuilder:
    """Capture new-Run snapshots and select a tool-safe active message window."""

    def __init__(
        self,
        session_context_service: SessionContextService,
        *,
        recent_run_message_limit: int | None = None,
        settings: Settings | None = None,
    ) -> None:
        runtime_settings = settings or get_settings()
        self.session_context_service = session_context_service
        self.recent_run_message_limit = (
            recent_run_message_limit
            if recent_run_message_limit is not None
            else runtime_settings.AGENT_RUNTIME_SESSION_RECENT_MESSAGES
        )
        if self.recent_run_message_limit <= 0:
            raise ValueError("recent_run_message_limit must be greater than zero")

    async def capture_run_inputs(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID | None,
        initial_input: Mapping[str, Any],
        related_run_summaries: Sequence[Mapping[str, Any]] = (),
    ) -> RunInputSnapshots:
        """Freeze new-Run inputs; resumed Runs must reuse the checkpoint copy."""
        normalized_input = _json_object(initial_input, field="initial_input")
        normalized_related = _json_objects(
            related_run_summaries,
            field="related_run_summaries",
        )
        if session_id is None:
            session_context = _empty_session_snapshot()
            session_context_version = 0
            recent_messages: tuple[JsonObject, ...] = ()
        else:
            pack = await self.session_context_service.load_context_pack(
                db,
                tenant_id=tenant_id,
                session_id=session_id,
            )
            session_context = pack.snapshot.to_json()
            session_context_version = pack.snapshot.version
            recent_messages = _json_objects(
                pack.recent_messages,
                field="recent_session_messages",
            )
            _validate_session_messages(recent_messages)

        return RunInputSnapshots(
            session_context=session_context,
            session_context_version=session_context_version,
            recent_session_messages=recent_messages,
            related_run_summaries=normalized_related,
            initial_input=normalized_input,
        )

    async def build(
        self,
        state: RuntimeGraphState,
        *,
        resume_input: JsonValue | None = None,
        tool_execution_ledger: Ledger | None = None,
        run_message_token_budget: int | None = None,
        token_counter: TokenCounter | None = None,
    ) -> RuntimeContextBuild:
        """Build from the fixed checkpoint snapshot without refreshing the session."""
        snapshots = state["snapshots"]
        session_context = _json_object(
            snapshots.session_context,
            field="session_context_snapshot",
        )
        if snapshots.session_context_version != session_context.get("version"):
            raise ContextBuildError(
                "invalid_session_context_snapshot",
                "checkpoint Session Context version disagrees with its snapshot",
            )

        recent_session_messages = _json_objects(
            snapshots.recent_session_messages,
            field="recent_session_messages_snapshot",
        )
        _validate_session_messages(recent_session_messages)
        related_run_summaries = _json_objects(
            snapshots.related_run_summaries,
            field="related_run_summaries",
        )

        lifecycle = state["lifecycle"]
        run_messages = _json_objects(
            lifecycle.get("run_messages", []),
            field="run_messages",
        )
        selection = build_recent_tool_safe_window(
            run_messages,
            tool_execution_ledger,
            target_messages=self.recent_run_message_limit,
            token_budget=run_message_token_budget,
            token_counter=token_counter,
        )
        selected_run_messages = _json_objects(
            selection.messages,
            field="selected_run_messages",
        )

        return RuntimeContextBuild(
            session_context_snapshot=session_context,
            current_run=_current_run_section(state),
            related_run_summaries=related_run_summaries,
            recent_session_messages_snapshot=recent_session_messages,
            recent_run_messages=selected_run_messages,
            initial_input=_json_object(
                snapshots.initial_input,
                field="initial_input",
            ),
            resume_input=_json_value(resume_input, field="resume_input"),
            omitted_tool_exchanges=selection.compaction_summaries,
            retry_model=selection.retry_model,
            blocked=selection.blocked,
            requires_confirmation=selection.requires_confirmation,
        )


__all__ = [
    "ContextBuildError",
    "ContextBuilder",
    "RuntimeContextBuild",
]
