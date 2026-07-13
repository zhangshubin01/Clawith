"""Tenant-scoped HTTP boundary for native group chats."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.group import GroupMember
from app.models.participant import Participant
from app.models.user import User
from app.services import group_chat_service
from app.services.group_chat_service import GroupChatServiceError
from app.services.participant_identity import get_or_create_user_participant


router = APIRouter(prefix="/api/groups", tags=["groups"])


class CreateGroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None


class PatchGroupIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None


class GroupOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    description: str | None = None
    created_by_participant_id: uuid.UUID
    created_at: datetime
    updated_at: datetime


class InviteGroupMemberIn(BaseModel):
    participant_id: uuid.UUID


class GroupMemberOut(BaseModel):
    id: uuid.UUID
    participant_id: uuid.UUID
    participant_type: str
    participant_ref_id: uuid.UUID
    display_name: str
    avatar_url: str | None = None
    role: str
    role_description: str | None = None
    title: str | None = None
    joined_at: datetime


class CreateGroupSessionIn(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)


class PatchGroupSessionIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class GroupSessionOut(BaseModel):
    id: uuid.UUID
    group_id: uuid.UUID
    title: str
    is_primary: bool
    unread_count: int = 0
    created_by_participant_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime | None = None


class MarkGroupSessionReadIn(BaseModel):
    message_id: uuid.UUID


class GroupReadStateOut(BaseModel):
    session_id: uuid.UUID
    last_read_message_id: uuid.UUID
    advanced: bool


_NOT_FOUND_CODES = {
    "group_not_found",
    "group_member_not_found",
    "group_session_not_found",
    "group_message_not_found",
}
_FORBIDDEN_CODES = {
    "group_access_denied",
    "group_human_member_required",
    "group_manager_required",
    "group_creator_invalid",
}
_CONFLICT_CODES = {
    "group_member_already_active",
    "group_last_manager_required",
}


def _tenant_id(current_user: User) -> uuid.UUID:
    tenant_id = current_user.tenant_id
    if tenant_id is None:
        raise HTTPException(status_code=403, detail="A tenant is required for groups")
    return tenant_id


async def _current_participant(db: AsyncSession, current_user: User) -> Participant:
    if not current_user.is_active:
        raise HTTPException(status_code=403, detail="Current user is not active")
    return await get_or_create_user_participant(
        db,
        current_user.id,
        current_user.display_name,
        current_user.avatar_url,
    )


def _translate_domain_error(exc: GroupChatServiceError) -> HTTPException:
    if exc.code in _NOT_FOUND_CODES:
        status_code = status.HTTP_404_NOT_FOUND
    elif exc.code in _FORBIDDEN_CODES:
        status_code = status.HTTP_403_FORBIDDEN
    elif exc.code in _CONFLICT_CODES:
        status_code = status.HTTP_409_CONFLICT
    else:
        status_code = status.HTTP_400_BAD_REQUEST
    return HTTPException(
        status_code=status_code,
        detail={"code": exc.code, "message": str(exc)},
    )


def _stage_audit(
    db: AsyncSession,
    *,
    current_user: User,
    action: str,
    tenant_id: uuid.UUID,
    group_id: uuid.UUID,
    details: dict | None = None,
) -> None:
    db.add(
        AuditLog(
            user_id=current_user.id,
            action=action,
            details={
                "tenant_id": str(tenant_id),
                "group_id": str(group_id),
                **(details or {}),
            },
        )
    )


def _group_session_out(session, *, unread_count: int = 0) -> GroupSessionOut:
    return GroupSessionOut(
        id=session.id,
        group_id=session.group_id,
        title=session.title,
        is_primary=bool(session.is_primary),
        unread_count=unread_count,
        created_by_participant_id=session.created_by_participant_id,
        created_at=session.created_at,
        updated_at=session.updated_at,
        last_message_at=session.last_message_at,
    )


async def _member_outputs(
    db: AsyncSession,
    memberships: list[GroupMember],
) -> list[GroupMemberOut]:
    participant_ids = [membership.participant_id for membership in memberships]
    if not participant_ids:
        return []
    participant_result = await db.execute(
        select(Participant).where(Participant.id.in_(participant_ids))
    )
    participants = {participant.id: participant for participant in participant_result.scalars().all()}

    agent_ref_ids = {
        participant.ref_id for participant in participants.values() if participant.type == "agent"
    }
    user_ref_ids = {
        participant.ref_id for participant in participants.values() if participant.type == "user"
    }
    agents: dict[uuid.UUID, Agent] = {}
    users: dict[uuid.UUID, User] = {}
    if agent_ref_ids:
        agent_result = await db.execute(select(Agent).where(Agent.id.in_(agent_ref_ids)))
        agents = {agent.id: agent for agent in agent_result.scalars().all()}
    if user_ref_ids:
        user_result = await db.execute(select(User).where(User.id.in_(user_ref_ids)))
        users = {user.id: user for user in user_result.scalars().all()}

    output: list[GroupMemberOut] = []
    for membership in memberships:
        participant = participants.get(membership.participant_id)
        if participant is None:
            continue
        agent = agents.get(participant.ref_id) if participant.type == "agent" else None
        user = users.get(participant.ref_id) if participant.type == "user" else None
        output.append(
            GroupMemberOut(
                id=membership.id,
                participant_id=participant.id,
                participant_type=participant.type,
                participant_ref_id=participant.ref_id,
                display_name=participant.display_name,
                avatar_url=participant.avatar_url,
                role=membership.role,
                role_description=agent.role_description if agent is not None else None,
                title=user.title if user is not None else None,
                joined_at=membership.joined_at,
            )
        )
    return output


@router.post("", response_model=GroupOut, status_code=status.HTTP_201_CREATED)
async def create_group(
    body: CreateGroupIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = _tenant_id(current_user)
    participant = await _current_participant(db, current_user)
    try:
        group = await group_chat_service.create_group(
            db,
            tenant_id=tenant_id,
            creator_participant_id=participant.id,
            name=body.name,
            description=body.description,
        )
    except GroupChatServiceError as exc:
        raise _translate_domain_error(exc) from exc
    _stage_audit(
        db,
        current_user=current_user,
        action="group:create",
        tenant_id=tenant_id,
        group_id=group.id,
    )
    return group


@router.get("", response_model=list[GroupOut])
async def list_groups(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = _tenant_id(current_user)
    participant = await _current_participant(db, current_user)
    return await group_chat_service.list_groups(
        db,
        tenant_id=tenant_id,
        participant_id=participant.id,
    )


@router.get("/{group_id}", response_model=GroupOut)
async def get_group(
    group_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = _tenant_id(current_user)
    participant = await _current_participant(db, current_user)
    try:
        return await group_chat_service.get_group(
            db,
            tenant_id=tenant_id,
            group_id=group_id,
            participant_id=participant.id,
        )
    except GroupChatServiceError as exc:
        raise _translate_domain_error(exc) from exc


@router.patch("/{group_id}", response_model=GroupOut)
async def patch_group(
    group_id: uuid.UUID,
    body: PatchGroupIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if "name" not in body.model_fields_set and "description" not in body.model_fields_set:
        raise HTTPException(status_code=400, detail="At least one field must be supplied")
    tenant_id = _tenant_id(current_user)
    participant = await _current_participant(db, current_user)
    try:
        group = await group_chat_service.update_group(
            db,
            tenant_id=tenant_id,
            group_id=group_id,
            actor_participant_id=participant.id,
            name=body.name if "name" in body.model_fields_set else None,
            description=body.description,
            update_description="description" in body.model_fields_set,
        )
    except GroupChatServiceError as exc:
        raise _translate_domain_error(exc) from exc
    _stage_audit(
        db,
        current_user=current_user,
        action="group:update",
        tenant_id=tenant_id,
        group_id=group_id,
        details={"fields": sorted(body.model_fields_set)},
    )
    return group


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(
    group_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = _tenant_id(current_user)
    participant = await _current_participant(db, current_user)
    try:
        await group_chat_service.soft_delete_group(
            db,
            tenant_id=tenant_id,
            group_id=group_id,
            actor_participant_id=participant.id,
        )
    except GroupChatServiceError as exc:
        raise _translate_domain_error(exc) from exc
    _stage_audit(
        db,
        current_user=current_user,
        action="group:delete",
        tenant_id=tenant_id,
        group_id=group_id,
    )
    return None


@router.get("/{group_id}/members", response_model=list[GroupMemberOut])
async def list_group_members(
    group_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = _tenant_id(current_user)
    participant = await _current_participant(db, current_user)
    try:
        memberships = await group_chat_service.list_group_members(
            db,
            tenant_id=tenant_id,
            group_id=group_id,
            actor_participant_id=participant.id,
        )
    except GroupChatServiceError as exc:
        raise _translate_domain_error(exc) from exc
    return await _member_outputs(db, memberships)


@router.post(
    "/{group_id}/members",
    response_model=GroupMemberOut,
    status_code=status.HTTP_201_CREATED,
)
async def invite_group_member(
    group_id: uuid.UUID,
    body: InviteGroupMemberIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = _tenant_id(current_user)
    participant = await _current_participant(db, current_user)
    try:
        membership = await group_chat_service.invite_group_member(
            db,
            tenant_id=tenant_id,
            group_id=group_id,
            actor_participant_id=participant.id,
            participant_id=body.participant_id,
        )
        outputs = await _member_outputs(db, [membership])
    except GroupChatServiceError as exc:
        raise _translate_domain_error(exc) from exc
    if not outputs:
        raise HTTPException(status_code=409, detail="Participant identity is not available")
    _stage_audit(
        db,
        current_user=current_user,
        action="group:member_invite",
        tenant_id=tenant_id,
        group_id=group_id,
        details={"participant_id": str(body.participant_id)},
    )
    return outputs[0]


@router.delete("/{group_id}/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_group_member(
    group_id: uuid.UUID,
    member_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = _tenant_id(current_user)
    participant = await _current_participant(db, current_user)
    try:
        removed = await group_chat_service.remove_group_member(
            db,
            tenant_id=tenant_id,
            group_id=group_id,
            actor_participant_id=participant.id,
            member_id=member_id,
        )
    except GroupChatServiceError as exc:
        raise _translate_domain_error(exc) from exc
    _stage_audit(
        db,
        current_user=current_user,
        action="group:member_remove",
        tenant_id=tenant_id,
        group_id=group_id,
        details={"member_id": str(member_id), "participant_id": str(removed.participant_id)},
    )
    return None


@router.get("/{group_id}/sessions", response_model=list[GroupSessionOut])
async def list_group_sessions(
    group_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = _tenant_id(current_user)
    participant = await _current_participant(db, current_user)
    try:
        sessions = await group_chat_service.list_group_sessions(
            db,
            tenant_id=tenant_id,
            group_id=group_id,
            actor_participant_id=participant.id,
        )
        output = []
        for session in sessions:
            unread_count = await group_chat_service.get_group_session_unread_count(
                db,
                tenant_id=tenant_id,
                group_id=group_id,
                session_id=session.id,
                participant_id=participant.id,
            )
            output.append(_group_session_out(session, unread_count=unread_count))
        return output
    except GroupChatServiceError as exc:
        raise _translate_domain_error(exc) from exc


@router.post(
    "/{group_id}/sessions",
    response_model=GroupSessionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_group_session(
    group_id: uuid.UUID,
    body: CreateGroupSessionIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = _tenant_id(current_user)
    participant = await _current_participant(db, current_user)
    try:
        session = await group_chat_service.create_group_session(
            db,
            tenant_id=tenant_id,
            group_id=group_id,
            actor_participant_id=participant.id,
            title=body.title,
        )
    except GroupChatServiceError as exc:
        raise _translate_domain_error(exc) from exc
    _stage_audit(
        db,
        current_user=current_user,
        action="group:session_create",
        tenant_id=tenant_id,
        group_id=group_id,
        details={"session_id": str(session.id)},
    )
    return _group_session_out(session)


@router.patch("/{group_id}/sessions/{session_id}", response_model=GroupSessionOut)
async def patch_group_session(
    group_id: uuid.UUID,
    session_id: uuid.UUID,
    body: PatchGroupSessionIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = _tenant_id(current_user)
    participant = await _current_participant(db, current_user)
    try:
        session = await group_chat_service.update_group_session(
            db,
            tenant_id=tenant_id,
            group_id=group_id,
            session_id=session_id,
            actor_participant_id=participant.id,
            title=body.title,
        )
    except GroupChatServiceError as exc:
        raise _translate_domain_error(exc) from exc
    _stage_audit(
        db,
        current_user=current_user,
        action="group:session_update",
        tenant_id=tenant_id,
        group_id=group_id,
        details={"session_id": str(session_id)},
    )
    return _group_session_out(session)


@router.delete(
    "/{group_id}/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_group_session(
    group_id: uuid.UUID,
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = _tenant_id(current_user)
    participant = await _current_participant(db, current_user)
    try:
        deletion = await group_chat_service.soft_delete_group_session(
            db,
            tenant_id=tenant_id,
            group_id=group_id,
            session_id=session_id,
            actor_participant_id=participant.id,
        )
    except GroupChatServiceError as exc:
        raise _translate_domain_error(exc) from exc
    _stage_audit(
        db,
        current_user=current_user,
        action="group:session_delete",
        tenant_id=tenant_id,
        group_id=group_id,
        details={
            "session_id": str(session_id),
            "replacement_session_id": (
                str(deletion.replacement.id) if deletion.replacement is not None else None
            ),
            "cancelled_run_count": len(deletion.cancelled_run_ids),
        },
    )
    return None


@router.post(
    "/{group_id}/sessions/{session_id}/read",
    response_model=GroupReadStateOut,
)
async def mark_group_session_read(
    group_id: uuid.UUID,
    session_id: uuid.UUID,
    body: MarkGroupSessionReadIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = _tenant_id(current_user)
    participant = await _current_participant(db, current_user)
    try:
        result = await group_chat_service.mark_group_session_read(
            db,
            tenant_id=tenant_id,
            group_id=group_id,
            session_id=session_id,
            participant_id=participant.id,
            message_id=body.message_id,
        )
    except GroupChatServiceError as exc:
        raise _translate_domain_error(exc) from exc
    return GroupReadStateOut(
        session_id=result.session_id,
        last_read_message_id=result.last_read_message_id,
        advanced=result.advanced,
    )
