"""Transaction-scoped Web Chat intake for the durable Agent Runtime."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models.agent import Agent
from app.models.agent_run import AgentRun
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.llm import LLMModel
from app.models.user import User
from app.services.agent_runtime.adapter import TransactionalAgentRuntimeAdapter
from app.services.agent_runtime.config import decide_runtime_v2
from app.services.agent_runtime.contracts import (
    ResumeRunCommand,
    RunHandle,
    StartRunCommand,
)
from app.services.participant_identity import get_or_create_user_participant


_ACTIVE_AGENT_STATUSES = frozenset({"creating", "running", "idle"})


class ChatRuntimeIntakeError(RuntimeError):
    """A Web Chat input selected for Runtime v2 cannot be accepted safely."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ChatRuntimeIntake:
    """Stable identities accepted in one caller-owned transaction."""

    handle: RunHandle
    message_id: uuid.UUID
    resumed: bool


def stored_user_content(
    content: str,
    *,
    display_content: str = "",
    file_name: str = "",
) -> str:
    """Preserve executable image input while keeping ordinary display text concise."""
    has_image_marker = "[image_data:" in content
    if has_image_marker:
        return f"[file:{file_name}]\n{content}" if file_name else content

    saved = display_content or content
    if file_name:
        saved = f"[file:{file_name}]\n{saved}"
    return saved


def _chat_goal(content: str, display_content: str, file_name: str) -> str:
    visible = (display_content or content).strip()
    if "[image_data:" in visible:
        visible = "Analyze the attached image and respond to the user."
    if file_name:
        visible = f"{visible}\nAttached file: {file_name}" if visible else f"Handle attached file: {file_name}"
    return visible or "Respond to the user's chat message."


def _validate_scope(
    *,
    agent: Agent,
    user: User,
    session: ChatSession,
    model: LLMModel,
) -> uuid.UUID:
    tenant_id = agent.tenant_id
    if tenant_id is None:
        raise ChatRuntimeIntakeError(
            "agent_tenant_missing",
            "Runtime Chat Agent has no tenant",
        )
    if user.tenant_id != tenant_id:
        raise ChatRuntimeIntakeError(
            "chat_tenant_mismatch",
            "Chat user and Agent do not belong to the same tenant",
        )
    if (
        session.tenant_id != tenant_id
        or session.session_type != "direct"
        or session.agent_id != agent.id
        or session.user_id != user.id
        or session.source_channel != "web"
        or session.deleted_at is not None
    ):
        raise ChatRuntimeIntakeError(
            "chat_session_scope_mismatch",
            "Web Chat session is not an active direct session for this user and Agent",
        )
    if agent.is_expired or agent.status not in _ACTIVE_AGENT_STATUSES:
        raise ChatRuntimeIntakeError(
            "agent_unavailable",
            "Runtime Chat Agent is unavailable",
        )
    if not model.enabled or model.tenant_id not in {None, tenant_id}:
        raise ChatRuntimeIntakeError(
            "model_unavailable",
            "Selected Chat model is disabled or outside the tenant scope",
        )
    return tenant_id


async def _require_resume_run(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
) -> AgentRun:
    result = await db.execute(
        select(AgentRun).where(
            AgentRun.tenant_id == tenant_id,
            AgentRun.id == run_id,
        )
    )
    run = result.scalar_one_or_none()
    if run is None:
        raise ChatRuntimeIntakeError(
            "run_not_found",
            "Requested waiting Chat Run does not exist in this tenant",
        )
    if (
        run.agent_id != agent_id
        or run.session_id != session_id
        or run.origin_user_id != user_id
        or run.source_type != "chat"
        or run.run_kind != "foreground"
        or run.runtime_type != "langgraph"
        or run.runtime_thread_id != str(run.id)
    ):
        raise ChatRuntimeIntakeError(
            "chat_resume_scope_mismatch",
            "Requested Run is not a resumable Web Chat Run for this session",
        )
    return run


async def _persist_user_message(
    db: AsyncSession,
    *,
    message_id: uuid.UUID,
    agent: Agent,
    user: User,
    session: ChatSession,
    content: str,
) -> None:
    participant = await get_or_create_user_participant(
        db,
        user.id,
        user.display_name,
        user.avatar_url,
    )
    existing = await db.get(ChatMessage, message_id)
    if existing is None:
        db.add(
            ChatMessage(
                id=message_id,
                agent_id=agent.id,
                user_id=user.id,
                role="user",
                content=content,
                conversation_id=str(session.id),
                participant_id=participant.id,
                mentions=[],
            )
        )
    elif (
        existing.agent_id != agent.id
        or existing.user_id != user.id
        or existing.role != "user"
        or existing.content != content
        or existing.conversation_id != str(session.id)
        or existing.participant_id != participant.id
    ):
        raise ChatRuntimeIntakeError(
            "chat_message_idempotency_mismatch",
            "Chat message ID already exists with different immutable input",
        )

    now = datetime.now(UTC)
    session.last_message_at = now
    if session.title.startswith("Session "):
        clean_title = content.replace("[图片] ", "📷 ").replace("[image_data:", "").strip()
        session.title = clean_title[:40] or "New chat"
    await db.flush()


async def enqueue_chat_runtime(
    db: AsyncSession,
    *,
    agent: Agent,
    user: User,
    session: ChatSession,
    model: LLMModel,
    content: str,
    display_content: str = "",
    file_name: str = "",
    message_id: uuid.UUID | None = None,
    resume_run_id: uuid.UUID | None = None,
    resume_correlation_id: str | None = None,
    settings_override: Settings | None = None,
) -> ChatRuntimeIntake | None:
    """Persist one Web Chat message and its start/resume Command atomically.

    Returning ``None`` means the rollout gate selected the untouched legacy
    path. This function never commits; the WebSocket ingress owns the boundary.
    """
    runtime_settings = settings_override or get_settings()
    decision = decide_runtime_v2(
        agent_id=agent.id,
        source_type="chat",
        settings=runtime_settings,
    )
    if not decision.use_v2:
        return None

    if not isinstance(content, str) or not content.strip():
        raise ChatRuntimeIntakeError(
            "invalid_chat_input",
            "Runtime Chat content must not be blank",
        )
    tenant_id = _validate_scope(
        agent=agent,
        user=user,
        session=session,
        model=model,
    )
    if (resume_run_id is None) != (resume_correlation_id is None):
        raise ChatRuntimeIntakeError(
            "incomplete_chat_resume",
            "Chat resume requires both run_id and correlation_id",
        )
    if resume_correlation_id is not None and not resume_correlation_id.strip():
        raise ChatRuntimeIntakeError(
            "invalid_chat_resume_correlation",
            "Chat resume correlation_id must not be blank",
        )

    resolved_message_id = message_id or uuid.uuid4()
    saved_content = stored_user_content(
        content,
        display_content=display_content,
        file_name=file_name,
    )
    await _persist_user_message(
        db,
        message_id=resolved_message_id,
        agent=agent,
        user=user,
        session=session,
        content=saved_content,
    )

    adapter = TransactionalAgentRuntimeAdapter(db, settings=runtime_settings)
    if resume_run_id is not None:
        await _require_resume_run(
            db,
            tenant_id=tenant_id,
            run_id=resume_run_id,
            agent_id=agent.id,
            session_id=session.id,
            user_id=user.id,
        )
        correlation_id = resume_correlation_id.strip()  # type: ignore[union-attr]
        handle = await adapter.resume_run(
            ResumeRunCommand(
                tenant_id=tenant_id,
                run_id=resume_run_id,
                idempotency_key=f"resume:chat:{resolved_message_id}",
                payload={
                    "resume_type": "user_input",
                    "correlation_id": correlation_id,
                    "payload": {
                        "message_id": str(resolved_message_id),
                        "content": content,
                    },
                },
                actor_user_id=user.id,
            )
        )
        return ChatRuntimeIntake(
            handle=handle,
            message_id=resolved_message_id,
            resumed=True,
        )

    source_execution_id = f"chat:{resolved_message_id}"
    handle = await adapter.start_run(
        StartRunCommand(
            tenant_id=tenant_id,
            agent_id=agent.id,
            session_id=session.id,
            source_type="chat",
            source_id=str(resolved_message_id),
            source_execution_id=source_execution_id,
            goal=_chat_goal(content, display_content, file_name),
            run_kind="foreground",
            model_id=model.id,
            delivery_status="pending",
            delivery_target={
                "kind": "direct",
                "session_id": str(session.id),
                "user_id": str(user.id),
            },
            idempotency_key=f"start:{source_execution_id}",
            payload={
                "message_id": str(resolved_message_id),
                "source_channel": "web",
                "user_id": str(user.id),
            },
            origin_user_id=user.id,
            actor_user_id=user.id,
        )
    )
    return ChatRuntimeIntake(
        handle=handle,
        message_id=resolved_message_id,
        resumed=False,
    )


__all__ = [
    "ChatRuntimeIntake",
    "ChatRuntimeIntakeError",
    "enqueue_chat_runtime",
    "stored_user_content",
]
