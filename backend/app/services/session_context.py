"""Services for managing IDEA plugin session context."""

from typing import Optional, Dict, Any
from uuid import UUID
from loguru import logger

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_session import ChatSession


class SessionContextManager:
    """Manages IDEA plugin session context information."""

    async def update_ide_context(
        self,
        db: AsyncSession,
        session_id: str,
        project_path: Optional[str] = None,
        current_file: Optional[str] = None,
        open_files: Optional[list[str]] = None,
    ):
        """Update IDEA plugin session context."""
        try:
            sid = UUID(session_id)
        except (ValueError, TypeError):
            return

        result = await db.execute(select(ChatSession).where(ChatSession.id == sid))
        session = result.scalar_one_or_none()
        
        if not session:
            return

        # Update fields if provided
        if project_path is not None:
            session.project_path = project_path
        if current_file is not None:
            session.current_file = current_file
        if open_files is not None:
            session.open_files = open_files
        
        # Mark as IDE plugin client if not already set
        if session.client_type != "ide_plugin":
            session.client_type = "ide_plugin"

        await db.commit()

    async def get_ide_context(self, db: AsyncSession, session_id: str) -> Dict[str, Any]:
        """Get session context for building prompts."""
        try:
            sid = UUID(session_id)
        except (ValueError, TypeError):
            return {}

        result = await db.execute(select(ChatSession).where(ChatSession.id == sid))
        session = result.scalar_one_or_none()
        
        if not session:
            return {}

        return {
            "client_type": session.client_type,
            "project_path": session.project_path,
            "current_file": session.current_file,
            "open_files": session.open_files,
        }

    async def get_latest_ide_context_by_agent(self, db: AsyncSession, agent_id: UUID) -> Dict[str, Any]:
        """Get the latest IDE context for an agent's most recent session."""
        result = await db.execute(
            select(ChatSession)
            .where(ChatSession.agent_id == agent_id)
            .order_by(ChatSession.created_at.desc())
            .limit(1)
        )
        session = result.scalar_one_or_none()
        
        if not session:
            return {}

        return {
            "project_path": session.project_path,
            "current_file": session.current_file,
            "open_files": session.open_files,
        }
