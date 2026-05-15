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
        """更新 IDE 插件会话上下文信息。

        #135 修复：添加 info 级别日志追踪，记录 session_id 和变更的字段名；
        当 client_type 从 web 切换为 ide_plugin 时明确记录。
        """
        try:
            sid = UUID(session_id)
        except (ValueError, TypeError):
            logger.warning("[SessionCtx] 无效的 session_id: {}", session_id)
            return

        result = await db.execute(select(ChatSession).where(ChatSession.id == sid))
        session = result.scalar_one_or_none()

        if not session:
            logger.debug("[SessionCtx] 未找到会话: session_id={}", session_id)
            return

        # 记录本次变更的字段名
        changed_fields: list[str] = []

        if project_path is not None:
            if session.project_path != project_path:
                changed_fields.append("project_path")
            session.project_path = project_path
        if current_file is not None:
            if session.current_file != current_file:
                changed_fields.append("current_file")
            session.current_file = current_file
        if open_files is not None:
            changed_fields.append("open_files")
            session.open_files = open_files

        # 标记为 IDE 插件客户端（若尚未设置）
        previous_client_type = session.client_type
        if session.client_type != "ide_plugin":
            session.client_type = "ide_plugin"
            changed_fields.append("client_type")
            # 明确记录 client_type 从非 IDE 切换为 IDE 插件的过程
            logger.info(
                "[SessionCtx] client_type 变更: session_id={} {} → ide_plugin",
                session_id, previous_client_type or "web",
            )

        if changed_fields:
            logger.info(
                "[SessionCtx] 更新 IDE 上下文: session_id={} fields=[{}]",
                session_id, ", ".join(changed_fields),
            )
        else:
            logger.debug("[SessionCtx] 无变更: session_id={}", session_id)

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
