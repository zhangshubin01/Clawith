"""Helpers for first-party chat session selection and creation."""

from __future__ import annotations

import time as _time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import case, cast, func, select, String
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession

# #135 修复：get_primary_platform_session 的内存缓存
# TTL 30 秒，避免频繁查询主会话。
# #147 修复：添加 OrderedDict LRU 淘汰，maxsize=256 防止缓存无界增长。
# cachetools 未安装，使用 OrderedDict 实现 LRU，无需外部依赖。
_PRIMARY_SESSION_CACHE_TTL = 30.0
_PRIMARY_SESSION_CACHE_MAXSIZE = 256
_primary_session_cache: OrderedDict[tuple, tuple[float, uuid.UUID | None]] = OrderedDict()


def _cache_set(key: tuple, value: tuple[float, uuid.UUID | None]) -> None:
    """将键值写入缓存，超出 maxsize 时淘汰最久未使用的条目（LRU）。

    使用 OrderedDict 实现：新条目默认追加到末尾（最近使用），
    淘汰时从头部弹出（最久未使用）。
    """
    global _primary_session_cache
    # 如果键已存在，先删除再插入以更新位置到末尾
    if key in _primary_session_cache:
        del _primary_session_cache[key]
    elif len(_primary_session_cache) >= _PRIMARY_SESSION_CACHE_MAXSIZE:
        _evicted_key, _evicted_value = _primary_session_cache.popitem(last=False)
        logger.debug(
            "[SessionCache] LRU 淘汰: agent={} user={} channel={}",
            *_evicted_key
        )
    _primary_session_cache[key] = value


async def get_primary_platform_session(
    db: AsyncSession,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    source_channel: str = "web",
) -> ChatSession | None:
    """返回用户+Agent 配对的长期主会话（#100 修复：source_channel 参数化）。

    # #135 修复：添加 30 秒 TTL 的内存缓存，减少重复 DB 查询。
    # #147 修复：maxsize=256 上限 + OrderedDict LRU 淘汰，防止缓存无界增长。
    # 缓存命中时只通过主键快速获取，避免重复执行复杂查询。
    """

    _cache_key = (agent_id, user_id, source_channel)
    _now = _time.monotonic()
    _cached = _primary_session_cache.get(_cache_key)
    if _cached is not None:
        _cache_ts, _cached_id = _cached
        if _now - _cache_ts < _PRIMARY_SESSION_CACHE_TTL:
            # 缓存命中：移到 OrderedDict 末尾（标记为最近使用）
            _primary_session_cache.move_to_end(_cache_key)
            if _cached_id is None:
                return None
            # 通过主键快速获取，避免重复执行复杂查询
            result = await db.execute(
                select(ChatSession).where(ChatSession.id == _cached_id)
            )
            session = result.scalar_one_or_none()
            if session:
                logger.debug("[SessionCache] 缓存命中: agent={} user={} channel={}", agent_id, user_id, source_channel)
                return session
            # 缓存指向的会话已被删除，清除缓存继续走 DB 查询
            logger.debug("[SessionCache] 缓存过期：会话已被删除 session_id={}", _cached_id)
            del _primary_session_cache[_cache_key]
        else:
            # TTL 过期，清除旧缓存
            del _primary_session_cache[_cache_key]

    result = await db.execute(
        select(ChatSession)
        .where(
            ChatSession.agent_id == agent_id,
            ChatSession.user_id == user_id,
            ChatSession.source_channel == source_channel,
            ChatSession.is_group == False,
            ChatSession.is_primary == True,
        )
        .limit(1)
    )
    session = result.scalar_one_or_none()

    # 更新缓存（无论是否为 None 都缓存，避免缓存穿透）
    _cache_set(_cache_key, (_now, session.id if session else None))
    return session


async def ensure_primary_platform_session(
    db: AsyncSession,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    source_channel: str = "web",
) -> ChatSession:
    """返回用户+Agent 配对的长期主会话（#100 修复：source_channel 参数化）。

    升级策略：
    - 已有主会话时直接复用
    - 否则将最相关的已有会话提升为主会话
    - 从未对话过则创建全新主会话
    """

    primary = await get_primary_platform_session(db, agent_id, user_id, source_channel=source_channel)
    if primary:
        return primary

    # Prefer a session with at least one user-authored message so we anchor the long-lived
    # primary conversation to the user's real historical thread when possible.
    user_message_count = (
        select(
            ChatMessage.conversation_id.label("conversation_id"),
            func.sum(case((ChatMessage.role == "user", 1), else_=0)).label("user_msg_count"),
        )
        .group_by(ChatMessage.conversation_id)
        .subquery()
    )

    result = await db.execute(
        select(ChatSession)
        .outerjoin(user_message_count, user_message_count.c.conversation_id == cast(ChatSession.id, String))
        .where(
            ChatSession.agent_id == agent_id,
            ChatSession.user_id == user_id,
            ChatSession.source_channel == source_channel,
            ChatSession.is_group == False,
        )
        .order_by(
            case((func.coalesce(user_message_count.c.user_msg_count, 0) > 0, 0), else_=1),
            ChatSession.last_message_at.desc().nulls_last(),
            ChatSession.created_at.desc(),
        )
        .limit(1)
    )
    existing = result.scalar_one_or_none()
    if existing:
        existing.is_primary = True
        await db.flush()
        return existing

    now = datetime.now(timezone.utc)
    session = ChatSession(
        agent_id=agent_id,
        user_id=user_id,
        title=f"Session {now.strftime('%m-%d %H:%M')}",
        source_channel=source_channel,
        is_primary=True,
        created_at=now,
    )
    db.add(session)
    await db.flush()
    return session


async def save_tool_call_log(
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: str,
    tool_name: str,
    arguments: dict | None,
    result: str,
    status: str = "done",
    tool_call_id: str | None = None,
    reasoning_content: str | None = None,
) -> None:
    """Save a tool call execution log into chat history as a ChatMessage."""
    if not conversation_id:
        return
    import json
    from app.database import async_session
    from loguru import logger

    payload = {
        "name": tool_name,
        "args": arguments or {},
        "status": status,
        "result": str(result) if result is not None else "",
        "tool_call_id": tool_call_id,
        "reasoning_content": reasoning_content,
    }

    try:
        async with async_session() as db:
            db.add(ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="tool_call",
                content=json.dumps(payload, ensure_ascii=False, default=str),
                conversation_id=conversation_id,
            ))
            await db.commit()
    except Exception as e:
        logger.warning(f"Failed to save tool call log: {e}")

