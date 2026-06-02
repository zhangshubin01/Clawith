"""ACP 会话管理 — ChatSession 表 CRUD (source_channel="acp")。

复用已有 ChatSession/ChatMessage 模型，通过 source_channel 区分来源。
实际模型字段: ChatSession.project_path (cwd), ChatMessage.conversation_id (session)
"""

import uuid
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.chat_session import ChatSession
from app.models.audit import ChatMessage


class AcpSessionManager:
    """ACP 会话管理器 — 基于 ChatSession/ChatMessage 表持久化。"""

    async def create(self, user_id: str, agent_id: str, cwd: str = "") -> str:
        """创建新会话 — source_channel="acp", project_path=cwd。"""
        session_id = str(uuid.uuid4())

        async with async_session() as db:
            session = ChatSession(
                id=session_id,
                user_id=user_id,
                agent_id=agent_id,
                title="ACP 会话",
                project_path=cwd,           # ChatSession.cwd → project_path
                source_channel="acp",
                client_type="ide_acp",
                created_at=datetime.now(timezone.utc),
            )
            db.add(session)
            await db.commit()

        logger.info(f"[ACP-Session] 创建: id={session_id} agent={agent_id}")
        return session_id

    async def load(self, session_id: str, user_id: str) -> dict | None:
        """加载已有会话 — 返回 agent_id + cwd + 历史消息。"""
        async with async_session() as db:
            result = await db.execute(
                select(ChatSession)
                .where(ChatSession.id == session_id)
                .limit(1)
            )
            session: ChatSession | None = result.scalar_one_or_none()
            if not session:
                logger.warning(f"[ACP-Session] 会话不存在: {session_id}")
                return None

            # 加载最近 20 条历史消息
            history_result = await db.execute(
                select(ChatMessage)
                .where(ChatMessage.conversation_id == session_id)  # ChatMessage.session_id → conversation_id
                .order_by(ChatMessage.created_at.asc())
                .limit(20)
            )
            messages = history_result.scalars().all()
            history = [
                {"role": msg.role, "content": msg.content}
                for msg in messages
            ]

            logger.info(f"[ACP-Session] 加载: id={session_id} history={len(history)}条")
            return {
                "agent_id": str(session.agent_id),
                "cwd": session.project_path or "",  # ChatSession.project_path
                "history": history,
            }
