"""Tenant-scoped Direct Chat session management endpoints."""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import String, and_, cast, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.participant import Participant
from app.models.user import Identity, User
from app.services.chat_session_service import (
    create_direct_session,
    soft_delete_direct_session,
)
from app.services.participant_identity import get_or_create_user_participant

router = APIRouter(prefix="/api/agents", tags=["chat-sessions"])


def _can_view_all_agent_chat_sessions(user: User, agent: Agent) -> bool:
    """Admins and the agent creator may inspect other users' direct sessions."""
    return user.role in ("platform_admin", "org_admin", "agent_admin") or str(agent.creator_id) == str(user.id)


def _require_tenant_id(user: User) -> uuid.UUID:
    tenant_id = getattr(user, "tenant_id", None)
    if tenant_id is None:
        raise HTTPException(status_code=403, detail="A tenant is required for chat sessions")
    return tenant_id


def _active_direct_filters(
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
):
    return (
        ChatSession.tenant_id == tenant_id,
        ChatSession.agent_id == agent_id,
        ChatSession.session_type == "direct",
        ChatSession.deleted_at.is_(None),
    )


def _active_agent_session_filters(
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
):
    """Scope the legacy Agent session surface to active associated sessions."""
    return (
        ChatSession.tenant_id == tenant_id,
        ChatSession.deleted_at.is_(None),
        or_(
            ChatSession.agent_id == agent_id,
            and_(
                ChatSession.session_type == "a2a",
                ChatSession.peer_agent_id == agent_id,
            ),
        ),
    )


def _is_a2a_session(session: ChatSession) -> bool:
    return session.session_type == "a2a"


def _is_group_session(session: ChatSession) -> bool:
    return session.session_type == "group"


async def _check_direct_agent_access(
    db: AsyncSession,
    current_user: User,
    agent_id: uuid.UUID,
) -> tuple[Agent, uuid.UUID]:
    tenant_id = _require_tenant_id(current_user)
    agent, _ = await check_agent_access(db, current_user, agent_id)
    if agent.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="No access to this agent")
    return agent, tenant_id


def _authorize_session_owner(current_user: User, agent: Agent, session: ChatSession) -> None:
    if str(session.user_id) != str(current_user.id) and not _can_view_all_agent_chat_sessions(current_user, agent):
        raise HTTPException(status_code=403, detail="Not authorized")


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    agent_id: str | None = None
    user_id: str | None = None
    username: str | None = None
    source_channel: str = "web"
    title: str
    created_at: str
    last_message_at: str | None = None
    message_count: int = 0
    unread_count: int = 0
    is_primary: bool = False
    peer_agent_id: str | None = None
    peer_agent_name: str | None = None
    participant_type: str = "user"
    is_group: bool = False
    group_name: str | None = None


class CreateSessionIn(BaseModel):
    title: str | None = None


class PatchSessionIn(BaseModel):
    title: str


def _session_out(
    session: ChatSession,
    *,
    username: str | None = None,
    message_count: int = 0,
    unread_count: int = 0,
    peer_agent_id: uuid.UUID | None = None,
    peer_agent_name: str | None = None,
    participant_type: str = "user",
    is_group: bool = False,
    group_name: str | None = None,
) -> SessionOut:
    return SessionOut(
        id=str(session.id),
        agent_id=str(session.agent_id) if session.agent_id else None,
        user_id=str(session.user_id) if session.user_id else None,
        username=username,
        source_channel=session.source_channel,
        title=session.title,
        created_at=session.created_at.isoformat(),
        last_message_at=session.last_message_at.isoformat() if session.last_message_at else None,
        message_count=message_count,
        unread_count=unread_count,
        is_primary=bool(session.is_primary),
        peer_agent_id=str(peer_agent_id) if peer_agent_id else None,
        peer_agent_name=peer_agent_name,
        participant_type=participant_type,
        is_group=is_group,
        group_name=group_name,
    )


@router.get("/{agent_id}/sessions")
async def list_sessions(
    agent_id: uuid.UUID,
    scope: Annotated[str, Query(description="'mine' or 'all'")] = "mine",
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List active sessions on the legacy Agent session surface."""
    agent, tenant_id = await _check_direct_agent_access(db, current_user, agent_id)
    if scope not in {"mine", "all"}:
        raise HTTPException(status_code=400, detail="scope must be 'mine' or 'all'")
    if scope == "all" and not _can_view_all_agent_chat_sessions(current_user, agent):
        raise HTTPException(status_code=403, detail="Not authorized to view all sessions")

    if scope == "mine":
        session_filters = _active_direct_filters(tenant_id, agent_id)
        session_query = select(ChatSession).where(
            *session_filters,
            ChatSession.user_id == current_user.id,
        )
    else:
        session_filters = _active_agent_session_filters(tenant_id, agent_id)
        session_query = select(ChatSession).where(*session_filters)
    result = await db.execute(
        session_query.order_by(
            ChatSession.last_message_at.desc().nulls_last(),
            ChatSession.created_at.desc(),
            ChatSession.id.desc(),
        )
    )
    sessions = list(result.scalars().all())
    if not sessions:
        return []

    session_ids = [session.id for session in sessions]
    conversation_ids = [str(session_id) for session_id in session_ids]
    count_result = await db.execute(
        select(ChatMessage.conversation_id, func.count(ChatMessage.id))
        .join(ChatSession, ChatMessage.conversation_id == cast(ChatSession.id, String))
        .where(
            *session_filters,
            ChatSession.id.in_(session_ids),
            ChatMessage.conversation_id.in_(conversation_ids),
        )
        .group_by(ChatMessage.conversation_id)
    )
    message_counts = {row[0]: int(row[1] or 0) for row in count_result.all()}

    unread_result = await db.execute(
        select(ChatSession.id, func.count(ChatMessage.id))
        .join(ChatMessage, ChatMessage.conversation_id == cast(ChatSession.id, String))
        .where(
            *_active_direct_filters(tenant_id, agent_id),
            ChatSession.id.in_(session_ids),
            ChatSession.user_id == current_user.id,
            ChatMessage.role.in_(("assistant", "system", "tool_call")),
            ChatMessage.created_at
            > func.coalesce(
                ChatSession.last_read_at_by_user,
                datetime(1970, 1, 1, tzinfo=UTC),
            ),
        )
        .group_by(ChatSession.id)
    )
    unread_counts = {str(row[0]): int(row[1] or 0) for row in unread_result.all()}

    user_names: dict[str, str] = {}
    agent_names: dict[str, str] = {}
    if scope == "all":
        user_ids = list(
            {
                session.user_id
                for session in sessions
                if session.user_id and not _is_a2a_session(session) and not _is_group_session(session)
            }
        )
        if user_ids:
            user_result = await db.execute(
                select(User.id, func.coalesce(User.display_name, Identity.username))
                .join(Identity, User.identity_id == Identity.id)
                .where(User.tenant_id == tenant_id, User.id.in_(user_ids))
            )
            user_names = {str(row[0]): row[1] or "Unknown" for row in user_result.all()}

        a2a_agent_ids = {
            candidate_id
            for session in sessions
            if _is_a2a_session(session)
            for candidate_id in (session.agent_id, session.peer_agent_id)
            if candidate_id is not None
        }
        if a2a_agent_ids:
            agent_result = await db.execute(
                select(Agent.id, Agent.name).where(
                    Agent.tenant_id == tenant_id,
                    Agent.id.in_(a2a_agent_ids),
                )
            )
            agent_names = {str(row[0]): row[1] or "Agent" for row in agent_result.all()}

    output = []
    for session in sessions:
        count = message_counts.get(str(session.id), 0)
        if count == 0:
            continue
        username = None
        peer_agent_id = None
        peer_agent_name = None
        participant_type = "user"
        is_group = False
        group_name = None
        if scope == "all" and _is_a2a_session(session):
            participant_type = "agent"
            peer_agent_id = session.peer_agent_id if session.agent_id == agent_id else session.agent_id
            peer_agent_name = agent_names.get(str(peer_agent_id), "Agent")
            primary_name = agent_names.get(str(session.agent_id), "Agent")
            stored_peer_name = agent_names.get(str(session.peer_agent_id), "Agent")
            username = f"Agent {primary_name} - {stored_peer_name}"
        elif scope == "all" and _is_group_session(session):
            participant_type = "group"
            is_group = True
            group_name = session.group_name
            username = session.group_name or session.title or "Group Chat"
        elif scope == "all":
            username = user_names.get(str(session.user_id), "Unknown")

        output.append(
            _session_out(
                session,
                username=username,
                message_count=count,
                unread_count=unread_counts.get(str(session.id), 0),
                peer_agent_id=peer_agent_id,
                peer_agent_name=peer_agent_name,
                participant_type=participant_type,
                is_group=is_group,
                group_name=group_name,
            )
        )
    return output


@router.post("/{agent_id}/sessions", status_code=201)
async def create_session(
    agent_id: uuid.UUID,
    body: CreateSessionIn = CreateSessionIn(),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a direct session for the active current-tenant User."""
    _, tenant_id = await _check_direct_agent_access(db, current_user, agent_id)
    user_result = await db.execute(
        select(User).where(
            User.id == current_user.id,
            User.tenant_id == tenant_id,
            User.is_active.is_(True),
        )
    )
    user = user_result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=403, detail="Current user is not active in this tenant")

    participant = await get_or_create_user_participant(
        db,
        user.id,
        user.display_name,
        user.avatar_url,
    )
    session = await create_direct_session(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        user_id=user.id,
        created_by_participant_id=participant.id,
        title=body.title,
    )
    await db.commit()
    await db.refresh(session)
    return _session_out(session)


@router.patch("/{agent_id}/sessions/{session_id}")
async def rename_session(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    body: PatchSessionIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Rename one active direct session."""
    agent, tenant_id = await _check_direct_agent_access(db, current_user, agent_id)
    result = await db.execute(
        select(ChatSession).where(
            *_active_direct_filters(tenant_id, agent_id),
            ChatSession.id == session_id,
        )
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    _authorize_session_owner(current_user, agent, session)

    session.title = body.title
    session.updated_at = datetime.now(UTC)
    await db.commit()
    return {"id": str(session.id), "title": session.title}


@router.delete("/{agent_id}/sessions/{session_id}", status_code=204)
async def delete_session(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete a direct session and cancel only its foreground collaboration."""
    agent, tenant_id = await _check_direct_agent_access(db, current_user, agent_id)
    result = await db.execute(
        select(ChatSession).where(
            *_active_direct_filters(tenant_id, agent_id),
            ChatSession.id == session_id,
        )
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    _authorize_session_owner(current_user, agent, session)
    if session.user_id is None:
        raise HTTPException(status_code=404, detail="Session not found")

    deleted = await soft_delete_direct_session(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        user_id=session.user_id,
        session_id=session_id,
        actor_user_id=current_user.id,
    )
    if deleted is None:
        raise HTTPException(status_code=404, detail="Session not found")
    await db.commit()
    return None


def _parse_message_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    timestamp_text, separator, message_id_text = cursor.rpartition("|")
    try:
        if separator:
            message_id = uuid.UUID(message_id_text)
        else:
            timestamp_text = cursor
            # Legacy timestamp-only cursors may duplicate equal-timestamp messages,
            # but never skip them. New clients should round-trip the emitted cursor.
            message_id = uuid.UUID(int=(1 << 128) - 1)
        created_at = datetime.fromisoformat(timestamp_text.replace("Z", "+00:00"))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="Invalid `before` cursor. Use '<ISO 8601>|<message UUID>'.",
        ) from None
    return created_at, message_id


def _message_cursor(message: ChatMessage) -> str:
    return f"{message.created_at.isoformat()}|{message.id}"


def _base_message_entry(message: ChatMessage) -> dict:
    return {
        "id": str(message.id),
        "role": message.role,
        "content": message.content,
        "created_at": message.created_at.isoformat() if message.created_at else None,
        "cursor": _message_cursor(message),
    }


@router.get("/{agent_id}/sessions/{session_id}/messages")
async def get_session_messages(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    limit: Annotated[int, Query(ge=1, le=500, description="Messages to return")] = 20,
    before: Annotated[
        str | None,
        Query(description="Cursor '<created_at>|<id>' for the first excluded position"),
    ] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return associated session messages by authoritative `(created_at, id)` position."""
    agent, tenant_id = await _check_direct_agent_access(db, current_user, agent_id)
    result = await db.execute(
        select(ChatSession).where(
            *_active_agent_session_filters(tenant_id, agent_id),
            ChatSession.id == session_id,
        )
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    _authorize_session_owner(current_user, agent, session)

    query = (
        select(ChatMessage)
        .join(ChatSession, ChatMessage.conversation_id == cast(ChatSession.id, String))
        .where(
            *_active_agent_session_filters(tenant_id, agent_id),
            ChatSession.id == session_id,
            ChatMessage.conversation_id == str(session_id),
        )
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .limit(limit)
    )
    if before:
        before_created_at, before_id = _parse_message_cursor(before)
        query = query.where(tuple_(ChatMessage.created_at, ChatMessage.id) < tuple_(before_created_at, before_id))
    message_result = await db.execute(query)
    messages = list(reversed(message_result.scalars().all()))

    if session.session_type == "direct" and str(session.user_id) == str(current_user.id):
        read_at = datetime.now(UTC)
        session.last_read_at_by_user = read_at
        session.updated_at = read_at
        await db.commit()

    sender_names: dict[str, str] = {}
    if _is_a2a_session(session):
        participant_ids = {message.participant_id for message in messages if message.participant_id}
        if participant_ids:
            participant_result = await db.execute(
                select(Participant.id, Participant.display_name)
                .join(
                    Agent,
                    and_(
                        Participant.type == "agent",
                        Participant.ref_id == Agent.id,
                    ),
                )
                .where(
                    Participant.id.in_(participant_ids),
                    Agent.tenant_id == tenant_id,
                )
            )
            sender_names = {str(row[0]): row[1] or "Unknown" for row in participant_result.all()}

    output = []
    for message in messages:
        sender_name = sender_names.get(str(message.participant_id)) if message.participant_id else None
        entry = _base_message_entry(message)
        if message.role == "tool_call":
            try:
                data = json.loads(message.content)
            except (TypeError, ValueError):
                data = None
            if isinstance(data, dict):
                entry["content"] = ""
                entry["toolName"] = data.get("name") or data.get("tool_name") or ""
                entry["toolArgs"] = data.get("args") or data.get("arguments")
                entry["toolStatus"] = data.get("status", "done")
                entry["toolResult"] = data.get("result", "")
                entry["toolThinking"] = data.get("reasoning_content", "")
        if getattr(message, "thinking", None):
            entry["thinking"] = message.thinking
        if sender_name:
            entry["sender_name"] = sender_name
        if message.participant_id:
            entry["participant_id"] = str(message.participant_id)
        if _is_a2a_session(session) and message.role == "assistant" and "```tool_code" in (message.content or ""):
            for part in _split_inline_tools(message.content):
                part["id"] = str(message.id)
                part["created_at"] = message.created_at.isoformat() if message.created_at else None
                part["cursor"] = _message_cursor(message)
                if sender_name:
                    part["sender_name"] = sender_name
                if message.participant_id:
                    part["participant_id"] = str(message.participant_id)
                output.append(part)
        else:
            output.append(entry)
    return output


def _split_inline_tools(content: str) -> list[dict]:
    """Legacy parser retained for clients rendering archived inline tool blocks."""
    pattern = re.compile(
        r"```tool_code\s*\n\s*(\w+)\s*\n```"
        r"(?:\s*```json\s*\n(.*?)\n```)?",
        re.DOTALL,
    )
    parts: list[dict] = []
    last_end = 0
    for match in pattern.finditer(content):
        text_before = content[last_end : match.start()].strip()
        if text_before:
            parts.append({"role": "assistant", "content": text_before})
        args_str = match.group(2)
        tool_args = None
        if args_str:
            try:
                tool_args = json.loads(args_str.strip())
            except (TypeError, ValueError):
                tool_args = {"raw": args_str.strip()}
        parts.append(
            {
                "role": "tool_call",
                "content": "",
                "toolName": match.group(1),
                "toolArgs": tool_args,
                "toolStatus": "done",
                "toolResult": "",
            }
        )
        last_end = match.end()
    trailing = content[last_end:].strip()
    if trailing:
        parts.append({"role": "assistant", "content": trailing})
    return parts or [{"role": "assistant", "content": content}]
