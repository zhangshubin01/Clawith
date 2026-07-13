"""Session Context reads and optimistic writes for Agent Runtime.

Session Context is a product-owned, session-level summary.  It is deliberately
separate from LangGraph checkpoints: checkpoints resume one Run, while this
service supplies a versioned background snapshot for newly-created Runs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import math
from typing import Any
import uuid

from sqlalchemy import String, and_, cast as sa_cast, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.session_context_state import SessionContextState
from app.services.agent_runtime.state import JsonObject, JsonValue


_USER_VISIBLE_ROLES = ("user", "assistant")


class SessionContextError(RuntimeError):
    """A Session Context operation cannot safely continue."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SessionContextConflict(SessionContextError):
    """The expected Session Context version or watermark is stale."""

    def __init__(self) -> None:
        super().__init__(
            "session_context_conflict",
            "Session Context changed before the compare-and-swap completed",
        )


@dataclass(frozen=True, slots=True)
class MessagePosition:
    """Stable ChatMessage ordering key; UUID alone is never an ordering key."""

    created_at: datetime
    message_id: uuid.UUID

    @property
    def sort_key(self) -> tuple[datetime, int]:
        return self.created_at, self.message_id.int


@dataclass(frozen=True, slots=True)
class SessionContextSnapshot:
    """One immutable read of the current rolling Session Context."""

    version: int
    summary: str
    requirements: tuple[JsonValue, ...]
    decisions: tuple[JsonValue, ...]
    open_items: tuple[JsonValue, ...]
    evidence_refs: tuple[JsonValue, ...]
    workspace_refs: tuple[JsonValue, ...]
    covered_through_message_id: uuid.UUID | None

    @classmethod
    def empty(cls) -> "SessionContextSnapshot":
        """Represent an existing session that has not been compacted yet."""
        return cls(
            version=0,
            summary="",
            requirements=(),
            decisions=(),
            open_items=(),
            evidence_refs=(),
            workspace_refs=(),
            covered_through_message_id=None,
        )

    def to_json(self) -> JsonObject:
        return {
            "version": self.version,
            "summary": self.summary,
            "requirements": _copy_json_sequence(self.requirements, "requirements"),
            "decisions": _copy_json_sequence(self.decisions, "decisions"),
            "open_items": _copy_json_sequence(self.open_items, "open_items"),
            "evidence_refs": _copy_json_sequence(self.evidence_refs, "evidence_refs"),
            "workspace_refs": _copy_json_sequence(self.workspace_refs, "workspace_refs"),
            "covered_through_message_id": (
                str(self.covered_through_message_id) if self.covered_through_message_id is not None else None
            ),
        }


@dataclass(frozen=True, slots=True)
class SessionContextCandidate:
    """Validated candidate content written through version-and-watermark CAS."""

    summary: str
    requirements: Sequence[JsonValue] = ()
    decisions: Sequence[JsonValue] = ()
    open_items: Sequence[JsonValue] = ()
    evidence_refs: Sequence[JsonValue] = ()
    workspace_refs: Sequence[JsonValue] = ()
    covered_through_message_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class SessionContextPack:
    """Session snapshot plus the fixed recent user-visible message window."""

    snapshot: SessionContextSnapshot
    recent_messages: tuple[JsonObject, ...]


def _copy_json_value(value: object, field: str) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SessionContextError(
                "invalid_session_context",
                f"{field} contains a non-finite number",
            )
        return value
    if isinstance(value, Mapping):
        copied: dict[str, JsonValue] = {}
        for key, nested in value.items():
            if not isinstance(key, str):
                raise SessionContextError(
                    "invalid_session_context",
                    f"{field} contains a non-string object key",
                )
            copied[key] = _copy_json_value(nested, field)
        return copied
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_copy_json_value(nested, field) for nested in value]
    raise SessionContextError(
        "invalid_session_context",
        f"{field} contains a value that is not JSON serializable",
    )


def _copy_json_sequence(values: object, field: str) -> list[JsonValue]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise SessionContextError(
            "invalid_session_context",
            f"{field} must be an array",
        )
    return [_copy_json_value(value, field) for value in values]


def _snapshot_from_row(row: SessionContextState) -> SessionContextSnapshot:
    if row.version < 1:
        raise SessionContextError(
            "invalid_session_context",
            "persisted Session Context version must be at least 1",
        )
    return SessionContextSnapshot(
        version=row.version,
        summary=row.summary,
        requirements=tuple(_copy_json_sequence(row.requirements, "requirements")),
        decisions=tuple(_copy_json_sequence(row.decisions, "decisions")),
        open_items=tuple(_copy_json_sequence(row.open_items, "open_items")),
        evidence_refs=tuple(_copy_json_sequence(row.evidence_refs, "evidence_refs")),
        workspace_refs=tuple(_copy_json_sequence(row.workspace_refs, "workspace_refs")),
        covered_through_message_id=row.covered_through_message_id,
    )


def _message_to_json(message: ChatMessage) -> JsonObject:
    if message.role not in _USER_VISIBLE_ROLES:
        raise SessionContextError(
            "invalid_session_message",
            f"message {message.id} is not user-visible",
        )
    if message.created_at is None:
        raise SessionContextError(
            "invalid_session_message",
            f"message {message.id} has no Message Position",
        )
    return {
        "id": str(message.id),
        "role": message.role,
        "content": message.content,
        "created_at": message.created_at.isoformat(),
        "agent_id": str(message.agent_id) if message.agent_id is not None else None,
        "user_id": str(message.user_id) if message.user_id is not None else None,
        "participant_id": (str(message.participant_id) if message.participant_id is not None else None),
        "mentions": deepcopy(message.mentions or []),
    }


def _session_statement(tenant_id: uuid.UUID, session_id: uuid.UUID):
    return select(ChatSession).where(
        ChatSession.tenant_id == tenant_id,
        ChatSession.id == session_id,
        ChatSession.deleted_at.is_(None),
    )


def _state_statement(tenant_id: uuid.UUID, session_id: uuid.UUID):
    return select(SessionContextState).where(
        SessionContextState.tenant_id == tenant_id,
        SessionContextState.session_id == session_id,
    )


def _message_scope(tenant_id: uuid.UUID, session_id: uuid.UUID):
    return (
        ChatSession.tenant_id == tenant_id,
        ChatSession.id == session_id,
        ChatSession.deleted_at.is_(None),
        ChatMessage.conversation_id == sa_cast(ChatSession.id, String),
        ChatMessage.role.in_(_USER_VISIBLE_ROLES),
        ChatMessage.created_at.is_not(None),
    )


def _recent_messages_statement(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    limit: int,
):
    return (
        select(ChatMessage)
        .join(
            ChatSession,
            ChatMessage.conversation_id == sa_cast(ChatSession.id, String),
        )
        .where(*_message_scope(tenant_id, session_id))
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .limit(limit)
    )


def _watermark_statement(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    message_id: uuid.UUID,
):
    return (
        select(ChatMessage)
        .join(
            ChatSession,
            ChatMessage.conversation_id == sa_cast(ChatSession.id, String),
        )
        .where(
            *_message_scope(tenant_id, session_id),
            ChatMessage.id == message_id,
        )
    )


def _incremental_messages_statement(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    watermark: MessagePosition | None,
):
    statement = (
        select(ChatMessage)
        .join(
            ChatSession,
            ChatMessage.conversation_id == sa_cast(ChatSession.id, String),
        )
        .where(*_message_scope(tenant_id, session_id))
    )
    if watermark is not None:
        statement = statement.where(
            or_(
                ChatMessage.created_at > watermark.created_at,
                and_(
                    ChatMessage.created_at == watermark.created_at,
                    ChatMessage.id > watermark.message_id,
                ),
            )
        )
    return statement.order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())


def _candidate_values(candidate: SessionContextCandidate) -> dict[str, Any]:
    if not isinstance(candidate.summary, str):
        raise SessionContextError(
            "invalid_session_context",
            "summary must be a string",
        )
    return {
        "summary": candidate.summary,
        "requirements": _copy_json_sequence(candidate.requirements, "requirements"),
        "decisions": _copy_json_sequence(candidate.decisions, "decisions"),
        "open_items": _copy_json_sequence(candidate.open_items, "open_items"),
        "evidence_refs": _copy_json_sequence(candidate.evidence_refs, "evidence_refs"),
        "workspace_refs": _copy_json_sequence(candidate.workspace_refs, "workspace_refs"),
        "covered_through_message_id": candidate.covered_through_message_id,
    }


class SessionContextService:
    """Read and update the one current Session Context for an active session."""

    def __init__(
        self,
        *,
        recent_message_limit: int | None = None,
        settings: Settings | None = None,
    ) -> None:
        runtime_settings = settings or get_settings()
        self.recent_message_limit = (
            recent_message_limit
            if recent_message_limit is not None
            else runtime_settings.AGENT_RUNTIME_SESSION_RECENT_MESSAGES
        )
        if self.recent_message_limit <= 0:
            raise ValueError("recent_message_limit must be greater than zero")

    async def _require_active_session(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
    ) -> ChatSession:
        result = await db.execute(_session_statement(tenant_id, session_id))
        session = result.scalar_one_or_none()
        if session is None:
            raise SessionContextError(
                "session_context_unavailable",
                "session does not exist in the tenant or has been deleted",
            )
        return session

    @staticmethod
    def _expected_agent_id(session: ChatSession) -> uuid.UUID | None:
        return None if session.session_type == "group" else session.agent_id

    async def _load_snapshot_for_session(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        session: ChatSession,
    ) -> SessionContextSnapshot:
        result = await db.execute(_state_statement(tenant_id, session.id))
        row = result.scalar_one_or_none()
        if row is None:
            return SessionContextSnapshot.empty()
        expected_agent_id = self._expected_agent_id(session)
        if row.agent_id != expected_agent_id:
            raise SessionContextError(
                "invalid_session_context_scope",
                "persisted Session Context agent scope does not match its session",
            )
        return _snapshot_from_row(row)

    async def load_snapshot(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
    ) -> SessionContextSnapshot:
        session = await self._require_active_session(
            db,
            tenant_id=tenant_id,
            session_id=session_id,
        )
        return await self._load_snapshot_for_session(
            db,
            tenant_id=tenant_id,
            session=session,
        )

    async def _load_recent_for_session(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        session: ChatSession,
        limit: int,
    ) -> tuple[JsonObject, ...]:
        result = await db.execute(_recent_messages_statement(tenant_id, session.id, limit=limit))
        newest_first = list(result.scalars().all())
        return tuple(_message_to_json(message) for message in reversed(newest_first))

    async def load_recent_user_visible_messages(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        limit: int | None = None,
    ) -> tuple[JsonObject, ...]:
        selected_limit = self.recent_message_limit if limit is None else limit
        if selected_limit <= 0:
            raise ValueError("limit must be greater than zero")
        session = await self._require_active_session(
            db,
            tenant_id=tenant_id,
            session_id=session_id,
        )
        return await self._load_recent_for_session(
            db,
            tenant_id=tenant_id,
            session=session,
            limit=selected_limit,
        )

    async def load_context_pack(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
    ) -> SessionContextPack:
        """Capture the current summary and recent window for one new Run."""
        session = await self._require_active_session(
            db,
            tenant_id=tenant_id,
            session_id=session_id,
        )
        snapshot = await self._load_snapshot_for_session(
            db,
            tenant_id=tenant_id,
            session=session,
        )
        recent_messages = await self._load_recent_for_session(
            db,
            tenant_id=tenant_id,
            session=session,
            limit=self.recent_message_limit,
        )
        return SessionContextPack(
            snapshot=snapshot,
            recent_messages=recent_messages,
        )

    async def _resolve_position(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        message_id: uuid.UUID,
    ) -> MessagePosition:
        result = await db.execute(_watermark_statement(tenant_id, session_id, message_id))
        message = result.scalar_one_or_none()
        if message is None or message.created_at is None:
            raise SessionContextError(
                "session_context_rebuild_required",
                "watermark message is missing, hidden, or outside the active session",
            )
        return MessagePosition(
            created_at=message.created_at,
            message_id=message.id,
        )

    async def load_messages_after_watermark(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        covered_through_message_id: uuid.UUID | None,
    ) -> tuple[JsonObject, ...]:
        """Read the compact input after resolving the watermark's full position."""
        await self._require_active_session(
            db,
            tenant_id=tenant_id,
            session_id=session_id,
        )
        watermark = None
        if covered_through_message_id is not None:
            watermark = await self._resolve_position(
                db,
                tenant_id=tenant_id,
                session_id=session_id,
                message_id=covered_through_message_id,
            )
        result = await db.execute(_incremental_messages_statement(tenant_id, session_id, watermark))
        return tuple(_message_to_json(message) for message in result.scalars().all())

    async def _validate_watermark_transition(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        expected_message_id: uuid.UUID | None,
        candidate_message_id: uuid.UUID | None,
    ) -> None:
        if expected_message_id is not None and candidate_message_id is None:
            raise SessionContextError(
                "session_context_watermark_regression",
                "Session Context watermark cannot move backward to null",
            )
        expected_position = None
        if expected_message_id is not None:
            expected_position = await self._resolve_position(
                db,
                tenant_id=tenant_id,
                session_id=session_id,
                message_id=expected_message_id,
            )
        candidate_position = None
        if candidate_message_id is not None:
            candidate_position = await self._resolve_position(
                db,
                tenant_id=tenant_id,
                session_id=session_id,
                message_id=candidate_message_id,
            )
        if (
            expected_position is not None
            and candidate_position is not None
            and candidate_position.sort_key < expected_position.sort_key
        ):
            raise SessionContextError(
                "session_context_watermark_regression",
                "Session Context watermark cannot move to an earlier Message Position",
            )

    async def compare_and_swap(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        expected_version: int,
        expected_covered_through_message_id: uuid.UUID | None,
        candidate: SessionContextCandidate,
    ) -> SessionContextSnapshot:
        """Atomically create or replace the current state in the caller transaction."""
        if expected_version < 0:
            raise ValueError("expected_version must not be negative")
        if expected_version == 0 and expected_covered_through_message_id is not None:
            raise SessionContextError(
                "invalid_session_context_expectation",
                "an uninitialized Session Context cannot have an expected watermark",
            )

        session = await self._require_active_session(
            db,
            tenant_id=tenant_id,
            session_id=session_id,
        )
        values = _candidate_values(candidate)
        await self._validate_watermark_transition(
            db,
            tenant_id=tenant_id,
            session_id=session_id,
            expected_message_id=expected_covered_through_message_id,
            candidate_message_id=candidate.covered_through_message_id,
        )

        next_version = expected_version + 1
        if expected_version == 0:
            statement = (
                pg_insert(SessionContextState)
                .values(
                    tenant_id=tenant_id,
                    agent_id=self._expected_agent_id(session),
                    session_id=session_id,
                    version=next_version,
                    **values,
                )
                .on_conflict_do_nothing(index_elements=[SessionContextState.session_id])
                .returning(SessionContextState)
            )
        else:
            expected_agent_id = self._expected_agent_id(session)
            statement = (
                update(SessionContextState)
                .where(
                    SessionContextState.tenant_id == tenant_id,
                    SessionContextState.session_id == session_id,
                    SessionContextState.agent_id.is_not_distinct_from(expected_agent_id),
                    SessionContextState.version == expected_version,
                    SessionContextState.covered_through_message_id.is_not_distinct_from(
                        expected_covered_through_message_id
                    ),
                )
                .values(
                    version=next_version,
                    updated_at=func.now(),
                    **values,
                )
                .returning(SessionContextState)
            )

        result = await db.execute(statement)
        updated = result.scalar_one_or_none()
        if updated is None:
            raise SessionContextConflict()
        return _snapshot_from_row(updated)


__all__ = [
    "MessagePosition",
    "SessionContextCandidate",
    "SessionContextConflict",
    "SessionContextError",
    "SessionContextPack",
    "SessionContextService",
    "SessionContextSnapshot",
]
