"""CCR 存储服务 —— Layer 0 有损压缩的可逆归档（PostgreSQL 真源 + 进程内 LRU 读缓存）。

职责：
- store_entry: 有损压缩前把完整原文写入 ctx_ccr_entries，返回 64 位 SHA256 hash
- retrieve_entry: 按 (session_id, content_hash) 取回原文（先查内存 LRU，未命中查 PG）
- ccr_marker: 生成压缩结果首行的 CCR marker + retrieve 提示
- purge_expired: 删除过期条目（TTL）

reversibility gate 由调用方（caller._process_tool_call）执行：store 失败 → 回退原文。

日志前缀 [CTX-CCR]。
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.database import async_session
from app.models.ctx_ccr import CtxCcrEntry
from app.services.llm.stable_context import (
    CCR_SYSTEM_APPENDIX,
    RETRIEVE_CONTEXT_TOOL_DEFINITION,
    RETRIEVE_CONTEXT_TOOL_NAME,
)

# 进程内 write-through 读缓存（全局，跨会话）。key = f"{session_id}:{hash}"。
# PG 为真源；commit 成功后才写入 _MEM。
_MEM_MAX = 1000


@dataclass(frozen=True)
class MemEntry:
    """进程内 CCR 缓存条目，携带 PG 一致的 expires_at。"""

    content: str
    expires_at: datetime | None


_MEM: "OrderedDict[str, MemEntry]" = OrderedDict()

# Stage5 运维计数器
CCR_METRICS: dict[str, int] = {
    "store_ok": 0,
    "store_fail": 0,
    "store_reject_empty_session": 0,
    "store_reject_empty_content": 0,
    "store_evict": 0,
    "retrieve_hit": 0,
    "retrieve_miss": 0,
    "retrieve_miss_not_found": 0,
    "retrieve_miss_expired": 0,
    "retrieve_fail": 0,
    "gate_skip_no_retrieve_tool": 0,
    "gate_skip_store_failed": 0,
    "gate_skip_never_worse_after_store": 0,
    "hard_ceil_store_ok": 0,
    "hard_ceil_store_fail": 0,
    "hard_ceil_store_error": 0,
    "hard_ceil_irreversible": 0,
    "purge_deleted": 0,
    "fold_aborted_offload_incomplete": 0,
    "fold_noop_tier1": 0,
    "fold_skip_no_retrieve": 0,
    "fold_failed": 0,
    "fold_ok": 0,
}

# 进程内 token savings 账本（scope=process，重启清零）
@dataclass
class _StatsBucket:
    count: int = 0
    original_tokens: int = 0
    final_tokens: int = 0
    saved_tokens: int = 0
    ccr_store_ok: int = 0
    ccr_store_fail: int = 0
    retrieve_hit: int = 0
    retrieve_miss: int = 0


_COMPRESSION_STATS: dict[str, _StatsBucket] = defaultdict(_StatsBucket)
_COMPRESSION_STATS_MAX_KEYS = 500

_CCR_RETRIEVED_MARKER = "<!-- ccr:retrieved -->"


def _session_tag(session_id: str) -> str:
    """日志脱敏：仅暴露 session 前缀。"""
    s = (session_id or "").strip()
    return s[:8] if s else ""


def content_sha256(content: str) -> str:
    """完整 64 位 SHA256 hex。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def ccr_marker(content_hash: str) -> str:
    """压缩结果首行注入的 CCR marker + 恢复提示（rtk tee hint 等价）。"""
    return (
        f"<!-- ccr:{content_hash} -->\n"
        f"[完整内容已归档，可调用 retrieve_context(hash=\"{content_hash}\") 取回]"
    )


def is_retrieved(content: str) -> bool:
    """判断 tool 结果是否为 retrieve_context 取回的原文（Layer 0/1 应跳过压缩）。"""
    return isinstance(content, str) and _CCR_RETRIEVED_MARKER in content


def _mem_put(key: str, content: str, expires_at: datetime | None) -> None:
    _MEM[key] = MemEntry(content=content, expires_at=expires_at)
    _MEM.move_to_end(key)
    while len(_MEM) > _MEM_MAX:
        _MEM.popitem(last=False)


def _mem_remove(key: str) -> None:
    _MEM.pop(key, None)


def _mem_get_valid(key: str) -> str | None:
    """读取 mem 缓存并校验 TTL；过期则删除。"""
    entry = _MEM.get(key)
    if entry is None:
        return None
    now = datetime.now(timezone.utc)
    if entry.expires_at is not None and entry.expires_at < now:
        _mem_remove(key)
        incr_ccr_metric("retrieve_miss_expired")
        return None
    _MEM.move_to_end(key)
    return entry.content


def _mem_expiring_count() -> int:
    now = datetime.now(timezone.utc)
    return sum(1 for e in _MEM.values() if e.expires_at is not None and e.expires_at < now)


def incr_ccr_metric(name: str, delta: int = 1) -> None:
    """CCR 进程内计数器统一入口；未知 key 也保留，便于灰度观测新事件。"""
    CCR_METRICS[name] = CCR_METRICS.get(name, 0) + delta


def record_compression_event(
    *,
    tool_name: str,
    strategy: str,
    lossiness: str,
    original_tokens: int,
    final_tokens: int,
    store_ok: bool,
) -> None:
    """记录 Layer0/hard_ceil 压缩事件到进程内 stats；never-worse 回退不计 saved。"""
    if final_tokens >= original_tokens:
        return
    key = f"{tool_name}|{strategy}|{lossiness}"
    if len(_COMPRESSION_STATS) >= _COMPRESSION_STATS_MAX_KEYS and key not in _COMPRESSION_STATS:
        return
    bucket = _COMPRESSION_STATS[key]
    bucket.count += 1
    bucket.original_tokens += original_tokens
    bucket.final_tokens += final_tokens
    bucket.saved_tokens += max(0, original_tokens - final_tokens)
    if store_ok:
        bucket.ccr_store_ok += 1
    else:
        bucket.ccr_store_fail += 1


def record_retrieve_event(*, hit: bool) -> None:
    """retrieve 命中/未命中聚合（无 hash/session）。"""
    key = "_retrieve|_|_"
    bucket = _COMPRESSION_STATS[key]
    if hit:
        bucket.retrieve_hit += 1
    else:
        bucket.retrieve_miss += 1


def _stats_snapshot() -> dict:
    """按 tool|strategy|lossiness 聚合的脱敏 stats。"""
    by_lossiness: dict[str, dict] = defaultdict(lambda: {"count": 0, "saved_tokens": 0})
    top_tools: dict[str, dict] = defaultdict(lambda: {"count": 0, "saved_tokens": 0})
    total_saved = 0
    total_count = 0
    for key, bucket in _COMPRESSION_STATS.items():
        if key.startswith("_retrieve"):
            continue
        parts = key.split("|", 2)
        tool = parts[0] if parts else "unknown"
        lossiness = parts[2] if len(parts) > 2 else "unknown"
        by_lossiness[lossiness]["count"] += bucket.count
        by_lossiness[lossiness]["saved_tokens"] += bucket.saved_tokens
        top_tools[tool]["count"] += bucket.count
        top_tools[tool]["saved_tokens"] += bucket.saved_tokens
        total_saved += bucket.saved_tokens
        total_count += bucket.count
    retrieve_bucket = _COMPRESSION_STATS.get("_retrieve|_|_")
    return {
        "scope": "process",
        "total_events": total_count,
        "total_saved_tokens": total_saved,
        "retrieve_hit": retrieve_bucket.retrieve_hit if retrieve_bucket else 0,
        "retrieve_miss": retrieve_bucket.retrieve_miss if retrieve_bucket else 0,
        "by_lossiness": dict(by_lossiness),
        "top_tools": dict(sorted(top_tools.items(), key=lambda x: -x[1]["saved_tokens"])[:10]),
    }


def get_ccr_metrics_snapshot() -> dict:
    """返回脱敏 CCR 健康快照；禁止暴露原文、hash、session、agent、path。"""
    settings = get_settings()
    hard_ceil_chars = None
    try:
        from app.services.llm.tool_trim import _TOOL_HARD_CEIL_CHARS

        hard_ceil_chars = _TOOL_HARD_CEIL_CHARS
    except Exception as e:
        logger.debug("[CTX-CCR] metrics snapshot hard_ceil unavailable err={}", e)
    return {
        "ccr": {
            "metrics": dict(CCR_METRICS),
            "mem_cache_size": len(_MEM),
            "mem_cache_max": _MEM_MAX,
            "mem_cache_expiring_count": _mem_expiring_count(),
            "config": {
                "ttl_hours": getattr(settings, "CTX_CCR_TTL_HOURS", 24),
                "max_per_session": getattr(settings, "CTX_CCR_MAX_PER_SESSION", 500),
                "tracker_enabled": getattr(settings, "CTX_TRACKER_ENABLED", True),
                "offload_history": True,
            },
        },
        "compression": {
            "config": {
                "enabled": getattr(settings, "CTX_COMPRESS_ENABLED", True),
                "lossless_only": getattr(settings, "CTX_LOSSLESS_ONLY", False),
                "relevance_split_enabled": getattr(settings, "CTX_RELEVANCE_SPLIT_ENABLED", True),
                "feedback_enabled": getattr(settings, "CTX_COMPRESSION_FEEDBACK_ENABLED", False),
                "output_shaper_enabled": getattr(settings, "CTX_OUTPUT_SHAPER_ENABLED", False),
                "hard_ceil_chars": hard_ceil_chars,
            },
            "stats": _stats_snapshot(),
        },
    }


async def store_entry(
    session_id: str,
    agent_id,
    content: str,
    tool_name: str = "",
    path: str = "",
    original_tokens: int = 0,
    compressed_tokens: int = 0,
) -> str | None:
    """把完整原文写入 PG（幂等），返回 64 位 hash；失败返回 None（调用方回退原文）。"""
    if not session_id:
        incr_ccr_metric("store_reject_empty_session")
        logger.warning("[CTX-CCR] store rejected reason=empty_session tool={}", tool_name)
        return None
    if not content:
        incr_ccr_metric("store_reject_empty_content")
        logger.warning("[CTX-CCR] store rejected reason=empty_content tool={}", tool_name)
        return None

    h = content_sha256(content)
    key = f"{session_id}:{h}"
    settings = get_settings()
    ttl_hours = getattr(settings, "CTX_CCR_TTL_HOURS", 24)
    max_per_session = getattr(settings, "CTX_CCR_MAX_PER_SESSION", 500)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=ttl_hours)
    agent_id_str = str(agent_id) if agent_id else None
    sid = _session_tag(session_id)

    try:
        async with async_session() as db:
            existing = await db.execute(
                select(CtxCcrEntry.expires_at).where(
                    CtxCcrEntry.session_id == session_id,
                    CtxCcrEntry.content_hash == h,
                )
            )
            existing_exp = existing.scalar_one_or_none()
            if existing_exp is not None:
                incr_ccr_metric("store_ok")
                _mem_put(key, content, existing_exp)
                logger.debug("[CTX-CCR] store dedup session={} hash={} tool={}", sid, h[:12], tool_name)
                return h

            cnt = await db.execute(
                select(func.count()).select_from(CtxCcrEntry).where(CtxCcrEntry.session_id == session_id)
            )
            if (cnt.scalar_one() or 0) >= max_per_session:
                oldest = await db.execute(
                    select(CtxCcrEntry.id, CtxCcrEntry.content_hash)
                    .where(CtxCcrEntry.session_id == session_id)
                    .order_by(CtxCcrEntry.created_at.asc())
                    .limit(1)
                )
                row = oldest.first()
                if row is not None:
                    oid, evicted_hash = row[0], row[1]
                    await db.execute(delete(CtxCcrEntry).where(CtxCcrEntry.id == oid))
                    _mem_remove(f"{session_id}:{evicted_hash}")
                    incr_ccr_metric("store_evict")
                    logger.info(
                        "[CTX-CCR] evict session={} evicted_hash={} reason=max_per_session max={}",
                        sid, evicted_hash[:12], max_per_session,
                    )

            db.add(CtxCcrEntry(
                session_id=session_id,
                agent_id=agent_id_str,
                content_hash=h,
                tool_name=tool_name or "",
                content=content,
                original_tokens=original_tokens,
                compressed_tokens=compressed_tokens,
                path=path or "",
                expires_at=expires_at,
            ))
            await db.commit()

        _mem_put(key, content, expires_at)
        incr_ccr_metric("store_ok")
        logger.info(
            "[CTX-CCR] store session={} hash={} tool={} path={} tokens={}→{}",
            sid, h[:12], tool_name, path, original_tokens, compressed_tokens,
        )
        return h
    except IntegrityError:
        incr_ccr_metric("store_ok")
        try:
            async with async_session() as db:
                row = await db.execute(
                    select(CtxCcrEntry.expires_at).where(
                        CtxCcrEntry.session_id == session_id,
                        CtxCcrEntry.content_hash == h,
                    )
                )
                exp = row.scalar_one_or_none()
            _mem_put(key, content, exp)
        except Exception:
            pass
        logger.debug("[CTX-CCR] store dedup race session={} hash={} tool={}", sid, h[:12], tool_name)
        return h
    except Exception as e:
        _mem_remove(key)
        incr_ccr_metric("store_fail")
        logger.warning("[CTX-CCR] store failed session={} tool={} err={}", sid, tool_name, e)
        return None


async def retrieve_entry(session_id: str, content_hash: str) -> str | None:
    """按 (session_id, content_hash) 取回原文。先查内存 LRU，未命中查 PG（校验未过期）。"""
    if not session_id or not content_hash:
        return None

    key = f"{session_id}:{content_hash}"
    sid = _session_tag(session_id)
    cached = _mem_get_valid(key)
    if cached is not None:
        incr_ccr_metric("retrieve_hit")
        record_retrieve_event(hit=True)
        logger.info("[CTX-CCR] retrieve hit session={} hash={} src=mem", sid, content_hash[:12])
        return cached

    try:
        now = datetime.now(timezone.utc)
        async with async_session() as db:
            row = await db.execute(
                select(CtxCcrEntry.content, CtxCcrEntry.expires_at).where(
                    CtxCcrEntry.session_id == session_id,
                    CtxCcrEntry.content_hash == content_hash,
                )
            )
            hit = row.first()
        if hit is None:
            incr_ccr_metric("retrieve_miss")
            incr_ccr_metric("retrieve_miss_not_found")
            record_retrieve_event(hit=False)
            logger.info("[CTX-CCR] retrieve miss session={} hash={} reason=not_found", sid, content_hash[:12])
            return None
        content, expires_at = hit[0], hit[1]
        if expires_at is not None and expires_at < now:
            _mem_remove(key)
            incr_ccr_metric("retrieve_miss")
            incr_ccr_metric("retrieve_miss_expired")
            record_retrieve_event(hit=False)
            logger.info("[CTX-CCR] retrieve miss session={} hash={} reason=expired", sid, content_hash[:12])
            return None
        _mem_put(key, content, expires_at)
        incr_ccr_metric("retrieve_hit")
        record_retrieve_event(hit=True)
        logger.info("[CTX-CCR] retrieve hit session={} hash={} src=pg", sid, content_hash[:12])
        return content
    except Exception as e:
        incr_ccr_metric("retrieve_fail")
        logger.warning("[CTX-CCR] retrieve failed session={} hash={} err={}", sid, content_hash[:12], e)
        return None


def messages_have_ccr(messages: list) -> bool:
    """扫描历史消息是否含 CCR marker（决定是否 sticky 注入 retrieve_context 工具）。"""
    for m in messages or []:
        c = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
        if isinstance(c, str) and "<!-- ccr:" in c:
            return True
    return False


async def retrieve_context_tool(session_id: str, arguments: dict) -> str:
    """execute_tool 的 retrieve_context 分支实现。返回原文（带 retrieved marker）或错误文案。"""
    content_hash = (arguments or {}).get("hash") or ""
    content_hash = str(content_hash).strip()
    if not content_hash:
        return "retrieve_context error: missing required `hash` argument."
    content = await retrieve_entry(session_id, content_hash)
    if content is None:
        return (
            f"retrieve_context miss: archived content for hash={content_hash[:12]}... is not available in this session. "
            "It may belong to another session, may have expired, or may have been purged. "
            "Recover by re-running the command or re-reading the referenced file if the marker includes enough context."
        )

    offset_raw = (arguments or {}).get("offset")
    limit_raw = (arguments or {}).get("limit")
    if offset_raw is not None:
        try:
            offset = max(int(offset_raw), 0)
        except (TypeError, ValueError):
            return "retrieve_context error: `offset` must be a non-negative integer."
        limit = None
        if limit_raw is not None:
            try:
                limit = max(int(limit_raw), 1)
            except (TypeError, ValueError):
                return "retrieve_context error: `limit` must be a positive integer."
        lines = content.split("\n")
        end = offset + limit if limit is not None else None
        content = "\n".join(lines[offset:end])

    # 首行 retrieved marker → Layer 0/1 跳过再压，保持 verbatim
    return f"{_CCR_RETRIEVED_MARKER}\n{content}"


def log_ccr_metrics() -> None:
    """进程内 CCR 计数器周期日志（运维可观测）。"""
    logger.info(
        "[CTX-CCR] metrics store_ok={} store_fail={} retrieve_hit={} retrieve_miss={} "
        "retrieve_miss_not_found={} retrieve_miss_expired={} retrieve_fail={} "
        "gate_skip_no_retrieve_tool={} gate_skip_store_failed={} gate_skip_never_worse={} "
        "hard_ceil_store_ok={} hard_ceil_store_fail={} hard_ceil_store_error={} hard_ceil_irreversible={} "
        "purge_deleted={}",
        CCR_METRICS.get("store_ok", 0),
        CCR_METRICS.get("store_fail", 0),
        CCR_METRICS.get("retrieve_hit", 0),
        CCR_METRICS.get("retrieve_miss", 0),
        CCR_METRICS.get("retrieve_miss_not_found", 0),
        CCR_METRICS.get("retrieve_miss_expired", 0),
        CCR_METRICS.get("retrieve_fail", 0),
        CCR_METRICS.get("gate_skip_no_retrieve_tool", 0),
        CCR_METRICS.get("gate_skip_store_failed", 0),
        CCR_METRICS.get("gate_skip_never_worse_after_store", 0),
        CCR_METRICS.get("hard_ceil_store_ok", 0),
        CCR_METRICS.get("hard_ceil_store_fail", 0),
        CCR_METRICS.get("hard_ceil_store_error", 0),
        CCR_METRICS.get("hard_ceil_irreversible", 0),
        CCR_METRICS.get("purge_deleted", 0),
    )


async def purge_expired() -> int:
    """删除所有已过期条目，返回删除行数；同步清理 mem 中过期 key。"""
    try:
        now = datetime.now(timezone.utc)
        async with async_session() as db:
            rows = await db.execute(
                select(CtxCcrEntry.session_id, CtxCcrEntry.content_hash).where(
                    CtxCcrEntry.expires_at < now
                )
            )
            expired_keys = [f"{r[0]}:{r[1]}" for r in rows.all()]
            res = await db.execute(delete(CtxCcrEntry).where(CtxCcrEntry.expires_at < now))
            await db.commit()
        for k in expired_keys:
            _mem_remove(k)
        n = res.rowcount or 0
        if n:
            incr_ccr_metric("purge_deleted", n)
            logger.info("[CTX-CCR] purge_expired deleted={}", n)
        return n
    except Exception as e:
        logger.warning("[CTX-CCR] purge_expired failed err={}", e)
        return 0
