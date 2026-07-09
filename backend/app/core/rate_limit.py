"""统一速率限制模块。

提供 IP 粒度和 session 粒度的滑动窗口速率限制。
auth.py / websocket.py / ide_plugin API 等共用此模块，
避免速率限制逻辑散落在各文件中。

优先使用 Redis 滑动窗口（跨 worker 一致），Redis 不可用时回退到内存实现。
"""

import asyncio
import time
from collections import defaultdict

from loguru import logger

# ── 内存回退存储 ──
_ip_store: dict[str, list[float]] = defaultdict(list)
_session_store: dict[str, list[float]] = defaultdict(list)
_lock = asyncio.Lock()

# ── Redis 客户端（惰性初始化 + 冷却退避）──
_redis_client: "Redis | None" = None  # type: ignore[name-defined]
_redis_init_attempted = False
_redis_last_fail_time = 0.0
_REDIS_RETRY_COOLDOWN_S = 60  # 连接失败后 60s 内不再重试，避免日志风暴


def _get_redis():
    global _redis_client, _redis_init_attempted, _redis_last_fail_time
    if _redis_init_attempted:
        # 已有成功连接，直接返回
        if _redis_client is not None:
            return _redis_client
        # 之前失败过，冷却期内不再重试
        if time.time() - _redis_last_fail_time < _REDIS_RETRY_COOLDOWN_S:
            return None
        # 冷却期过，允许重试一次
        _redis_init_attempted = False
    _redis_init_attempted = True
    try:
        import redis.asyncio as aioredis
        from app.config import get_settings
        cfg = get_settings()
        if cfg.REDIS_URL:
            _redis_client = aioredis.from_url(
                cfg.REDIS_URL,
                socket_connect_timeout=2,
                socket_timeout=2,
                decode_responses=True,
            )
            logger.info("Redis rate limiter connected: {}", cfg.REDIS_URL)
    except Exception:
        _redis_last_fail_time = time.time()
    return _redis_client


# ── 默认限流配额 ──
# 格式: key → (max_attempts, window_seconds)
_DEFAULT_IP_LIMITS: dict[str, tuple[int, int]] = {
    "ws_connect": (10, 60),       # WebSocket 连接：10次/60s
    "auth_refresh": (10, 60),     # JWT 刷新：10次/60s
}

_DEFAULT_SESSION_LIMITS: dict[str, tuple[int, int]] = {
    "tool_execute": (30, 10),     # 工具执行：30次/10s（同一 session）
    "tool_write": (10, 60),       # 写操作工具：10次/60s（file_write/delete 类）
    "auth_refresh": (10, 60),     # JWT 刷新：10次/60s（按 user_id）
}


async def check_ip_rate_limit(
    ip: str, endpoint_key: str, limits: dict[str, tuple[int, int]] | None = None
) -> None:
    """按 IP 粒度限流。超限时抛 HTTPException(429)。

    Args:
        ip: 客户端 IP 地址
        endpoint_key: 端点标识（如 "ws_connect"），用于查找限流配额
        limits: 自定义限流配置，默认使用 _DEFAULT_IP_LIMITS
    """
    _ = limits  # 保留扩展点

    # localhost/loopback 豁免：IDE 插件、本地调试工具从 127.0.0.1/::1 连接，
    # 不应受速率限制。IDE 插件重连（6 次/12s）会触发 10/60s 阈值。
    # localhost/loopback + Docker Desktop 主机 IP 豁免：
    # IDE 插件重连（6 次/12s）会触发 10/60s 阈值；Docker Desktop macOS 通过 192.168.65.1 转发
    if ip in ("127.0.0.1", "::1", "0:0:0:0:0:0:0:1", "localhost", "192.168.65.1"):
        return

    max_attempts, window_s = _DEFAULT_IP_LIMITS.get(endpoint_key, (10, 60))
    now = time.time()
    cutoff = now - window_s

    redis = _get_redis()
    if redis is not None:
        redis_key = f"rl:ip:{endpoint_key}:{ip}"
        try:
            async with redis.pipeline() as pipe:
                pipe.zremrangebyscore(redis_key, 0, cutoff)
                pipe.zcard(redis_key)
                pipe.zadd(redis_key, {str(now): now})
                pipe.expire(redis_key, window_s + 5)
                _, current, _, _ = await pipe.execute()
                if int(current) >= max_attempts:
                    raise _rate_limit_exceeded(endpoint_key, max_attempts, window_s, int(current))
                return
        except Exception as e:
            if "429" in str(type(e).__name__) or "rate" in str(e).lower():
                raise
            global _redis_client, _redis_last_fail_time
            _redis_client = None
            _redis_last_fail_time = time.time()
            logger.info("[RateLimit] Redis 不可用，回退到内存限流（开发环境正常）: {}", e)

    # 内存回退（单 worker 有效）
    bucket_key = f"{endpoint_key}:{ip}"
    async with _lock:
        ts_list = _ip_store[bucket_key]
        ts_list[:] = [t for t in ts_list if t > cutoff]
        if len(ts_list) >= max_attempts:
            raise _rate_limit_exceeded(endpoint_key, max_attempts, window_s, len(ts_list))
        ts_list.append(now)


async def check_session_rate_limit(
    session_id: str, endpoint_key: str, limits: dict[str, tuple[int, int]] | None = None
) -> None:
    """按 session 粒度限流。超限时抛出 RuntimeError（由调用方转为友好错误）。

    Args:
        session_id: 会话 ID
        endpoint_key: 端点标识（如 "tool_execute"），用于查找限流配额
        limits: 自定义限流配置，默认使用 _DEFAULT_SESSION_LIMITS
    """
    target = limits if limits else _DEFAULT_SESSION_LIMITS
    max_attempts, window_s = target.get(endpoint_key, (50, 10))
    now = time.time()
    cutoff = now - window_s

    redis = _get_redis()
    if redis is not None:
        redis_key = f"rl:session:{endpoint_key}:{session_id}"
        try:
            async with redis.pipeline() as pipe:
                pipe.zremrangebyscore(redis_key, 0, cutoff)
                pipe.zcard(redis_key)
                pipe.zadd(redis_key, {str(now): now})
                pipe.expire(redis_key, window_s + 5)
                _, current, _, _ = await pipe.execute()
                if int(current) >= max_attempts:
                    logger.warning(
                        "[RATE] limit exceeded endpoint={} count={}/{}s",
                        f"session:{endpoint_key}", int(current), window_s,
                    )
                    raise RuntimeError(
                        f"Session rate limit exceeded: {endpoint_key} "
                        f"({max_attempts} per {window_s}s)"
                    )
                return
        except Exception as e:
            if "rate" in str(e).lower():
                raise
            global _redis_client, _redis_last_fail_time
            _redis_client = None
            _redis_last_fail_time = time.time()
            logger.info("[RateLimit] Redis 不可用，回退到内存限流（开发环境正常）: {}", e)

    # 内存回退
    bucket_key = f"session:{endpoint_key}:{session_id}"
    async with _lock:
        ts_list = _session_store[bucket_key]
        ts_list[:] = [t for t in ts_list if t > cutoff]
        if len(ts_list) >= max_attempts:
            logger.warning(
                "[RATE] limit exceeded endpoint=session:{} count={}/{}s",
                endpoint_key, len(ts_list), window_s,
            )
            raise RuntimeError(
                f"Session rate limit exceeded: {endpoint_key} "
                f"({max_attempts} per {window_s}s)"
            )
        ts_list.append(now)


def _rate_limit_exceeded(key: str, max_attempts: int, window_s: int, count: int = 0):
    from fastapi import HTTPException, status
    logger.warning(
        "[RATE] limit exceeded endpoint={} count={}/{}s",
        key, count if count else f">{max_attempts}", window_s,
    )
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=f"Rate limit exceeded: {key} ({max_attempts} per {window_s}s)",
    )


from app.services.llm.tool_execution_policy import WORKSPACE_WRITE_TOOLS, SERIAL_ALWAYS


def is_write_tool(tool_name: str) -> bool:
    """判断工具是否为写操作（更严格的频率限制）。"""
    return tool_name in WORKSPACE_WRITE_TOOLS or tool_name in SERIAL_ALWAYS
