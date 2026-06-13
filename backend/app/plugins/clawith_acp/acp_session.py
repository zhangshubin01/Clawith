"""ACP 会话管理 — ChatSession 表 CRUD (source_channel="acp")。

复用已有 ChatSession/ChatMessage 模型，通过 source_channel 区分来源。
实际模型字段: ChatSession.project_path (cwd), ChatMessage.conversation_id (session)
"""

import os
import time
import uuid
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.chat_session import ChatSession
from app.models.audit import ChatMessage

# 多轮对话上下文上限（user+assistant 各算一条）
ACP_HISTORY_LIMIT = int(os.getenv("ACP_HISTORY_LIMIT", "40"))


class AcpSessionManager:
    """ACP 会话管理器 — 基于 ChatSession/ChatMessage 表持久化。"""

    def __init__(self):
        self._history_cache: dict[str, tuple[float, list[dict]]] = {}
        # TTL 30s: ACP 多轮对话场景, persist_turn 后主动更新缓存, 延长 TTL 覆盖连续对话间隔
        self._CACHE_TTL = 30.0

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

        logger.info(f"[ACP] 创建: id={session_id} agent={agent_id}")
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
                logger.warning(f"[ACP] 会话不存在: {session_id}")
                return None
            if str(session.user_id) != str(user_id):
                logger.warning(f"[ACP] 会话归属不匹配: {session_id}")
                return None

            # 取最近 N 条再按时间正序，供 session/load 展示。
            # 增加 user_id 过滤: 与 load_history_for_llm 保持一致, 纵深防御 + 索引覆盖。
            history_result = await db.execute(
                select(ChatMessage)
                .where(ChatMessage.conversation_id == session_id)
                .where(ChatMessage.user_id == uuid.UUID(user_id))
                .where(ChatMessage.role.in_(("user", "assistant")))
                .order_by(ChatMessage.created_at.desc())
                .limit(ACP_HISTORY_LIMIT)
            )
            messages = list(reversed(history_result.scalars().all()))
            history = [
                {"role": msg.role, "content": msg.content}
                for msg in messages
            ]

            logger.info(f"[ACP] 加载: id={session_id} history={len(history)}条")
            return {
                "agent_id": str(session.agent_id),
                "cwd": session.project_path or "",  # ChatSession.project_path
                "history": history,
            }

    async def update_agent(self, session_id: str, agent_id: str) -> None:
        """更新会话绑定的智能体（用户切换智能体后重连）。"""
        async with async_session() as db:
            result = await db.execute(
                select(ChatSession).where(ChatSession.id == session_id).limit(1)
            )
            session: ChatSession | None = result.scalar_one_or_none()
            if session:
                session.agent_id = agent_id
                await db.commit()
                logger.info(f"[ACP] 更新会话智能体: id={session_id} → agent={agent_id}")

    async def load_history_for_llm(
        self,
        session_id: str,
        user_id: str,
        *,
        limit: int = ACP_HISTORY_LIMIT,
    ) -> list[dict]:
        """加载最近对话历史，供 LLM 多轮上下文（仅 user/assistant）。"""
        cached = self._history_cache.get(session_id)
        if cached and (time.monotonic() - cached[0]) < self._CACHE_TTL:
            return cached[1]

        try:
            sid_uuid = uuid.UUID(session_id)
            uid_uuid = uuid.UUID(user_id)
        except ValueError:
            return []

        async with async_session() as db:
            sr = await db.execute(
                select(ChatSession).where(ChatSession.id == sid_uuid).limit(1)
            )
            sess = sr.scalar_one_or_none()
            if not sess or str(sess.user_id) != str(user_id):
                return []

            hr = await db.execute(
                select(ChatMessage)
                .where(ChatMessage.conversation_id == session_id)
                .where(ChatMessage.user_id == uid_uuid)
                .where(ChatMessage.role.in_(("user", "assistant")))
                .order_by(ChatMessage.created_at.desc())
                .limit(limit)
            )
            rows = list(reversed(hr.scalars().all()))
            result = [{"role": m.role, "content": m.content} for m in rows]
            self._history_cache[session_id] = (time.monotonic(), result)
            return result

    async def persist_turn(
        self,
        session_id: str,
        user_id: str,
        agent_id: str,
        user_text: str,
        assistant_text: str,
    ) -> None:
        """持久化一轮 user/assistant 对话，下一轮 prompt 可续接上下文。"""
        if not user_text and not assistant_text:
            return
        try:
            sid_uuid = uuid.UUID(session_id)
            uid_uuid = uuid.UUID(user_id)
            aid_uuid = uuid.UUID(agent_id)
        except ValueError:
            logger.warning(f"[ACP] persist 跳过非法 UUID: session={session_id}")
            return

        now = datetime.now(timezone.utc)
        try:
            async with async_session() as db:
                async with db.begin():
                    sr = await db.execute(
                        select(ChatSession).where(ChatSession.id == sid_uuid).limit(1)
                    )
                    sess = sr.scalar_one_or_none()
                    if not sess or str(sess.user_id) != str(user_id):
                        logger.warning(f"[ACP] persist 会话不存在或无权限: {session_id}")
                        return

                    sess.last_message_at = now
                    if str(sess.agent_id) != agent_id:
                        sess.agent_id = aid_uuid

                    if user_text:
                        db.add(
                            ChatMessage(
                                agent_id=aid_uuid,
                                user_id=uid_uuid,
                                role="user",
                                content=user_text,
                                conversation_id=session_id,
                                created_at=now - timedelta(seconds=1),
                            )
                        )
                    if assistant_text:
                        db.add(
                            ChatMessage(
                                agent_id=aid_uuid,
                                user_id=uid_uuid,
                                role="assistant",
                                content=assistant_text,
                                conversation_id=session_id,
                                created_at=now,
                            )
                        )
                    sess.message_count = (sess.message_count or 0) + (
                        (1 if user_text else 0) + (1 if assistant_text else 0)
                    )
            logger.info(
                f"[ACP] persist: session={session_id} "
                f"user_len={len(user_text)} reply_len={len(assistant_text)}"
            )
            # 淘汰缓存: 会话已更新, 下次 prompt 从 DB 加载最新历史。TTL 30s 覆盖连续对话间隔。
            self._history_cache.pop(session_id, None)
        except Exception as e:
            logger.error(f"[ACP] persist 失败: {e}", exc_info=True)