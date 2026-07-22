"""工具执行输出流式推送 — Redis pubsub + 外部频道（飞书等）。"""

from __future__ import annotations

import json
import time as _time
import uuid
from datetime import datetime, timezone

import redis.asyncio as aioredis
from loguru import logger

from app.config import get_settings

# Redis pubsub 通道前缀
CHANNEL_PREFIX = "tool_output"


def _redis_url() -> str:
    return get_settings().REDIS_URL


# 模块级共享 Redis 连接（延迟初始化），避免每次 publish 都重新建连
_shared_redis: aioredis.Redis | None = None


async def _get_redis() -> aioredis.Redis:
    """获取或创建共享 Redis 连接。"""
    global _shared_redis
    old = _shared_redis
    if old is None:
        _shared_redis = aioredis.from_url(_redis_url())
    else:
        try:
            await old.ping()
        except Exception:
            _shared_redis = aioredis.from_url(_redis_url())
            try:
                await old.aclose()  # 关闭旧连接，防止连接泄漏
            except Exception:
                pass
    return _shared_redis


async def publish_tool_output(session_id: str, call_id: str, output: str, stream: str = "stdout") -> None:
    """发布工具执行输出到 Redis pubsub 通道。

    由 android_compile 的 on_output 回调调用，每次产生一行输出时发布。
    复用共享 Redis 连接，避免每行输出都重新建连。
    """
    try:
        r = await _get_redis()
        channel = f"{CHANNEL_PREFIX}:{session_id}:{call_id}"
        payload = json.dumps({"output": output, "stream": stream})
        await r.publish(channel, payload)
    except Exception as exc:
        logger.warning(f"[ToolStream] publish failed: {exc}")


async def subscribe_tool_output(session_id: str, call_id: str):
    """订阅工具执行输出通道，异步生成器逐条返回输出。

    由 chat_stream.py 在工具开始执行时调用，异步迭代每条输出。
    工具完成后由 chat_stream.py 取消 task 触发 CancelledError 退出。
    """
    r = aioredis.from_url(_redis_url())
    pubsub = r.pubsub()
    channel = f"{CHANNEL_PREFIX}:{session_id}:{call_id}"
    await pubsub.subscribe(channel)
    try:
        async for message in pubsub.listen():
            if message["type"] == "subscribe":
                continue
            if message["type"] == "message":
                data = json.loads(message["data"])
                yield data["output"], data.get("stream", "stdout")
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()
        await r.aclose()


# 频道推送去重（key = "session_id:tool:status" -> 上次推送时间）
_channel_push_sent: dict[str, float] = {}
_CHANNEL_PUSH_COOLDOWN = 3.0


async def push_tool_status_to_channel(
    session_id: str,
    tool_name: str,
    tool_status: str,
    result_text: str = "",
    *,
    run_id: uuid.UUID | None = None,
) -> None:
    """工具执行状态变更时推送进度到外部频道（飞书/Discord/Slack 等）。

    仅对 source_channel != "web" 的会话生效。
    通过 stage_channel_delivery 加入 outbox，异步发送，不阻塞主流程。
    """
    if not session_id:
        return

    logger.debug(f"[ToolStream] push_tool_status_to_channel: session={session_id} tool={tool_name} status={tool_status}")

    # 去重
    dedup_key = f"{session_id}:{tool_name}:{tool_status}"
    now = _time.monotonic()
    last = _channel_push_sent.get(dedup_key, 0)
    if now - last < _CHANNEL_PUSH_COOLDOWN:
        return
    _channel_push_sent[dedup_key] = now

    try:
        from app.database import async_session as async_session_factory

        async with async_session_factory() as db:
            session_uuid = uuid.UUID(session_id)

            # 查会话来源和 run
            from sqlalchemy import text
            row = await db.execute(
                text(
                    "SELECT cs.source_channel, cs.agent_id, cs.tenant_id, "
                    "ar.id AS run_id, ar.delivery_target "
                    "FROM chat_sessions cs "
                    "LEFT JOIN agent_runs ar ON ar.session_id = cs.id "
                    "AND ar.created_at = ("
                    "  SELECT MAX(created_at) FROM agent_runs WHERE session_id = cs.id"
                    ") "
                    "WHERE cs.id = :sid"
                ),
                {"sid": session_uuid},
            )
            session_row = row.fetchone()
            if session_row is None or session_row.source_channel == "web":
                return
            if session_row.run_id is None:
                return

            if tool_status == "running":
                content = f"⚡️ 正在执行 `{tool_name}`..."
            elif tool_status == "done":
                if result_text:
                    content = f"✅ `{tool_name}` 完成\n\n{result_text[:300]}"
                else:
                    content = f"✅ `{tool_name}` 完成"
            else:
                return

            now = datetime.now(timezone.utc)
            delivery_id = uuid.uuid4()
            message_id = uuid.uuid4()

            # 直接插入 ChatMessage（绕过 ORM FK 校验）
            await db.execute(
                text(
                    "INSERT INTO chat_messages "
                    "(id, agent_id, role, content, conversation_id, participant_id, created_at) "
                    "VALUES (:id, :agent_id, 'assistant', :content, :conv_id, NULL, :now)"
                ),
                {
                    "id": message_id,
                    "agent_id": session_row.agent_id,
                    "content": content,
                    "conv_id": session_id,
                    "now": now,
                },
            )

            # 直接插入 ChannelDelivery outbox
            delivery_target = session_row.delivery_target
            if isinstance(delivery_target, str):
                import json as _json
                delivery_target = _json.loads(delivery_target)
            channel_route = (delivery_target or {}).get("channel_delivery", {})
            target = channel_route.get("target", {})
            channel = (channel_route.get("channel") or "feishu")

            await db.execute(
                text(
                    "INSERT INTO channel_deliveries "
                    "(id, tenant_id, run_id, agent_id, session_id, message_id, "
                    " channel, target, idempotency_key, status, attempt_count, "
                    " next_attempt_at, created_at, updated_at) "
                    "VALUES (:id, :tenant_id, :run_id, :agent_id, :session_id, :message_id, "
                    " :channel, :target, :idempotency_key, 'pending', 0, "
                    " :now, :now, :now)"
                ),
                {
                    "id": delivery_id,
                    "tenant_id": session_row.tenant_id,
                    "run_id": session_row.run_id,
                    "agent_id": session_row.agent_id,
                    "session_id": session_uuid,
                    "message_id": message_id,
                    "channel": channel,
                    "target": json.dumps(target),
                    "idempotency_key": f"tool_{session_row.run_id}_{tool_name}_{tool_status}",
                    "now": now,
                },
            )
            await db.commit()
    except Exception:
        logger.warning("[ToolStream] push_tool_status_to_channel failed", exc_info=True)
