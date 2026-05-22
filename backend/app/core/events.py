"""Redis Pub/Sub events for enterprise info sync."""

import json

import redis.asyncio as redis

from app.config import get_settings

settings = get_settings()

_redis_client: redis.Redis | None = None


async def get_redis() -> redis.Redis | None:
    """Get or create the Redis client. Returns None if Redis is unavailable."""
    global _redis_client
    if _redis_client is None:
        try:
            _redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
            await _redis_client.ping()
        except Exception:
            _redis_client = None
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
