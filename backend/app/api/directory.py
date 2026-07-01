"""Read-only agent directory API."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User
from app.services.agent_directory import DirectoryQueryError, query_agent_directory

router = APIRouter(prefix="/agents/{agent_id}/directory", tags=["agent-directory"])


@router.get("")
async def get_agent_directory(
    agent_id: uuid.UUID,
    member_type: str = "all",
    query: str = "",
    include_uncontactable: bool = False,
    limit: int = 50,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the people and agents the source agent can currently contact."""
    await check_agent_access(db, current_user, agent_id)
    try:
        return await query_agent_directory(
            db,
            source_agent_id=agent_id,
            query=query,
            member_type=member_type,
            include_uncontactable=include_uncontactable,
            limit=limit,
            offset=offset,
            max_limit=100,
        )
    except DirectoryQueryError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}) from exc
