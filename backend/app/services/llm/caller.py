"""Unified LLM calling service with failover support for all execution paths.

This module provides a shared entry point for all LLM calls across:
- WebSocket chat
- IM channels (Feishu, Slack, Teams, Discord, WeCom, DingTalk)
- Background services (task executor, scheduler, heartbeat, etc.)

All paths now support:
1. Config-level fallback: if primary missing, use fallback directly
2. Runtime failover: if primary fails with retryable error, try fallback once
"""

from __future__ import annotations

import asyncio
import time
import json
import re
import uuid
import contextvars
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from app.debug_session_log import debug_session_log

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import get_settings
from app.database import async_session

# NOTE: agent_tools imports are deferred to function bodies to avoid circular
# import: agent_tools → llm.finish → llm/__init__ → caller → agent_tools

async def get_agent_tools_for_llm(*args, **kwargs):
    from app.services.agent_tools import get_agent_tools_for_llm as _impl
    return await _impl(*args, **kwargs)

async def execute_tool(*args, **kwargs):
    from app.services.agent_tools import execute_tool as _impl
    return await _impl(*args, **kwargs)
from app.services.token_tracker import (
    TokenUsage,
    record_token_usage,
    extract_token_usage,
    estimate_token_usage_from_chars,
)

from .client import LLMError
from .failover import classify_error, FailoverErrorType
from .finish import FINISH_PROTOCOL_REMINDER, FINISH_TOOL_DEFINITION, find_finish_call, parse_tool_arguments
from .utils import LLMMessage, create_llm_client, get_max_tokens, get_model_api_key

# 上下文压缩管道（从 e43694d9 提取至 context_compressor.py）。
# 重导出保持 `from app.services.llm.caller import ...` 兼容，测试与既有调用点不变。
from .emit_guarded import emit_guarded
from .tool_execution_policy import (
    ToolExecutionMode,
    WORKSPACE_WRITE_TOOLS,
    max_parallel_concurrency,
    partition_tool_calls,
    rate_limit_sem,
    workspace_write_lock,
)
from .compression_result import Lossiness, requires_ccr
from .compression_config import (
    layer1_compress_threshold_ratio,
    pre_round_budget,
    pre_round_budget_post_fold,
)
from .context_compressor import (  # noqa: F401
    HAS_TIKTOKEN,
    ContextCompressor,
    _breaker_is_open,
    _breaker_record_failure,
    _breaker_record_success,
    _code,
    _ctx_compress,
    _dedup_file_tool_results,
    _dedup_list_tool_results,
    _detect,
    _est_tokens,
    _est_tokens_str,
    _get_ctx_guard_max_window,
    _get_ctx_guard_ratios,
    _isolate,
    _json,
    _log,
    _multi_role_compress,
    _repair_truncated_messages,
    _search,
    _text,
    _trunc,
    _work_paths,
)
# Layer 0：工具结果注入期类型感知压缩（共享单一真源）。
from .tool_trim import (  # noqa: F401
    _COMPRESS_MARKER,
    _TOOL_HARD_CEIL_CHARS,
    _dispatch_guarded,
    _dispatch_guarded_result,
    _hard_head_tail,
    _effective_tool_budget,
    _tool_token_budget,
)

if TYPE_CHECKING:
    from app.models.agent import Agent
    from app.models.llm import LLMModel


TOOLS_REQUIRING_ARGS = frozenset({
    "write_file", "read_file", "move_file", "delete_file", "read_document",
    "send_message_to_agent", "send_feishu_message", "send_email"
})

# 调用路径标记：供 [CTX]/[CTX-GUARD] 分端统计（acp / ws / feishu / agent_tools）。
# 各入口在 call_llm 前 set；未 set 时 _resolve_ctx_path 自动推断。
current_ctx_path: ContextVar[str] = ContextVar("current_ctx_path", default="")



def _resolve_ctx_path() -> str:
    """推断当前 LLM 调用路径，用于压缩日志分端观测。"""
    explicit = (current_ctx_path.get() or "").strip()
    if explicit:
        return explicit
    try:
        from app.plugins.clawith_acp.tool_bridge import current_acp_handler
        if current_acp_handler.get() is not None:
            return "acp"
    except Exception:
        pass
    return "ws"




async def _apply_pre_round_context(
    api_messages: list,
    *,
    ctx_window: int,
    model_name: str,
    session_id: str,
    agent_id,
    ctx_path: str,
    tools_for_llm: list[dict] | None,
    round_i: int,
    compressor: ContextCompressor,
    last_cache_read: int = 0,
    last_cache_creation: int = 0,
) -> None:
    """P1 pre_round 管道（cache-safe）：observe → read_lc → fold → re-observe → emergency Layer1 → overlay。"""
    from .ccr_store import incr_ccr_metric
    from .context_zones import compute_zones, reactive_fold_messages

    settings = get_settings()
    retrieve_avail = _tools_include_retrieve_context(tools_for_llm)
    live_rounds = int(getattr(settings, "CTX_LIVE_ZONE_ROUNDS", 10))
    frozen_head = int(getattr(settings, "CTX_FROZEN_PREFIX_MSGS", 2))
    emergency = float(getattr(settings, "CTX_LAYER1_EMERGENCY", 0.85))

    tracker = getattr(compressor, "prefix_tracker", None)
    last_fold_round = int(getattr(compressor, "_last_fold_round", -100))

    if tracker is not None:
        try:
            tracker.observe(api_messages, last_cache_read, ctx_window)
        except Exception as e:
            logger.warning("[CTX-CACHE] tracker.observe failed path={} err={}", ctx_path, e)

    effective_frozen = max(
        frozen_head,
        tracker.frozen_count if tracker is not None else frozen_head,
    )
    zones = compute_zones(api_messages, effective_frozen, live_rounds)
    protect = max(zones.live_start, effective_frozen)

    if getattr(settings, "CTX_READ_LIFECYCLE_ENABLED", True):
        try:
            from .read_lifecycle import ReadLifecycleManager

            lr = await ReadLifecycleManager().apply_async(
                api_messages,
                session_id=session_id,
                agent_id=agent_id,
                ctx_path=ctx_path,
                frozen_message_count=protect,
                tools_available=retrieve_avail,
                model_name=model_name,
            )
            api_messages[:] = lr.messages
            if lr.reads_stale or lr.reads_superseded:
                logger.info(
                    "[CTX-READ-LC] path={} stale={} superseded={} saved_bytes={}",
                    ctx_path, lr.reads_stale, lr.reads_superseded, lr.bytes_before - lr.bytes_after,
                )
        except Exception as e:
            logger.warning("[CTX-READ-LC] apply failed path={} err={}", ctx_path, e)

    cur = _est_tokens(api_messages, model_name)
    session_pressure = cur / max(ctx_window, 1)
    logger.info(
        "[CTX-PRESSURE] path={} session={} pressure={:.2f} tokens={}/{} round={} cache_read={} cache_creation={}",
        ctx_path, _session_tag(session_id), session_pressure, cur, ctx_window, round_i + 1,
        last_cache_read, last_cache_creation,
    )

    budget_pre = pre_round_budget(
        session_pressure,
        retrieve_avail=retrieve_avail,
        cache_read=last_cache_read,
        round_i=round_i,
        last_fold_round=last_fold_round,
    )
    if budget_pre.skip_reason == "no_retrieve_tool" and session_pressure >= budget_pre.fold_high:
        incr_ccr_metric("fold_skip_no_retrieve")
        logger.info(
            "[CTX-BUDGET] path={} action=fold_skipped reason=no_retrieve_tool pressure={:.2f}",
            ctx_path, session_pressure,
        )

    fold_ran = False
    fold_failed = False
    if budget_pre.should_fold:
        try:
            new_msgs, folded = await reactive_fold_messages(
                api_messages,
                frozen_head=effective_frozen,
                ctx_window=ctx_window,
                model_name=model_name,
                session_id=session_id,
                agent_id=agent_id,
                ctx_path=ctx_path,
                low_water=budget_pre.fold_low,
                live_rounds=live_rounds,
                est_tokens_fn=_est_tokens,
            )
            if folded:
                api_messages[:] = new_msgs
                fold_ran = True
                compressor._last_fold_round = round_i
                compressor._fold_noop_streak = 0
                compressor.layer0_budget_scale = 1.0
                incr_ccr_metric("fold_ok")
            else:
                compressor._fold_noop_streak = int(getattr(compressor, "_fold_noop_streak", 0)) + 1
                if compressor._fold_noop_streak >= 2 and session_pressure >= budget_pre.fold_high:
                    incr_ccr_metric("fold_noop_tier1")
                    compressor.layer0_budget_scale = 0.85
                    logger.warning(
                        "[CTX-BUDGET] path={} fold_noop_tier1 streak={} pressure={:.2f} layer0_scale=0.85",
                        ctx_path, compressor._fold_noop_streak, session_pressure,
                    )
        except Exception as e:
            fold_failed = True
            incr_ccr_metric("fold_failed")
            logger.warning("[CTX-FOLD] failed path={} err={}", ctx_path, e)

    cur = _est_tokens(api_messages, model_name)
    session_pressure = cur / max(ctx_window, 1)
    if tracker is not None and fold_ran:
        try:
            tracker.observe(api_messages, last_cache_read, ctx_window)
            effective_frozen = max(frozen_head, tracker.frozen_count)
            zones = compute_zones(api_messages, effective_frozen, live_rounds)
            protect = max(zones.live_start, effective_frozen)
        except Exception as e:
            logger.warning("[CTX-CACHE] post-fold observe failed path={} err={}", ctx_path, e)

    budget_post = pre_round_budget_post_fold(
        session_pressure,
        tokens_after_fold=cur,
        ctx_window=ctx_window,
        cache_read=last_cache_read,
        fold_ran=fold_ran,
        fold_failed=fold_failed,
        retrieve_avail=retrieve_avail,
    )

    final_action = budget_post.action if budget_post.should_layer1 else (
        "fold" if fold_ran else budget_pre.action
    )
    if not budget_post.should_layer1 and budget_post.skip_reason and not fold_ran:
        final_action = f"noop:{budget_post.skip_reason}"

    logger.info(
        "[CTX-BUDGET] path={} action={} pressure={:.2f} protect_from={} cache_read={} round={}",
        ctx_path, final_action, session_pressure, protect, last_cache_read, round_i + 1,
    )

    if budget_post.should_layer1:
        api_messages[:] = _multi_role_compress(
            api_messages,
            ctx_window=ctx_window,
            model_name=model_name,
            round_i=round_i,
            session_id=session_id,
            compressor=compressor,
            compress_ratio=emergency,
            protect_prefix_count=protect,
        )

    if tracker is not None:
        try:
            api_messages[:] = tracker.overlay(api_messages)
            tracker.note_forwarded(api_messages)
        except Exception as e:
            logger.warning("[CTX-CACHE] overlay failed path={} err={}", ctx_path, e)



def _session_tag(session_id: str) -> str:
    """日志脱敏 session 前缀。"""
    s = (session_id or "").strip()
    return s[:8] if s else ""


def _log_tool_call_args(tool_name: str, raw_args, args: dict) -> None:
    """工具调用日志：retrieve hash 截断，敏感 key 脱敏。"""
    if tool_name == "retrieve_context":
        h = str((args or {}).get("hash") or "")
        logger.info("[LLM] Calling tool: retrieve_context(hash={}...)", h[:12] if h else "")
        return
    summary = _summarize_tool_args(args if isinstance(args, dict) else {})
    if summary:
        logger.info("[LLM] Calling tool: {}({})", tool_name, summary)
    else:
        logger.info("[LLM] Calling tool: {}()", tool_name)


async def _guarded_compress_with_ccr(
    original: str,
    tool_name: str,
    budget_tokens: int,
    model_name: str,
    path: str,
    session_id: str,
    agent_id,
    tools_available: bool = True,
    ctx_window: int = 100000,
    user_query: str = "",
    tool_args_text: str = "",
    session_pressure: float = 0.0,
) -> str:
    """Layer 0 有损压缩 + CCR reversibility gate（headroom #1307）。

    - 先跑同步类型感知压缩 `_dispatch_guarded`（内建 never_worse / 禁 _text / lossless-only）。
    - 若发生压缩（结果≠原文）：先把完整原文写入 PG CCR，成功才注入压缩结果 + `<!-- ccr:hash -->`
      marker；store 失败则回退原文（gate skip），保证「有损必可逆」。
    """
    dispatch_kwargs = {"path": path, "ctx_window": ctx_window, "session_pressure": session_pressure}
    if user_query or tool_args_text:
        dispatch_kwargs.update({"user_query": user_query, "tool_args_text": tool_args_text})
    comp_result = _dispatch_guarded_result(
        original, tool_name, budget_tokens, model_name, **dispatch_kwargs,
    )
    before = comp_result.original_tokens or _est_tokens_str(original, model_name)
    after = comp_result.final_tokens or _est_tokens_str(comp_result.content, model_name)

    if not comp_result.changed or not requires_ccr(comp_result):
        if comp_result.changed:
            logger.info(
                "[CTX-COMPRESS] path={} tool={} lossless strategy={} tokens={}→{}",
                path, tool_name, comp_result.strategy, before, after,
            )
        else:
            logger.info(
                "[CTX-COMPRESS] path={} tool={} skip reason={} tokens={} budget={}",
                path, tool_name, comp_result.strategy, before, budget_tokens,
            )
        return comp_result.content

    from app.services.llm.ccr_store import (
        ccr_marker,
        incr_ccr_metric,
        record_compression_event,
        store_entry,
    )
    if not tools_available:
        incr_ccr_metric("gate_skip_no_retrieve_tool")
        logger.info(
            "[CTX-CCR] gate skip tool={} path={} reason=no_retrieve_tool — 回退原文",
            tool_name, path,
        )
        return original

    h = await store_entry(
        session_id=session_id,
        agent_id=agent_id,
        content=original,
        tool_name=tool_name,
        path=path,
        original_tokens=before,
        compressed_tokens=after,
    )
    if not h:
        incr_ccr_metric("gate_skip_store_failed")
        logger.info(
            "[CTX-CCR] gate skip tool={} path={} reason=store_failed — 回退原文",
            tool_name, path,
        )
        record_compression_event(
            tool_name=tool_name, strategy=comp_result.strategy,
            lossiness=str(comp_result.lossiness), original_tokens=before,
            final_tokens=before, store_ok=False,
        )
        return original

    try:
        from .context_tracker import track_session_compression
        track_session_compression(
            session_id=session_id,
            hash_key=h,
            tool_name=tool_name,
            sample_content=original,
            workspace_key=str(agent_id) if agent_id else "",
        )
    except Exception as _trk_err:
        logger.debug("[CTX-TRACKER] track failed: {}", _trk_err)

    final = emit_guarded(comp_result.content, ccr_marker(h), original, model_name, ctx_path=path)
    if final == original:
        incr_ccr_metric("gate_skip_never_worse_after_store")
        logger.info(
            "[CTX-CCR] gate skip tool={} path={} reason=never_worse_after_store hash={}",
            tool_name, path, h[:12],
        )
        record_compression_event(
            tool_name=tool_name, strategy=comp_result.strategy,
            lossiness=str(comp_result.lossiness), original_tokens=before,
            final_tokens=before, store_ok=True,
        )
        return original

    record_compression_event(
        tool_name=tool_name, strategy=comp_result.strategy,
        lossiness=str(comp_result.lossiness), original_tokens=before,
        final_tokens=_est_tokens_str(final, model_name), store_ok=True,
    )
    logger.info(
        "[CTX-COMPRESS] path={} tool={} strategy={} lossiness={} tokens={}→{} hash={}",
        path, tool_name, comp_result.strategy, comp_result.lossiness, before,
        _est_tokens_str(final, model_name), h[:12],
    )
    return final


async def _hard_ceil_with_ccr(
    original: str,
    tool_name: str,
    model_name: str,
    path: str,
    session_id: str,
    agent_id,
    *,
    tools_available: bool,
) -> str:
    """硬天花板兜底也必须尽量可逆：先归档原文，再输出带 CCR marker 的 head/tail。"""
    if len(original) <= _TOOL_HARD_CEIL_CHARS:
        return original

    sid = _session_tag(session_id)
    before_tok = _est_tokens_str(original, model_name)

    if not tools_available or not session_id:
        reason = "no_retrieve_tool" if not tools_available else "empty_session"
        incr_ccr_metric("hard_ceil_irreversible")
        logger.warning(
            "[CTX-CCR] hard_ceil gate skip tool={} path={} reason={} chars={} — 回退原文",
            tool_name, path, reason, len(original),
        )
        return original

    if "<!-- ccr:" in original:
        return original

    from app.services.llm.ccr_store import ccr_marker, incr_ccr_metric, record_compression_event, store_entry

    marker = ""
    try:
        h = await store_entry(
            session_id=session_id,
            agent_id=agent_id,
            content=original,
            tool_name=tool_name,
            path=path,
            original_tokens=before_tok,
            compressed_tokens=max(1, _TOOL_HARD_CEIL_CHARS // 4),
        )
        if h:
            incr_ccr_metric("hard_ceil_store_ok")
            marker = ccr_marker(h)
            try:
                from .context_tracker import track_session_compression
                track_session_compression(
                    session_id=session_id,
                    hash_key=h,
                    tool_name=tool_name,
                    sample_content=original,
                    workspace_key=str(agent_id) if agent_id else "",
                )
            except Exception as _trk_err:
                logger.debug("[CTX-TRACKER] hard_ceil track failed: {}", _trk_err)
            logger.info(
                "[CTX-CCR] hard_ceil store tool={} path={} hash={} chars={} session={}",
                tool_name, path, h[:12], len(original), sid,
            )
        else:
            incr_ccr_metric("hard_ceil_store_fail")
            incr_ccr_metric("hard_ceil_irreversible")
            logger.warning(
                "[CTX-CCR] hard_ceil irreversible tool={} path={} reason=store_failed chars={} session={} — 回退原文",
                tool_name, path, len(original), sid,
            )
            return original
    except Exception as e:
        incr_ccr_metric("hard_ceil_store_error")
        incr_ccr_metric("hard_ceil_irreversible")
        logger.warning(
            "[CTX-CCR] hard_ceil irreversible tool={} path={} reason=store_error err={} session={} — 回退原文",
            tool_name, path, e, sid,
        )
        return original

    budget = max(1024, _TOOL_HARD_CEIL_CHARS - len(marker) - 1)
    clipped = _hard_head_tail(original, max_chars=budget)
    final = marker + "\n" + clipped
    record_compression_event(
        tool_name=tool_name,
        strategy="hard_ceil",
        lossiness=str(Lossiness.HARD_CEIL),
        original_tokens=before_tok,
        final_tokens=_est_tokens_str(final, model_name),
        store_ok=True,
    )
    return final


def _tools_include_retrieve_context(tools_for_llm: list[dict] | None) -> bool:
    try:
        from app.services.llm.ccr_store import RETRIEVE_CONTEXT_TOOL_NAME
    except Exception:
        return False
    return any(
        (tool.get("function") or {}).get("name") == RETRIEVE_CONTEXT_TOOL_NAME
        for tool in (tools_for_llm or [])
    )


def _sanitize_tool_calls_for_context(tool_calls: list[dict]) -> tuple[list[dict] | None, str | None]:
    """Return OpenAI-compatible tool calls, or a retry instruction if args are invalid."""
    sanitized: list[dict] = []
    for tc in tool_calls:
        fn = tc.get("function") or {}
        tool_name = fn.get("name") or ""
        raw_args = fn.get("arguments", "{}")

        if raw_args is None or raw_args == "":
            args_str = "{}"
        elif isinstance(raw_args, str):
            try:
                json.loads(raw_args)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "[LLM] Invalid tool arguments JSON for {}: {} at pos {}",
                    tool_name or "<unknown>",
                    exc.msg,
                    exc.pos,
                )
                return None, (
                    "Your previous tool call arguments were not valid JSON. "
                    f"The affected tool was `{tool_name or 'unknown'}`. "
                    "Retry the tool call now with `function.arguments` as one valid JSON object string. "
                    "Escape all quotes and newlines inside long HTML, CSS, JavaScript, or markdown content. "
                    "Do not explain; only retry with a valid tool call."
                )
            args_str = raw_args
        elif isinstance(raw_args, (dict, list)):
            args_str = json.dumps(raw_args, ensure_ascii=False)
        else:
            return None, (
                "Your previous tool call arguments had an unsupported type. "
                f"The affected tool was `{tool_name or 'unknown'}`. "
                "Retry the tool call with `function.arguments` as one valid JSON object string."
            )

        new_tc = {
            "id": tc.get("id", ""),
            "type": tc.get("type") or "function",
            "function": {
                "name": tool_name,
                "arguments": args_str,
            },
        }
        if "_gemini_extra" in tc:
            new_tc["_gemini_extra"] = tc["_gemini_extra"]
        sanitized.append(new_tc)

    return sanitized, None


# ═══════════════════════════════════════════════════════════════════════════════
# Failover Guard
# ═══════════════════════════════════════════════════════════════════════════════

class FailoverGuard:
    """Guard state for failover decisions."""

    def __init__(self):
        self.tool_executed = False
        self.streaming_started = False
        self.failover_done = False

    def mark_tool_executed(self):
        """Mark that a side-effecting tool has been executed."""
        self.tool_executed = True

    def mark_streaming_started(self):
        """Mark that streaming output has started."""
        self.streaming_started = True

    def mark_failover_done(self):
        """Mark that failover has already happened once."""
        self.failover_done = True

    def can_failover(self) -> bool:
        """Check if failover is allowed based on guard rules."""
        if self.failover_done:
            return False  # Only failover once
        if self.tool_executed:
            return False  # Don't failover after side effects
        if self.streaming_started:
            return False  # Don't failover after streaming started
        return True


def is_retryable_error(result: str) -> bool:
    """Check if an error result is retryable.
    
    Uses unified classification from failover.py.
    """
    if not (result.startswith("[LLM Error]") or result.startswith("[LLM call error]") or result.startswith("[Error]")):
        return False
        
    return classify_error(Exception(result)) != FailoverErrorType.NON_RETRYABLE


def _format_friendly_error(error_text: str) -> str:
    """将 LLM 原始错误转换为用户可理解的中文提示。

    按优先级匹配错误模式，返回带 emoji 的中文提示。
    无法识别时返回简短摘要。
    """
    text_lower = error_text.lower()

    patterns: list[tuple[str, str]] = [
        (r'insufficient\s*balance', '💰 API 余额不足，请前往控制台充值后重试。'),
        (r'invalid.*api[\s_-]*key|unauthorized', '🔑 API 密钥无效或已过期，请检查 API 设置。'),
        (r'rate[\s_-]*limit|too\s*many\s*request', '⏳ 请求过于频繁，请稍后重试。'),
        (r'connection[\s_-]*refused', '🔌 无法连接到模型服务，请检查网络或服务状态。'),
        (r'time[-\s]?out|timed\s*out', '⏱️ 模型响应超时，请稍后重试。'),
        (r'context[\s_-]*length[\s_-]*exceed', '📏 上下文超过模型限制，请缩短对话或减少附加内容。'),
        (r'internal[\s_-]*server[\s_-]*error|internal\s+error', '🖥️ 模型服务暂时异常，请稍后重试或联系管理员。'),
        (r'invalid_request_error|tool_calls.*must\s+be\s+followed|tool.*must\s+be\s+a\s+response',
         '🔄 上下文消息格式异常（历史过长被截断导致工具调用配对丢失），请开启新会话重试。'),
    ]

    for pattern, friendly in patterns:
        if re.search(pattern, text_lower):
            return friendly

    status_match = re.search(r'HTTP\s+(\d{3})', error_text)
    if status_match and status_match.group(1).startswith('5'):
        return f'🖥️ 模型服务暂时异常（HTTP {status_match.group(1)}），请稍后重试或联系管理员。'

    short = error_text[:500].replace('\n', ' ')
    return f'⚠️ 调用模型出错，请联系管理员。错误摘要: {short}'


def _get_model_timeout(model: "LLMModel") -> float:
    """Return the effective request timeout for a model."""
    return float(getattr(model, "request_timeout", None) or 120.0)


def _usage_from_response_or_estimate(response, api_messages: list[LLMMessage]) -> TokenUsage:
    usage = extract_token_usage(response.usage)
    if usage:
        return usage
    round_chars = sum(len(m.content or '') if isinstance(m.content, str) else 0 for m in api_messages)
    round_chars += len(response.content or '')
    return estimate_token_usage_from_chars(round_chars)


# ═══════════════════════════════════════════════════════════════════════════════
# Helper Functions
# ═══════════════════════════════════════════════════════════════════════════════

async def _get_agent_config(agent_id) -> tuple[int, str | None]:
    """Get agent config: max_tool_rounds and token limit status."""
    if not agent_id:
        return 50, None

    try:
        from app.models.agent import Agent as AgentModel
        async with async_session() as _db:
            _ar = await _db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            _agent = _ar.scalar_one_or_none()
            if _agent:
                max_rounds = _agent.max_tool_rounds or 50
                if _agent.max_tokens_per_day and _agent.tokens_used_today >= _agent.max_tokens_per_day:
                    return max_rounds, f"⚠️ Daily token usage has reached the limit ({_agent.tokens_used_today:,}/{_agent.max_tokens_per_day:,}). Please try again tomorrow or ask admin to increase the limit."
                if _agent.max_tokens_per_month and _agent.tokens_used_month >= _agent.max_tokens_per_month:
                    return max_rounds, f"⚠️ Monthly token usage has reached the limit ({_agent.tokens_used_month:,}/{_agent.max_tokens_per_month:,}). Please ask admin to increase the limit."
                return max_rounds, None
    except Exception:
        pass
    return 50, None


async def _get_user_name(user_id) -> str | None:
    """Get user's display name for personalized context."""
    if not user_id:
        return None
    try:
        from app.models.user import User as _UserModel
        from app.models.agent import Agent as _AgentModel
        async with async_session() as _udb:
            _ur = await _udb.execute(select(_UserModel).where(_UserModel.id == user_id))
            _u = _ur.scalar_one_or_none()
            if _u:
                return _u.display_name or _u.username
            # Check Agent name fallback
            _ar = await _udb.execute(select(_AgentModel).where(_AgentModel.id == user_id))
            _a = _ar.scalar_one_or_none()
            if _a:
                return _a.name
    except Exception:
        pass
    return None


def _convert_messages_for_vision(
    api_messages: list, supports_vision: bool
) -> list:
    """Convert image markers to vision format if supported, or strip them."""
    import re as _re_v
    import copy

    # Deep copy to avoid modifying the original list in place
    new_messages = copy.deepcopy(api_messages)

    if supports_vision:
        # Vision format: convert image markers in strings to OpenAI Vision API list format
        for i, msg in enumerate(new_messages):
            if msg.role != "user" or not msg.content or not isinstance(msg.content, str):
                continue
            
            content_str = msg.content
            pattern = r'\[image_data:(data:image/[^;]+;base64,[A-Za-z0-9+/=]+)\]'
            images = _re_v.findall(pattern, content_str)
            
            if not images:
                continue

            text = _re_v.sub(pattern, '', content_str).strip()
            parts = [{"type": "image_url", "image_url": {"url": img}} for img in images]
            if text:
                # Per OpenAI spec, text part should come after image parts
                parts.append({"type": "text", "text": text})
            
            new_messages[i] = type(msg)(role=msg.role, content=parts, tool_calls=msg.tool_calls, tool_call_id=msg.tool_call_id)
    else:
        # Non-vision format: ensure content is a string for all roles, stripping image data.
        _img_marker_pattern = r'\[image_data:data:image/[^;]+;base64,[A-Za-z0-9+/=]+\]'
        for i, msg in enumerate(new_messages):
            
            if isinstance(msg.content, list):
                # It's a list, join all text parts. This handles user messages
                # with vision content and tool messages from vision_inject.
                text_parts = [part.get("text", "") for part in msg.content if part.get("type") == "text"]
                content_str = "\n".join(text_parts).strip()
                new_messages[i] = type(msg)(role=msg.role, content=content_str, tool_calls=msg.tool_calls, tool_call_id=msg.tool_call_id)

            elif isinstance(msg.content, str) and "[image_data:" in msg.content:
                # It's a string with image markers, strip them
                _n_imgs = len(_re_v.findall(_img_marker_pattern, msg.content))
                cleaned = _re_v.sub(_img_marker_pattern, '', msg.content).strip()
                if _n_imgs > 0:
                    cleaned += f"\n[用户发送了 {_n_imgs} 张图片，但当前模型不支持视觉，无法查看图片内容]"
                new_messages[i] = type(msg)(role=msg.role, content=cleaned, tool_calls=msg.tool_calls, tool_call_id=msg.tool_call_id)

    return new_messages


def _check_tool_requires_args(tool_name: str, args: dict) -> tuple[bool, str]:
    """Check if tool requires arguments and return (should_execute, result_or_error)."""
    if not args and tool_name in TOOLS_REQUIRING_ARGS:
        return False, f"Error: {tool_name} was called with empty arguments. You must provide the required parameters. Please retry with the correct arguments."
    return True, ""


def _allowed_tool_names(tools_for_llm: list[dict] | None) -> set[str]:
    names: set[str] = set()
    for tool in tools_for_llm or []:
        name = ((tool.get("function") or {}).get("name") or "").strip()
        if name:
            names.add(name)
    return names


def _tool_not_enabled_message(tool_name: str) -> str:
    return (
        f"Tool `{tool_name}` is not enabled for this agent. "
        "Do not call it again. Use only the tools currently available to you, "
        "or explain that the required capability is not enabled."
    )


_ABS_PATH_RE = re.compile(r"/(?:Users|home|var|tmp|opt|Volumes)/\S+")


def _redact_log_string(value: str, limit: int = 300) -> str:
    """日志脱敏：截断绝对路径，避免 execute_command 泄露宿主机目录（M1）。"""
    if not isinstance(value, str) or not value:
        return value
    redacted = _ABS_PATH_RE.sub("<PATH>", value)
    redacted = re.sub(
        r"\bcd\s+/[^\s;&|]+",
        "cd <PATH>",
        redacted,
    )
    return redacted[:limit]


_TOOL_ARG_SECRET_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "client_secret",
}


def _last_user_text(messages: list) -> str:
    for msg in reversed(messages or []):
        role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)
        if role != "user":
            continue
        content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", "")
        if isinstance(content, str):
            return content[-2000:]
        if isinstance(content, list):
            parts = [
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            return "\n".join(parts)[-2000:]
    return ""


def _summarize_tool_args(args: dict, limit: int = 300) -> str:
    """仅保留标量参数，屏蔽 secret-like key，避免相关性 query 泄露凭据/绝对路径（M1）。"""
    if not isinstance(args, dict):
        return ""
    fields: list[str] = []
    for key in sorted(args):
        key_text = str(key)
        if key_text.lower() in _TOOL_ARG_SECRET_KEYS:
            continue
        value = args.get(key)
        if isinstance(value, str):
            fields.append(f"{key_text}={_redact_log_string(value, limit=limit)}")
        elif isinstance(value, (int, float, bool)) or value is None:
            fields.append(f"{key_text}={value}")
        if sum(len(item) for item in fields) >= limit:
            break
    return "; ".join(fields)[:limit]


@dataclass
class ToolCallResult:
    """单条 tool 执行结果 — 调度器统一写入 api_messages，避免并行 task 竞态。"""

    tool_call_id: str
    tool_name: str
    content: str | list | None = None
    error: str = ""
    elapsed_ms: float = 0.0  # 可变字段，gather 后由调度器填充


def _apply_tool_result(result: ToolCallResult, api_messages: list) -> str:
    """将 ToolCallResult 追加到 api_messages；返回非空 error 供 legacy 分支兼容。"""
    if result.error:
        api_messages.append(LLMMessage(role="tool", tool_call_id=result.tool_call_id, content=result.error))
        return result.error
    if result.content is not None:
        api_messages.append(LLMMessage(role="tool", tool_call_id=result.tool_call_id, content=result.content))
    return ""


async def _process_tool_call(
    tc: dict,
    api_messages: list,
    agent_id,
    user_id,
    session_id: str,
    supports_vision: bool,
    on_tool_call,
    full_reasoning_content: str,
    allowed_tool_names: set[str],
    on_code_output=None,
    model_name: str = "",
    ctx_window: int = 100000,
    compress_enabled: bool = True,
    ctx_path: str = "",
    retrieve_tool_available: bool = True,
    round_i: int = 0,
    layer0_budget_scale: float = 1.0,
) -> ToolCallResult:
    """Process a single tool call; 由调度器统一 append api_messages。"""
    fn = tc["function"]
    tool_name = fn["name"]
    raw_args = fn.get("arguments", "{}")

    try:
        args = json.loads(raw_args) if raw_args else {}
    except json.JSONDecodeError:
        args = {}
    _log_tool_call_args(tool_name, raw_args, args)
    user_query = _last_user_text(api_messages)
    tool_args_text = _summarize_tool_args(args)

    # Guard: check if tool requires arguments
    should_execute, error_msg = _check_tool_requires_args(tool_name, args)
    if not should_execute:
        return ToolCallResult(tool_call_id=tc.get("id", ""), tool_name=tool_name, error=error_msg)

    # if tool_name not in allowed_tool_names:
    #     result = _tool_not_enabled_message(tool_name)
    #     logger.warning(f"[LLM] Blocked disabled tool call: {tool_name} agent_id={agent_id}")
    #     if on_tool_call:
    #         try:
    #             await on_tool_call({
    #                 "name": tool_name,
    #                 "call_id": tc.get("id", ""),
    #                 "args": args,
    #                 "status": "done",
    #                 "result": result,
    #                 "reasoning_content": full_reasoning_content
    #             })
    #         except Exception:
    #             pass
    #     api_messages.append(LLMMessage(
    #         role="tool",
    #         tool_call_id=tc["id"],
    #         content=result,
    #     ))
    #     return ""

    # Notify client about tool call (in-progress)
    if on_tool_call:
        try:
            await on_tool_call({
                "name": tool_name,
                "call_id": tc.get("id", ""),
                "args": args,
                "status": "running",
                "reasoning_content": full_reasoning_content
            })
        except Exception as _cb_err:
            logger.warning("[LLM] on_tool_call 回调失败: {}", _cb_err)  # pass on_output for execute_code streaming
    _on_output = on_code_output if tool_name in ("execute_code", "execute_code_e2b") else None
    if tool_name in WORKSPACE_WRITE_TOOLS:
        async with workspace_write_lock(session_id):
            result = await execute_tool(
                tool_name, args,
                agent_id=agent_id,
                user_id=user_id or agent_id,
                session_id=session_id,
                on_output=_on_output,
            )
    else:
        result = await execute_tool(
            tool_name, args,
            agent_id=agent_id,
            user_id=user_id or agent_id,
            session_id=session_id,
            on_output=_on_output,
        )
    logger.debug(f"[LLM] Tool result: {result[:100]}")

    # ── Vision injection for screenshot tools ──
    tool_content: str | list = str(result)
    if supports_vision and agent_id:
        try:
            from app.services.vision_inject import try_inject_screenshot_vision
            settings = get_settings()
            ws_path = Path(settings.STORAGE_LOCAL_ROOT or settings.AGENT_DATA_DIR) / str(agent_id)
            vision_content = try_inject_screenshot_vision(tool_name, str(result), ws_path)
            if vision_content:
                tool_content = vision_content
                logger.info(f"[LLM] Injected screenshot vision for {tool_name}")
        except Exception as e:
            logger.warning(f"[LLM] Vision injection failed for {tool_name}: {e}")

    # Notify client about tool call result
    if on_tool_call:
        try:
            await on_tool_call({
                "name": tool_name,
                "call_id": tc.get("id", ""),
                "args": args,
                "status": "done",
                "result": result,
                "reasoning_content": full_reasoning_content
            })
        except Exception as _cb_err:
            logger.warning("[LLM] on_tool_call 回调失败: {}", _cb_err)

    # Layer 0：工具结果注入期压缩（所有经 call_llm 的路径统一在此收口）。
    # - vision list（screenshot 注入）保留原结构，不做字符串压缩（Blocker #4）
    # - 已含 _COMPRESS_MARKER（执行期已压）则跳过，避免双压（Blocker #6）
    # - 触发按 token 预算，压缩走类型感知 _dispatch_guarded（never_worse 内建）
    # - 硬天花板始终生效，即使 flag 关闭也防单条巨型结果 OOM
    _path = ctx_path or _resolve_ctx_path()
    _session_pressure = min(1.0, _est_tokens(api_messages, model_name) / max(ctx_window, 1))
    # retrieve_context 取回的原文（含 <!-- ccr:retrieved -->）必须保持 verbatim，跳过压缩
    if isinstance(tool_content, str) and _COMPRESS_MARKER not in tool_content and "<!-- ccr:retrieved -->" not in tool_content:
        if compress_enabled:
            _base_budget = _tool_token_budget(tool_name, ctx_window)
            _budget = max(
                64,
                int(
                    _effective_tool_budget(
                        tool_name,
                        ctx_window,
                        round_i=round_i,
                        session_pressure=_session_pressure,
                    )
                    * max(layer0_budget_scale, 0.25)
                ),
            )
            _tool_tok = _est_tokens_str(tool_content, model_name)
            if _budget < _base_budget:
                logger.info(
                    "[CTX-F6] path={} tool={} round={} budget={}→{} session_p={:.2f} session={}",
                    _path,
                    tool_name,
                    round_i + 1,
                    _base_budget,
                    _budget,
                    _session_pressure,
                    _session_tag(session_id),
                )
            if _tool_tok > _budget:
                tool_content = await _guarded_compress_with_ccr(
                    tool_content, tool_name, _budget, model_name, _path, session_id, agent_id,
                    tools_available=retrieve_tool_available, ctx_window=ctx_window,
                    user_query=user_query, tool_args_text=tool_args_text,
                    session_pressure=_session_pressure,
                )
            else:
                logger.info(
                    "[CTX-COMPRESS] path={} tool={} skip reason=under_budget tokens={} budget={} chars={} session={}",
                    _path, tool_name, _tool_tok, _budget, len(tool_content), _session_tag(session_id),
                )
        elif isinstance(tool_content, str) and len(tool_content) > _TOOL_HARD_CEIL_CHARS:
            logger.info(
                "[CTX-COMPRESS] path={} tool={} skip reason=compress_disabled chars={} session={}",
                _path, tool_name, len(tool_content), _session_tag(session_id),
            )
        if len(tool_content) > _TOOL_HARD_CEIL_CHARS:
            _before_ceil = len(tool_content)
            tool_content = await _hard_ceil_with_ccr(
                tool_content,
                tool_name,
                model_name,
                _path,
                session_id,
                agent_id,
                tools_available=retrieve_tool_available,
            )
            logger.info(
                "[CTX] path={} tool={} hard_ceil {}→{} chars session={}",
                _path, tool_name, _before_ceil, len(tool_content), _session_tag(session_id),
            )

    return ToolCallResult(
        tool_call_id=tc.get("id", ""),
        tool_name=tool_name,
        content=tool_content,
    )




async def _execute_tool_round(
    sanitized_tool_calls: list[dict],
    *,
    api_messages: list,
    agent_id,
    user_id,
    session_id: str,
    supports_vision: bool,
    on_tool_call,
    on_code_output,
    full_reasoning_content: str,
    allowed_tool_names: set[str],
    model_name: str,
    ctx_window: int,
    compress_enabled: bool,
    ctx_path: str,
    retrieve_tool_available: bool,
    round_i: int,
    layer0_budget_scale: float = 1.0,
) -> float:
    """执行一轮 tool_calls：只读批可 asyncio.gather，写/副作用串行。"""
    _round_total_tool_ms = 0.0
    batches = partition_tool_calls(list(sanitized_tool_calls or []))

    async def _run_one_safe(tc: dict) -> ToolCallResult:
        tool_name = (tc.get("function") or {}).get("name", "?")
        tool_call_id = tc.get("id", "")
        try:
            from app.plugins.clawith_acp.turn_budget import (
                default_tool_timeout_seconds,
                get_turn_budget,
            )

            _t0 = time.perf_counter()
            _tool_timeout = default_tool_timeout_seconds()
            _budget = get_turn_budget()
            if _budget is not None:
                _tool_timeout = _budget.cap_timeout(_tool_timeout)

            async def _invoke() -> ToolCallResult:
                return await _process_tool_call(
                    tc=tc,
                    api_messages=api_messages,
                    agent_id=agent_id,
                    user_id=user_id,
                    session_id=session_id,
                    supports_vision=supports_vision,
                    on_tool_call=on_tool_call,
                    on_code_output=on_code_output,
                    full_reasoning_content=full_reasoning_content,
                    allowed_tool_names=allowed_tool_names,
                    model_name=model_name,
                    ctx_window=ctx_window,
                    compress_enabled=compress_enabled,
                    ctx_path=ctx_path,
                    retrieve_tool_available=retrieve_tool_available,
                    round_i=round_i,
                    layer0_budget_scale=layer0_budget_scale,
                )

            try:
                result = await asyncio.wait_for(_invoke(), timeout=_tool_timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "[ACP-BUDGET] tool_timeout name={} call_id={} timeout_s={} session={}",
                    tool_name,
                    tool_call_id,
                    _tool_timeout,
                    _session_tag(session_id),
                )
                return ToolCallResult(
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    error=f"Error: tool execution timed out after {int(_tool_timeout)}s",
                )
            result.elapsed_ms = (time.perf_counter() - _t0) * 1000
            return result
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            logger.error(
                "[LLM-PARALLEL] tool failed name={} call_id={} err={} session={}",
                tool_name,
                tool_call_id,
                exc,
                _session_tag(session_id),
            )
            return ToolCallResult(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                error=f"Error: tool execution failed: {exc}",
            )

    async def _run_with_limits(tc: dict, global_sem: asyncio.Semaphore) -> ToolCallResult:
        async with global_sem:
            name = (tc.get("function") or {}).get("name", "")
            rsem = rate_limit_sem(name)
            if rsem is not None:
                async with rsem:
                    return await _run_one_safe(tc)
            return await _run_one_safe(tc)

    for batch in batches:
        if batch.mode == ToolExecutionMode.PARALLEL:
            width = min(len(batch.calls), max_parallel_concurrency())
            logger.info(
                "[LLM-PARALLEL] round={} batch_size={} width={} session={}",
                round_i + 1,
                len(batch.calls),
                width,
                _session_tag(session_id),
            )
            global_sem = asyncio.Semaphore(width)
            _batch_t0 = time.perf_counter()
            ctx = contextvars.copy_context()
            tasks = [
                ctx.run(asyncio.create_task, _run_with_limits(tc, global_sem))
                for tc in batch.calls
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            _batch_wall_ms = (time.perf_counter() - _batch_t0) * 1000
            _batch_sum_ms = 0.0
            for tc, item in zip(batch.calls, results):
                if isinstance(item, BaseException):
                    if isinstance(item, asyncio.CancelledError):
                        raise item
                    tool_name = (tc.get("function") or {}).get("name", "?")
                    logger.error(
                        "[LLM-PARALLEL] gather err name={} call_id={} err={} session={}",
                        tool_name,
                        tc.get("id", ""),
                        item,
                        _session_tag(session_id),
                    )
                    _apply_tool_result(
                        ToolCallResult(
                            tool_call_id=tc.get("id", ""),
                            tool_name=tool_name,
                            error=f"Error: tool execution failed: {item}",
                        ),
                        api_messages,
                    )
                    continue
                _apply_tool_result(item, api_messages)
                _batch_sum_ms += item.elapsed_ms
                _round_total_tool_ms += item.elapsed_ms
                if item.elapsed_ms > 500:
                    logger.warning(
                        f"[LLM] 慢工具: {item.tool_name} elapsed={item.elapsed_ms:.0f}ms "
                        f"round={round_i+1} session={session_id}"
                    )
            if len(batch.calls) > 1:
                debug_session_log(
                    "H2",
                    "caller.py:_execute_tool_round",
                    "parallel_batch_timing",
                    {
                        "round": round_i + 1,
                        "batch_size": len(batch.calls),
                        "wall_ms": round(_batch_wall_ms, 1),
                        "sum_ms": round(_batch_sum_ms, 1),
                        "saved_ms": round(max(0.0, _batch_sum_ms - _batch_wall_ms), 1),
                        "session": _session_tag(session_id),
                    },
                )
                logger.info(
                    "[LLM-PARALLEL] round={} chunk={} wall_ms={:.0f} sum_ms={:.0f} saved_ms={:.0f} session={}",
                    round_i + 1,
                    len(batch.calls),
                    _batch_wall_ms,
                    _batch_sum_ms,
                    max(0.0, _batch_sum_ms - _batch_wall_ms),
                    _session_tag(session_id),
                )
        else:
            for tc in batch.calls:
                result = await _run_one_safe(tc)
                _apply_tool_result(result, api_messages)
                _round_total_tool_ms += result.elapsed_ms
                if result.elapsed_ms > 500:
                    logger.warning(
                        f"[LLM] 慢工具: {result.tool_name} elapsed={result.elapsed_ms:.0f}ms "
                        f"round={round_i+1} session={session_id}"
                    )

    return _round_total_tool_ms


# ═══════════════════════════════════════════════════════════════════════════════
# Core LLM Call Functions
# ═══════════════════════════════════════════════════════════════════════════════

async def call_llm(
    model: LLMModel,
    messages: list[dict],
    agent_name: str,
    role_description: str,
    agent_id=None,
    user_id=None,
    session_id: str = "",
    on_chunk=None,
    on_tool_call=None,
    on_tool_delta=None,
    on_thinking=None,
    supports_vision=False,
    max_tool_rounds_override: int | None = None,
    skip_tools: bool = False,
    on_code_output=None,
    current_user_name_override: str | None = None,
    system_prompt_suffix: str | None = None,
    cancel_event: asyncio.Event | None = None,
) -> str:
    """Call LLM via unified client with function-calling tool loop.

    参数 cancel_event: 当外部设置此事件时，工具循环将在下一轮开始前抛出 CancelledError，
    实现 ACP 协议的 prompt 取消机制。"""

    # Get agent config for tool rounds
    _max_tool_rounds, _token_limit_msg = await _get_agent_config(agent_id)
    if _token_limit_msg:
        return _token_limit_msg
    if max_tool_rounds_override and max_tool_rounds_override < _max_tool_rounds:
        _max_tool_rounds = max_tool_rounds_override

    # Get user's name for personalized context
    if current_user_name_override:
        _user_name = current_user_name_override
    else:
        _user_name = await _get_user_name(user_id)

    # Auto-assign fallback tool call logger if none provided but conversation context exists
    if on_tool_call is None and session_id:
        from app.services.chat_session_service import save_tool_call_log
        async def _default_on_tool_call(data: dict):
            if data.get("status") == "done" and agent_id:
                await save_tool_call_log(
                    agent_id=agent_id,
                    user_id=user_id or agent_id,
                    conversation_id=session_id,
                    tool_name=data.get("name", ""),
                    arguments=data.get("args"),
                    result=data.get("result"),
                    status="done",
                    tool_call_id=data.get("call_id"),
                    reasoning_content=data.get("reasoning_content"),
                )
        on_tool_call = _default_on_tool_call

    if agent_id:
        try:
            from app.services.focus_service import ensure_focus_migrated
            await ensure_focus_migrated(agent_id)
        except Exception as _focus_err:
            logger.warning("[FOCUS] bootstrap migrate failed: {}", _focus_err)

    # Build rich prompt with soul, memory, skills, relationships
    from app.services.agent_context import build_agent_context
    # Look up current user's display name so the agent knows who it's talking to
    static_prompt, dynamic_prompt = await build_agent_context(agent_id, agent_name, role_description, current_user_name=_user_name)
    if system_prompt_suffix:
        dynamic_prompt += system_prompt_suffix

    # Load tools dynamically from DB. `skip_tools=True` is set by the WS
    # handler on the onboarding greeting turn; keep the runtime-level `finish`
    # tool available so every turn still has an explicit stop signal.
    if skip_tools:
        tools_for_llm = [FINISH_TOOL_DEFINITION]
    else:
        from app.services.agent_tools import AGENT_TOOLS
        tools_for_llm = await get_agent_tools_for_llm(agent_id) if agent_id else AGENT_TOOLS
    # CCR ready：主工具循环必须先具备 retrieve_context，首次有损压缩才可安全生成 marker。
    # 否则 tools_available=false 会让 CCR gate 回退原文，最后落到不可恢复 hard_ceil。
    if not skip_tools:
        try:
            from app.services.llm.stable_context import (
                build_ccr_system_appendix,
                get_retrieve_context_tool_definition,
                RETRIEVE_CONTEXT_TOOL_NAME,
            )
            CCR_SYSTEM_APPENDIX = build_ccr_system_appendix()
            RETRIEVE_CONTEXT_TOOL_DEFINITION = get_retrieve_context_tool_definition()
            if not any(
                (t.get("function") or {}).get("name") == RETRIEVE_CONTEXT_TOOL_NAME
                for t in tools_for_llm
            ):
                tools_for_llm = list(tools_for_llm) + [RETRIEVE_CONTEXT_TOOL_DEFINITION]
                logger.info("[CTX-CCR] ready inject retrieve_context session={}", _session_tag(session_id))
            if CCR_SYSTEM_APPENDIX not in dynamic_prompt:
                dynamic_prompt += CCR_SYSTEM_APPENDIX
        except Exception as _e:
            logger.warning("[CTX-CCR] ready inject failed: {}", _e)

    _last_user = ""
    for _m in reversed(messages):
        if _m.get("role") == "user" and _m.get("content"):
            _last_user = str(_m["content"])
            break

    try:
        from .context_tracker import build_proactive_hints
        if _last_user and session_id and agent_id:
            _hints = build_proactive_hints(session_id, str(agent_id), _last_user)
            if _hints and _hints not in dynamic_prompt:
                dynamic_prompt += _hints
    except Exception as _e:
        logger.warning("[CTX-TRACKER] proactive hints failed: {}", _e)

    try:
        from .output_shaper import build_output_shaping_suffix
        _model_label = getattr(model, "model_name", None) or getattr(model, "name", "") or ""
        _shaping = build_output_shaping_suffix(
            path=_resolve_ctx_path(),
            user_query=_last_user,
            recent_tool_count=sum(1 for m in messages if m.get("role") == "tool"),
            model_name=str(_model_label),
        )
        if _shaping and _shaping not in dynamic_prompt:
            dynamic_prompt += _shaping
    except Exception as _e:
        logger.warning("[CTX-SHAPER] inject failed: {}", _e)

    allowed_tool_names = _allowed_tool_names(tools_for_llm)

    # Convert messages to LLMMessage format
    api_messages = [LLMMessage(role="system", content=static_prompt, dynamic_content=dynamic_prompt)]
    for msg in messages:
        api_messages.append(LLMMessage(
            role=msg.get("role", "user"),
            content=msg.get("content"),
            tool_calls=msg.get("tool_calls"),
            tool_call_id=msg.get("tool_call_id"),
        ))

    # Vision format conversion
    api_messages = _convert_messages_for_vision(api_messages, supports_vision)

    # ── 上下文压缩会话初始化（Layer 1 轮次压缩 + Layer 0 工具预算）──
    _session_compressor = ContextCompressor(session_id=session_id)
    try:
        from .context_zones import PrefixCacheTracker
        _session_compressor.prefix_tracker = PrefixCacheTracker(
            min_frozen=int(get_settings().CTX_FROZEN_PREFIX_MSGS)
        )
    except Exception:
        pass
    _model_name = model.model
    _ctx_window = _get_ctx_guard_max_window(_model_name)
    _ctx_warn_ratio, _ctx_compress_ratio = _get_ctx_guard_ratios(_model_name)
    _ctx_compress_enabled = get_settings().CTX_COMPRESS_ENABLED
    _ctx_path = _resolve_ctx_path()
    logger.info(
        "[CTX] path={} session={} compress_enabled={} ctx_window={} model={}",
        _ctx_path, session_id, _ctx_compress_enabled, _ctx_window, _model_name,
    )
    if not _ctx_compress_enabled:
        logger.info(
            "[CTX-GUARD] path={} session={} compress_enabled=false (Layer1 透传, Layer0 硬顶保留)",
            _ctx_path, session_id,
        )
    # 首轮前修复历史中可能的孤儿 tool 链（截断/跨会话拼接导致）
    api_messages[:] = _repair_truncated_messages(api_messages)

    # Create the unified LLM client
    try:
        client = create_llm_client(
            provider=model.provider,
            api_key=get_model_api_key(model),
            model=model.model,
            base_url=model.base_url,
            timeout=_get_model_timeout(model),
        )
    except Exception as e:
        return f"[Error] Failed to create LLM client: {e}"

    max_tokens = get_max_tokens(model.provider, model.model, getattr(model, 'max_output_tokens', None))
    _accumulated_usage = TokenUsage()
    _unsaved_usage = TokenUsage()
    _last_cache_read = 0
    _last_cache_creation = 0

    # Tool-calling loop
    _session_t0 = time.perf_counter()
    for round_i in range(_max_tool_rounds):
        _round_t0 = time.perf_counter()
        # 检查取消信号 — ACP 协议的 prompt 取消机制
        if cancel_event and cancel_event.is_set():
            logger.info(f"[LLM] 收到取消信号，终止工具循环 round={round_i}")
            if agent_id and _unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _unsaved_usage)
            await client.close()
            raise asyncio.CancelledError("LLM call cancelled by cancel_event")

        # ACP turn 分层预算：compute / workflow（非 ACP 路径 get_turn_budget() 为 None）
        try:
            from app.plugins.clawith_acp.turn_budget import get_turn_budget

            _budget = get_turn_budget()
            if _budget is not None:
                _budget.check_workflow_or_raise()
                _budget.check_compute_or_raise()
        except Exception as _budget_exc:
            from app.plugins.clawith_acp.turn_budget import BudgetExceededError

            if isinstance(_budget_exc, BudgetExceededError):
                raise

        # Dynamic tool-call limit warning
        _warn_threshold_80 = int(_max_tool_rounds * 0.8)
        _warn_threshold_96 = _max_tool_rounds - 2
        if round_i == _warn_threshold_80:
            api_messages.append(LLMMessage(
                role="user",
                content=(
                    f"⚠️ 你已使用 {round_i}/{_max_tool_rounds} 轮工具调用。"
                    "如果当前任务尚未完成，请尽快使用 upsert_focus_item 保存进度，"
                    "并使用 set_trigger 设置续接触发器，在剩余轮次中做好收尾。"
                ),
            ))
        elif round_i == _warn_threshold_96:
            api_messages.append(LLMMessage(
                role="user",
                content="🚨 仅剩 2 轮工具调用。请立即使用 upsert_focus_item 保存进度并设置续接触发器。",
            ))

        # Check token usage limit mid-loop (every 3 rounds)
        if round_i > 0 and round_i % 3 == 0:
            if agent_id and _unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _unsaved_usage)
                _unsaved_usage = TokenUsage()
                _, _token_limit_msg = await _get_agent_config(agent_id)
                if _token_limit_msg:
                    logger.warning(f"[LLM] Token limit exceeded mid-loop: {_token_limit_msg}")
                    await client.close()
                    return _token_limit_msg

        # ── CTX-GUARD：每轮 stream 前按 token 预算检查并压缩历史（Layer 1）──
        if _ctx_compress_enabled and api_messages:
            _cur_tokens = _est_tokens(api_messages, _model_name)
            if _cur_tokens > _ctx_window * _ctx_warn_ratio:
                logger.warning(
                    "[CTX-GUARD] path={} session={} tokens={}/{} ({:.0%}) round={}",
                    _ctx_path, session_id, _cur_tokens, _ctx_window, _cur_tokens / _ctx_window, round_i + 1,
                )
            _pre_pressure = _cur_tokens / max(_ctx_window, 1)
            _retrieve_avail = _tools_include_retrieve_context(tools_for_llm)
            _pre_budget = pre_round_budget(
                _pre_pressure,
                retrieve_avail=_retrieve_avail,
                cache_read=_last_cache_read,
                round_i=round_i,
                last_fold_round=int(getattr(_session_compressor, "_last_fold_round", -100)),
            )
            if _pre_budget.should_enter:
                await _apply_pre_round_context(
                    api_messages,
                    ctx_window=_ctx_window,
                    model_name=_model_name,
                    session_id=session_id,
                    agent_id=agent_id,
                    ctx_path=_ctx_path,
                    tools_for_llm=tools_for_llm,
                    round_i=round_i,
                    compressor=_session_compressor,
                    last_cache_read=_last_cache_read,
                    last_cache_creation=_last_cache_creation,
                )
                _after_tokens = _est_tokens(api_messages, _model_name)
                logger.info(
                    "[CTX-BUDGET] path={} session={} compressed {}→{} tokens round={}",
                    _ctx_path, session_id, _cur_tokens, _after_tokens, round_i + 1,
                )

        try:
            # Use streaming API for real-time responses
            # on_chunk 由调用方传入（WebSocket/ACP 路径需要流式输出），
            # 若未传入则使用空回调，文本仅通过 finish(content=...) 返回
            async def _noop_chunk(_text: str) -> None:
                return None
            _stream_chunk = on_chunk if on_chunk is not None else _noop_chunk

            response = await client.stream(
                messages=api_messages,
                tools=tools_for_llm if tools_for_llm else None,
                temperature=model.temperature,
                max_tokens=max_tokens,
                on_chunk=_stream_chunk,
                on_tool_delta=on_tool_delta,
                on_thinking=on_thinking,
            )
        except LLMError as e:
            logger.error(f"[LLM] LLMError: provider={getattr(model, 'provider', '?')} model={getattr(model, 'model', '?')} {e}")
            if agent_id and _unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _unsaved_usage)
            await client.close()
            return f"[LLM Error] {e}"
        except Exception as e:
            logger.exception(f"[LLM] Unexpected error: {type(e).__name__}: {str(e)[:300]}")
            if agent_id and _unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _unsaved_usage)
            await client.close()
            return f"[LLM call error] {type(e).__name__}: {str(e)[:200]}"

        # Track tokens for this round
        _usage_this_round = _usage_from_response_or_estimate(response, api_messages)
        _accumulated_usage.add(_usage_this_round)
        _unsaved_usage.add(_usage_this_round)
        if _usage_this_round.cache_read_tokens or _usage_this_round.cache_creation_tokens:
            _last_cache_read = _usage_this_round.cache_read_tokens
            _last_cache_creation = _usage_this_round.cache_creation_tokens
            logger.info(
                "[CTX-CACHE] path={} session={} round={} cache_read={} cache_creation={}",
                _ctx_path, session_id, round_i + 1, _last_cache_read, _last_cache_creation,
            )

        # LLM 输出纯文本（无工具调用）即视为最终答案。
        # 对齐 OpenAI Agents SDK: 文本 + 无工具调用 = final_output，不强制 finish 工具。
        # 此修改同时修复两个问题：
        #   1. 流式文本重复 — 不再有 Round 2 通过 finish 重发相同的 chunk 到前端
        #   2. FAILOVER-CANCEL — 正常文本不再走 is_retryable_error 触发 Canceled 误导日志
        if not response.tool_calls:
            if response.content:
                api_messages.append(LLMMessage(role="assistant", content=response.content))
            if agent_id and _unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _unsaved_usage)
            await client.close()
            return response.content or ""

        # Execute tool calls
        if on_thinking and round_i >= 0:
            try:
                await on_thinking(f"第 {round_i + 1} 轮：执行工具…")
            except Exception:
                pass
        _session_elapsed = time.perf_counter() - _session_t0
        logger.info(
            f"[LLM] Round {round_i+1}: {len(response.tool_calls)} tool call(s) "
            f"session_elapsed={_session_elapsed:.1f}s"
        )
        sanitized_tool_calls, retry_instruction = _sanitize_tool_calls_for_context(response.tool_calls)
        if retry_instruction:
            api_messages.append(LLMMessage(role="user", content=retry_instruction))
            continue

        finish_call = find_finish_call(sanitized_tool_calls)
        if finish_call:
            if finish_call.valid:
                if agent_id and _unsaved_usage.total_tokens > 0:
                    await record_token_usage(agent_id, _unsaved_usage)
                await client.close()
                return finish_call.content

            api_messages.append(LLMMessage(
                role="assistant",
                content=response.content or None,
                tool_calls=sanitized_tool_calls,
                reasoning_content=response.reasoning_content,
            ))
            api_messages.append(LLMMessage(
                role="tool",
                content=finish_call.error or "`finish` was invalid.",
                tool_call_id=finish_call.call_id,
            ))
            continue

        # Add assistant message with tool calls
        api_messages.append(LLMMessage(
            role="assistant",
            content=response.content or None,
            tool_calls=sanitized_tool_calls,
            reasoning_content=response.reasoning_content,
        ))

        full_reasoning_content = response.reasoning_content or ""

        _round_total_tool_ms = await _execute_tool_round(
            sanitized_tool_calls,
            api_messages=api_messages,
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
            supports_vision=supports_vision,
            on_tool_call=on_tool_call,
            on_code_output=on_code_output,
            full_reasoning_content=full_reasoning_content,
            allowed_tool_names=allowed_tool_names,
            model_name=_model_name,
            ctx_window=_ctx_window,
            compress_enabled=_ctx_compress_enabled,
            ctx_path=_ctx_path,
            retrieve_tool_available=_tools_include_retrieve_context(tools_for_llm),
            round_i=round_i,
            layer0_budget_scale=float(getattr(_session_compressor, "layer0_budget_scale", 1.0)),
        )

        # F4：本轮工具结果追加后，对重复 read_file 结果做会话级去重（会话隔离，非模块全局）
        if _ctx_compress_enabled:
            _dedup_file_tool_results(
                api_messages, round_i=round_i, dedup_store=_session_compressor._dedup_seen,
            )
            _dedup_list_tool_results(
                api_messages, round_i=round_i, list_store=_session_compressor._list_seen,
            )

        _round_ms = (time.perf_counter() - _round_t0) * 1000
        logger.info(
            f"[LLM-PERF] Round {round_i+1} done: total={_round_ms:.0f}ms "
            f"tools={_round_total_tool_ms:.0f}ms llm_overhead={_round_ms - _round_total_tool_ms:.0f}ms "
            f"session_elapsed={time.perf_counter() - _session_t0:.1f}s"
        )

    # Record tokens even on "too many rounds" exit
    if agent_id and _unsaved_usage.total_tokens > 0:
        await record_token_usage(agent_id, _unsaved_usage)
    await client.close()
    return "[Error] Too many tool call rounds"


async def call_llm_with_failover(
    primary_model,
    fallback_model,
    messages: list[dict],
    agent_name: str,
    role_description: str,
    agent_id=None,
    user_id=None,
    session_id: str = "",
    on_chunk=None,
    on_thinking=None,
    on_tool_call=None,
    on_tool_delta=None,
    supports_vision=False,
    on_failover=None,
    skip_tools: bool = False,
    on_code_output=None,
    current_user_name_override: str | None = None,
    system_prompt_suffix: str | None = None,
    cancel_event: asyncio.Event | None = None,
) -> str:
    """Call LLM with automatic failover support."""
    guard = FailoverGuard()

    # Config-level fallback: if no primary, use fallback directly
    if primary_model is None and fallback_model is not None:
        logger.info("[Failover] Primary model not configured, using fallback directly")
        primary_model = fallback_model
        fallback_model = None

    if primary_model is None:
        return "⚠️ 未配置 LLM 模型"

    # Wrapper callbacks to track state for guard checks
    async def _wrapped_on_chunk(text: str):
        guard.mark_streaming_started()
        if on_chunk:
            await on_chunk(text)

    async def _wrapped_on_tool_call(data: dict):
        if data.get("status") == "done":
            guard.mark_tool_executed()
        if on_tool_call:
            await on_tool_call(data)

    # Try primary model
    primary_result = await call_llm(
        primary_model,
        messages,
        agent_name,
        role_description,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        on_chunk=_wrapped_on_chunk,
        on_tool_call=_wrapped_on_tool_call,
        on_tool_delta=on_tool_delta,
        on_thinking=on_thinking,
        supports_vision=supports_vision,
        skip_tools=skip_tools,
        on_code_output=on_code_output,
        current_user_name_override=current_user_name_override,
        system_prompt_suffix=system_prompt_suffix,
        cancel_event=cancel_event,
    )

    # Check if we need to failover
    # 成功返回的文本（非错误前缀）直接返回，不需要 failover。
    # 避免正常回复（如 "我是 WL4，你的 Android 工程师"）触发 Canceled 误导日志。
    if not (primary_result.startswith("[LLM Error]") or
            primary_result.startswith("[LLM call error]") or
            primary_result.startswith("[Error]")):
        return primary_result
    if not is_retryable_error(primary_result):
        logger.warning(f"[Failover] Canceled: Primary model returned a non-retryable error: {primary_result[:150]}")
        return primary_result

    # Check guard conditions
    if not guard.can_failover():
        if guard.tool_executed:
            logger.warning("[Failover] Blocked: side-effecting tool already executed")
        elif guard.streaming_started:
            logger.warning("[Failover] Blocked: streaming already started")
        elif guard.failover_done:
            logger.warning("[Failover] Blocked: failover already done once")
        return primary_result

    # No fallback available
    if fallback_model is None:
        logger.warning("[Failover] No fallback model available")
        return primary_result

    # Runtime failover: retry with fallback model
    logger.info(f"[Failover] Retrying with fallback model: {fallback_model.provider}/{fallback_model.model}")

    if on_failover:
        try:
            await on_failover(f"Switched to fallback model: {fallback_model.model}")
        except Exception:
            pass

    guard.mark_failover_done()

    # Call fallback with fresh callbacks
    fallback_guard = FailoverGuard()
    fallback_guard.mark_failover_done()

    async def _fallback_on_chunk(text: str):
        fallback_guard.mark_streaming_started()
        if on_chunk:
            await on_chunk(text)

    async def _fallback_on_tool_call(data: dict):
        if data.get("status") == "done":
            fallback_guard.mark_tool_executed()
        if on_tool_call:
            await on_tool_call(data)

    fallback_result = await call_llm(
        fallback_model,
        messages,
        agent_name,
        role_description,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        on_chunk=_fallback_on_chunk,
        on_tool_call=_fallback_on_tool_call,
        on_tool_delta=on_tool_delta,
        on_thinking=on_thinking,
        supports_vision=getattr(fallback_model, 'supports_vision', False),
        skip_tools=skip_tools,
        on_code_output=on_code_output,
        current_user_name_override=current_user_name_override,
        system_prompt_suffix=system_prompt_suffix,
        cancel_event=cancel_event,
    )

    # Combine error messages if fallback also failed
    if is_retryable_error(fallback_result) or fallback_result.startswith("⚠️") or fallback_result.startswith("[Error]"):
        return f"⚠️ 调用模型出错: Primary: {primary_result[:80]} | Fallback: {fallback_result[:80]}"

    return fallback_result


# ═══════════════════════════════════════════════════════════════════════════════
# High-level Agent Call Functions
# ═══════════════════════════════════════════════════════════════════════════════

async def call_agent_llm(
    db: AsyncSession,
    agent_id: uuid.UUID,
    user_text: str,
    history: list[dict] | None = None,
    user_id: uuid.UUID | None = None,
    session_id: str = "",
    on_chunk=None,
    on_thinking=None,
    supports_vision: bool = False,
) -> str:
    """Call the agent's LLM with automatic failover support."""
    from app.models.agent import Agent
    from app.models.llm import LLMModel
    from app.core.permissions import is_agent_expired

    # Load agent
    agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent: Agent | None = agent_result.scalar_one_or_none()
    if not agent:
        return "⚠️ 数字员工未找到"

    if is_agent_expired(agent):
        return "This Agent has expired and is off duty. Please contact your admin to extend its service."

    # Load primary model
    primary_model: LLMModel | None = None
    if agent.primary_model_id:
        model_result = await db.execute(select(LLMModel).where(LLMModel.id == agent.primary_model_id))
        primary_model = model_result.scalar_one_or_none()

    # Load fallback model
    fallback_model: LLMModel | None = None
    if agent.fallback_model_id:
        fb_result = await db.execute(select(LLMModel).where(LLMModel.id == agent.fallback_model_id))
        fallback_model = fb_result.scalar_one_or_none()

    # Config-level fallback: primary missing -> use fallback
    if not primary_model and fallback_model:
        primary_model = fallback_model
        fallback_model = None
        logger.warning(f"[call_agent_llm] Primary model unavailable, using fallback: {primary_model.model}")

    if not primary_model:
        return f"⚠️ {agent.name} 未配置 LLM 模型，请在管理后台设置。"

    # Build conversation messages
    messages: list[dict] = []
    if history:
        messages.extend(history[-10:])
    messages.append({"role": "user", "content": user_text})

    # Use unified call_llm_with_failover
    try:
        reply = await call_llm_with_failover(
            primary_model=primary_model,
            fallback_model=fallback_model,
            messages=messages,
            agent_name=agent.name,
            role_description=agent.role_description or "",
            agent_id=agent_id,
            user_id=user_id or agent_id,
            session_id=session_id,
            on_chunk=on_chunk,
            on_thinking=on_thinking,
            supports_vision=supports_vision or getattr(primary_model, 'supports_vision', False),
        )
        return reply
    except Exception as e:
        error_msg = str(e) or repr(e)
        logger.error(f"[call_agent_llm] Unexpected error: {error_msg}")
        return f"⚠️ 调用模型出错: {error_msg[:150]}"


async def call_agent_llm_with_tools(
    db: AsyncSession,
    agent_id: uuid.UUID,
    system_prompt: str,
    user_prompt: str,
    max_rounds: int = 50,
    session_id: str = "",
) -> str:
    """Call agent LLM with tool-calling loop (for background services)."""
    from app.models.agent import Agent
    from app.models.llm import LLMModel

    # Load agent and models
    agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent: Agent | None = agent_result.scalar_one_or_none()
    if not agent:
        return "⚠️ Agent not found"

    # Load models
    primary_model: LLMModel | None = None
    if agent.primary_model_id:
        model_result = await db.execute(select(LLMModel).where(LLMModel.id == agent.primary_model_id))
        primary_model = model_result.scalar_one_or_none()

    fallback_model: LLMModel | None = None
    if agent.fallback_model_id:
        fb_result = await db.execute(select(LLMModel).where(LLMModel.id == agent.fallback_model_id))
        fallback_model = fb_result.scalar_one_or_none()

    # Config-level fallback
    if not primary_model and fallback_model:
        primary_model = fallback_model
        fallback_model = None

    if not primary_model:
        return f"⚠️ {agent.name} has no LLM model configured"

    # Build messages
    messages = [
        LLMMessage(role="system", content=system_prompt),
        LLMMessage(role="user", content=user_prompt),
    ]

    tools_for_llm = await get_agent_tools_for_llm(agent_id)
    # 单发工具循环：CCR marker 在本轮内产生，历史无从 sticky 检测，故静态注入 retrieve_context
    try:
        from app.services.llm.stable_context import (
            get_retrieve_context_tool_definition,
            RETRIEVE_CONTEXT_TOOL_NAME,
        )
        RETRIEVE_CONTEXT_TOOL_DEFINITION = get_retrieve_context_tool_definition()
        if not any(
            (t.get("function") or {}).get("name") == RETRIEVE_CONTEXT_TOOL_NAME
            for t in tools_for_llm
        ):
            tools_for_llm = list(tools_for_llm) + [RETRIEVE_CONTEXT_TOOL_DEFINITION]
    except Exception as _e:
        logger.warning("[CTX-CCR] retrieve inject failed: {}", _e)
    try:
        from app.services.agent_context import CTX_SUBAGENT_CCR_RULES
        from app.services.llm.stable_context import build_ccr_system_appendix, get_retrieve_context_tool_definition
        CCR_SYSTEM_APPENDIX = build_ccr_system_appendix()
        _ccr_block = CCR_SYSTEM_APPENDIX
        if CTX_SUBAGENT_CCR_RULES.strip() not in system_prompt:
            _ccr_block = CTX_SUBAGENT_CCR_RULES + CCR_SYSTEM_APPENDIX
        if _ccr_block not in system_prompt:
            system_prompt = system_prompt + _ccr_block
            messages[0] = LLMMessage(role="system", content=system_prompt)
    except Exception as _e:
        logger.warning("[CTX-CCR] system appendix inject failed: {}", _e)

    allowed_tool_names = _allowed_tool_names(tools_for_llm)

    async def _try_model(model: LLMModel) -> tuple[str, bool, bool]:
        """Try to complete with a model. Returns (response, success, tool_executed)."""
        _accumulated_usage = TokenUsage()
        _unsaved_usage = TokenUsage()
        _last_cache_read = 0
        _last_cache_creation = 0
        tool_executed = False
        # ── 上下文压缩会话初始化（后台旁路收口：与 call_llm 同层覆盖）──
        _session_compressor = ContextCompressor(session_id=session_id)
        try:
            from .context_zones import PrefixCacheTracker
            _session_compressor.prefix_tracker = PrefixCacheTracker(
                min_frozen=int(get_settings().CTX_FROZEN_PREFIX_MSGS)
            )
        except Exception:
            pass
        _model_name = model.model
        _ctx_window = _get_ctx_guard_max_window(_model_name)
        _ctx_warn_ratio, _ctx_compress_ratio = _get_ctx_guard_ratios(_model_name)
        _ctx_compress_enabled = get_settings().CTX_COMPRESS_ENABLED
        _ctx_path = "agent_tools"
        logger.info(
            "[CTX] path={} session={} compress_enabled={} ctx_window={} model={}",
            _ctx_path, session_id, _ctx_compress_enabled, _ctx_window, _model_name,
        )
        try:
            client = create_llm_client(
                provider=model.provider,
                api_key=get_model_api_key(model),
                model=model.model,
                base_url=model.base_url,
                timeout=_get_model_timeout(model),
            )

            max_tokens = get_max_tokens(
                model.provider, model.model,
                getattr(model, 'max_output_tokens', None)
            )

            # Tool-calling loop
            api_messages = list(messages)
            for round_i in range(max_rounds):
                # Check token usage limit mid-loop (every 3 rounds)
                if round_i > 0 and round_i % 3 == 0:
                    if agent_id and _unsaved_usage.total_tokens > 0:
                        await record_token_usage(agent_id, _unsaved_usage)
                        _unsaved_usage = TokenUsage()
                        _, _token_limit_msg = await _get_agent_config(agent_id)
                        if _token_limit_msg:
                            logger.warning(f"[call_agent_llm_with_tools] Token limit exceeded mid-loop: {_token_limit_msg}")
                            await client.close()
                            return _token_limit_msg, False, tool_executed

                # ── CTX-GUARD：每轮 complete 前按 token 预算压缩历史（Layer 1）──
                if _ctx_compress_enabled and api_messages:
                    _cur_tokens = _est_tokens(api_messages, _model_name)
                    if _cur_tokens > _ctx_window * _ctx_warn_ratio:
                        logger.warning(
                            "[CTX-GUARD] path={} session={} tokens={}/{} ({:.0%}) round={}",
                            _ctx_path, session_id, _cur_tokens, _ctx_window, _cur_tokens / _ctx_window, round_i + 1,
                        )
                    _pre_pressure = _cur_tokens / max(_ctx_window, 1)
                    _retrieve_avail = _tools_include_retrieve_context(tools_for_llm)
                    _pre_budget = pre_round_budget(
                        _pre_pressure,
                        retrieve_avail=_retrieve_avail,
                        cache_read=_last_cache_read,
                        round_i=round_i,
                        last_fold_round=int(getattr(_session_compressor, "_last_fold_round", -100)),
                    )
                    if _pre_budget.should_enter:
                        await _apply_pre_round_context(
                            api_messages,
                            ctx_window=_ctx_window,
                            model_name=_model_name,
                            session_id=session_id,
                            agent_id=agent_id,
                            ctx_path=_ctx_path,
                            tools_for_llm=tools_for_llm,
                            round_i=round_i,
                            compressor=_session_compressor,
                            last_cache_read=_last_cache_read,
                            last_cache_creation=_last_cache_creation,
                        )
                        logger.info(
                            "[CTX-BUDGET] path={} session={} compressed {}→{} tokens round={}",
                            _ctx_path, session_id, _cur_tokens, _est_tokens(api_messages, _model_name), round_i + 1,
                        )

                try:
                    response = await client.complete(
                        messages=api_messages,
                        tools=tools_for_llm if tools_for_llm else None,
                        temperature=model.temperature,
                        max_tokens=max_tokens,
                    )
                except Exception as e:
                    logger.error(f"[call_agent_llm_with_tools] Agent {agent_id}: LLM call error: {e}")
                    await client.close()
                    if agent_id and _unsaved_usage.total_tokens > 0:
                        await record_token_usage(agent_id, _unsaved_usage)
                    raise

                # Track tokens for this round
                _usage_this_round = _usage_from_response_or_estimate(response, api_messages)
                _accumulated_usage.add(_usage_this_round)
                _unsaved_usage.add(_usage_this_round)
                if _usage_this_round.cache_read_tokens or _usage_this_round.cache_creation_tokens:
                    _last_cache_read = _usage_this_round.cache_read_tokens
                    _last_cache_creation = _usage_this_round.cache_creation_tokens
                    logger.info(
                        "[CTX-CACHE] path={} session={} round={} cache_read={} cache_creation={}",
                        _ctx_path, session_id, round_i + 1, _last_cache_read, _last_cache_creation,
                    )

                # 纯文本即最终答案，对齐 OpenAI Agents SDK
                if not response.tool_calls:
                    if response.content:
                        api_messages.append(LLMMessage(role="assistant", content=response.content))
                    if agent_id and _unsaved_usage.total_tokens > 0:
                        await record_token_usage(agent_id, _unsaved_usage)
                    await client.close()
                    return response.content or "", True, tool_executed

                # Execute tool calls
                sanitized_tool_calls, retry_instruction = _sanitize_tool_calls_for_context(response.tool_calls)
                if retry_instruction:
                    api_messages.append(LLMMessage(role="user", content=retry_instruction))
                    continue

                finish_call = find_finish_call(sanitized_tool_calls)
                if finish_call:
                    if finish_call.valid:
                        if agent_id and _unsaved_usage.total_tokens > 0:
                            await record_token_usage(agent_id, _unsaved_usage)
                        await client.close()
                        return finish_call.content, True, tool_executed
                    api_messages.append(LLMMessage(
                        role="assistant",
                        content=response.content or None,
                        tool_calls=sanitized_tool_calls,
                        reasoning_content=response.reasoning_content,
                    ))
                    api_messages.append(LLMMessage(
                        role="tool",
                        tool_call_id=finish_call.call_id,
                        content=finish_call.error or "`finish` was invalid.",
                    ))
                    continue

                api_messages.append(LLMMessage(
                    role="assistant",
                    content=response.content or None,
                    tool_calls=sanitized_tool_calls,
                    reasoning_content=response.reasoning_content,
                ))

                if sanitized_tool_calls:
                    tool_executed = True
                await _execute_tool_round(
                    sanitized_tool_calls,
                    api_messages=api_messages,
                    agent_id=agent_id,
                    user_id=agent.creator_id,
                    session_id=session_id,
                    supports_vision=False,
                    on_tool_call=None,
                    on_code_output=None,
                    full_reasoning_content=response.reasoning_content or "",
                    allowed_tool_names=allowed_tool_names,
                    model_name=_model_name,
                    ctx_window=_ctx_window,
                    compress_enabled=_ctx_compress_enabled,
                    ctx_path=_ctx_path,
                    retrieve_tool_available=_tools_include_retrieve_context(tools_for_llm),
                    round_i=round_i,
                    layer0_budget_scale=float(getattr(_session_compressor, "layer0_budget_scale", 1.0)),
                )

                # F4：本轮工具结果去重（会话级隔离）
                if _ctx_compress_enabled:
                    _dedup_file_tool_results(
                        api_messages, round_i=round_i, dedup_store=_session_compressor._dedup_seen,
                    )
                    _dedup_list_tool_results(
                        api_messages, round_i=round_i, list_store=_session_compressor._list_seen,
                    )

            if agent_id and _unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _unsaved_usage)
            await client.close()
            return "[Error] Too many tool call rounds", False, tool_executed

        except Exception as e:
            if agent_id and _unsaved_usage.total_tokens > 0:
                await record_token_usage(agent_id, _unsaved_usage)
            return f"[Error] {e}", False, tool_executed

    # Try primary model
    reply, success, primary_tool_executed = await _try_model(primary_model)
    if success:
        return reply

    # Primary failed - check if retryable
    error_type = classify_error(Exception(reply))
    if error_type == FailoverErrorType.NON_RETRYABLE or not fallback_model:
        return reply

    if primary_tool_executed:
        logger.warning("[call_agent_llm_with_tools] Blocked fallback: side-effecting tool already executed")
        return reply

    # Try fallback model
    logger.info(f"[call_agent_llm_with_tools] Retrying with fallback: {fallback_model.model}")
    reply2, success2, _fallback_tool_executed = await _try_model(fallback_model)
    if success2:
        return reply2

    return f"⚠️ Both models failed | Primary: {reply[:80]} | Fallback: {reply2[:80]}"


__all__ = [
    "call_llm",
    "call_llm_with_failover",
    "call_agent_llm",
    "call_agent_llm_with_tools",
    "FailoverGuard",
    "is_retryable_error",
]
