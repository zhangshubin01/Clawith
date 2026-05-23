"""Redis Pub/Sub events for enterprise info sync."""

import json
import time


import redis.asyncio as redis

from app.config import get_settings

settings = get_settings()

_redis_client: redis.Redis | None = None
_redis_last_fail_time = 0.0
_REDIS_RETRY_COOLDOWN_S = 60


async def get_redis() -> redis.Redis | None:
    """获取或创建 Redis 客户端。

    增加冷却机制：连接失败后 60s 内不再重试，避免日志风暴。
    Redis 不可用时返回 None，调用方需自行降级处理。
    """
    global _redis_client, _redis_last_fail_time
    if _redis_client is not None:
        return _redis_client
    # 冷却期内不重试
    if time.time() - _redis_last_fail_time < _REDIS_RETRY_COOLDOWN_S:
        return None
    try:
        _redis_client = redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        await _redis_client.ping()
        _redis_last_fail_time = 0.0
    except Exception:
        _redis_client = None
        _redis_last_fail_time = time.time()
        return None
    return _redis_client


async def publish_event(channel: str, data: dict) -> None:
    """Publish an event to a Redis Pub/Sub channel."""
    r = await get_redis()
    if r is None:
        return
    await r.publish(channel, json.dumps(data))


async def close_redis() -> None:
    """Close the Redis connection."""
    global _redis_client
    if _redis_client:
        await _redis_client.aclose()
        _redis_client = None
