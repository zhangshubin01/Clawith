"""Durable Runtime intake for non-Web chat adapters."""

from __future__ import annotations

import uuid

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.agent import Agent
from app.models.agent_run import AgentRun
from app.models.agent_run_event import AgentRunEvent
from app.models.chat_session import ChatSession
from app.models.llm import LLMModel
from app.models.user import User
from app.services.agent_runtime.chat_intake import (
    ChatRuntimeIntake,
    enqueue_chat_runtime,
)


class ChannelChatRuntimeError(RuntimeError):
    """A channel cannot resolve the durable result for its accepted Run."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def channel_message_id(
    agent_id: uuid.UUID,
    source_channel: str,
    external_event_id: str | None,
) -> uuid.UUID:
    """Map a provider event identity to one retry-safe ChatMessage ID."""
    normalized_event = (external_event_id or "").strip()
    if not normalized_event:
        return uuid.uuid4()
    return uuid.uuid5(
        agent_id,
        f"channel-message:{source_channel.strip()}:{normalized_event}",
    )


async def _waiting_resume(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
) -> tuple[uuid.UUID, str] | None:
    """Discover a candidate wait; the Graph still authoritatively validates resume."""
    later = aliased(AgentRunEvent)
    result = await db.execute(
        select(AgentRun.id, AgentRunEvent.payload)
        .join(AgentRunEvent, AgentRunEvent.run_id == AgentRun.id)
        .where(
            AgentRun.tenant_id == tenant_id,
            AgentRun.agent_id == agent_id,
            AgentRun.session_id == session_id,
            AgentRun.origin_user_id == user_id,
            AgentRun.source_type == "chat",
            AgentRun.run_kind == "foreground",
            AgentRun.runtime_type == "langgraph",
            AgentRunEvent.tenant_id == tenant_id,
            AgentRunEvent.event_type == "waiting_started",
            ~exists(
                select(later.id).where(
                    later.tenant_id == tenant_id,
                    later.run_id == AgentRun.id,
                    later.event_type.in_(
                        (
                            "resumed",
                            "run_completed",
                            "run_failed",
                            "run_cancelled",
                        )
                    ),
                    or_(
                        later.created_at > AgentRunEvent.created_at,
                        and_(
                            later.created_at == AgentRunEvent.created_at,
                            later.id > AgentRunEvent.id,
                        ),
                    ),
                )
            ),
        )
        .order_by(AgentRunEvent.created_at.desc(), AgentRunEvent.id.desc())
        .limit(1)
    )
    row = result.first()
    if row is None:
        return None
    run_id, payload = row
    correlation_id = payload.get("correlation_id") if isinstance(payload, dict) else None
    if not isinstance(correlation_id, str) or not correlation_id.strip():
        raise ChannelChatRuntimeError(
            "channel_wait_correlation_missing",
            "Waiting channel Run has no stable resume correlation",
        )
    return run_id, correlation_id.strip()


async def enqueue_channel_chat_runtime(
    db: AsyncSession,
    *,
    agent: Agent,
    user: User,
    session: ChatSession,
    model: LLMModel | None,
    content: str,
    source_channel: str,
    message_id: uuid.UUID,
    channel_delivery_target: dict,
    display_content: str = "",
    file_name: str = "",
) -> ChatRuntimeIntake:
    """Atomically attach a channel message to a new or waiting Chat Run."""
    if agent.tenant_id is None or model is None:
        raise ChannelChatRuntimeError(
            "channel_model_unavailable",
            "Channel Agent has no available model",
        )
    resume = await _waiting_resume(
        db,
        tenant_id=agent.tenant_id,
        agent_id=agent.id,
        session_id=session.id,
        user_id=user.id,
    )
    intake = await enqueue_chat_runtime(
        db,
        agent=agent,
        user=user,
        session=session,
        model=model,
        content=content,
        display_content=display_content,
        file_name=file_name,
        message_id=message_id,
        resume_run_id=resume[0] if resume is not None else None,
        resume_correlation_id=resume[1] if resume is not None else None,
        source_channel=source_channel,
        channel_delivery_target=channel_delivery_target,
    )
    if intake is None:
        raise ChannelChatRuntimeError(
            "channel_runtime_disabled",
            "Unified Agent Runtime is not enabled for this channel",
        )
    return intake


__all__ = [
    "ChannelChatRuntimeError",
    "channel_message_id",
    "enqueue_channel_chat_runtime",
]
