"""ACP 会话管理 — ChatSession 表 CRUD (source_channel="acp")。

复用已有 ChatSession/ChatMessage 模型，通过 source_channel 区分来源。
实际模型字段: ChatSession.project_path (cwd), ChatMessage.conversation_id (session)
"""

import os
import time
import uuid
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import func, select

from app.database import async_session
from app.models.chat_session import ChatSession
from app.models.audit import ChatMessage

# session/load 展示：仅 user+assistant 条数上限
ACP_HISTORY_LIMIT = int(os.getenv("ACP_HISTORY_LIMIT", "40"))
# LLM 上下文：含 tool_call 原始行数上限（convert 后由 handler 再按 context_window 截断）
ACP_RAW_HISTORY_LIMIT = int(os.getenv("ACP_RAW_HISTORY_LIMIT", "200"))


class AcpSessionManager:
    """ACP 会话管理器 — 基于 ChatSession/ChatMessage 表持久化。"""

    def __init__(self):
        self._history_cache: dict[str, tuple[float, list[dict]]] = {}
        # TTL 30s: ACP 多轮对话场景, persist_turn 后主动更新缓存, 延长 TTL 覆盖连续对话间隔
        self._CACHE_TTL = 30.0
        from app.plugins.clawith_acp.history_cache import register_history_cache_invalidator

        register_history_cache_invalidator(self.invalidate_history_cache)

    def invalidate_history_cache(self, session_id: str) -> None:
        """tool 写入 DB 后淘汰内存历史，避免取消 prompt 后 30s 内脏读。"""
        if self._history_cache.pop(session_id, None) is not None:
            logger.debug("[ACP] history cache invalidated session={}", session_id[:8])

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

            tc_r = await db.execute(
                select(func.count())
                .select_from(ChatMessage)
                .where(ChatMessage.conversation_id == session_id)
                .where(ChatMessage.user_id == uuid.UUID(user_id))
                .where(ChatMessage.role == "tool_call")
            )
            tool_call_count = int(tc_r.scalar() or 0)

            logger.info(
                "[ACP] 加载: id={} history={}条 tool_call_count={}",
                session_id,
                len(history),
                tool_call_count,
            )
            return {
                "agent_id": str(session.agent_id),
                "cwd": session.project_path or "",  # ChatSession.project_path
                "history": history,
                "historyHasTools": tool_call_count > 0,
                "tool_call_count": tool_call_count,
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
        ctx_window: int = 100_000,
    ) -> list[dict]:
        """加载最近对话历史，供 LLM 多轮上下文（user/assistant + tool_call，对齐 WS 路径）。"""
        cached = self._history_cache.get(session_id)
        if cached and (time.monotonic() - cached[0]) < self._CACHE_TTL:
            return cached[1]

        try:
            sid_uuid = uuid.UUID(session_id)
            uid_uuid = uuid.UUID(user_id)
        except ValueError:
            return []

        from app.services.llm.utils import convert_chat_messages_to_llm_format

        raw_limit = max(limit, ACP_RAW_HISTORY_LIMIT)
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
                .where(ChatMessage.role.in_(("user", "assistant", "tool_call")))
                .order_by(ChatMessage.created_at.desc())
                .limit(raw_limit)
            )
            rows = list(reversed(hr.scalars().all()))
            result = convert_chat_messages_to_llm_format(
                rows, ctx_window=ctx_window, path="acp",
            )
            if result and sess:
                from app.services.llm.history_hydrate import hydrate_history_tool_results

                result = await hydrate_history_tool_results(
                    result,
                    session_id=session_id,
                    agent_id=sess.agent_id,
                    ctx_path="acp",
                    ctx_window=ctx_window,
                )
            self._history_cache[session_id] = (time.monotonic(), result)
            logger.info(
                f"[ACP] load_history_for_llm: session={session_id} "
                f"raw_rows={len(rows)} llm_msgs={len(result)}"
            )
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
            self.invalidate_history_cache(session_id)
        except Exception as e:
            logger.error(f"[ACP] persist 失败: {e}", exc_info=True)