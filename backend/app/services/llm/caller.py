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
import json
import os
import time
import re
import uuid
import hashlib
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import get_settings
from app.database import async_session


def _perf_channel_suffix() -> str:
    """ACP 会话在共用 caller 里打 channel=acp，便于 agentbay 筛选。"""
    try:
        from app.plugins.clawith_acp.tool_bridge import current_acp_handler

        if current_acp_handler.get() is not None:
            return " channel=acp"
    except Exception:
        pass
    return ""

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

if TYPE_CHECKING:
    from app.models.agent import Agent
    from app.models.llm import LLMModel


TOOLS_REQUIRING_ARGS = frozenset({
    "write_file", "read_file", "move_file", "delete_file", "read_document",
    "send_message_to_agent", "send_feishu_message", "send_email"
})

# ── 模型上下文窗口映射: 用于上下文利用率告警 ──
# key: model 标识符 (LLMModel.model), value: context window (tokens)
# 未匹配到的模型回退到 ACP_CTX_WINDOW_TOKENS 环境变量 (默认 131072)
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    # DeepSeek
    "deepseek-v3": 128_000,
    "deepseek-r1": 128_000,
    "deepseek-chat": 128_000,
    "deepseek-reasoner": 128_000,
    # OpenAI
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8_192,
    "o1": 200_000,
    "o1-mini": 200_000,
    "o3-mini": 200_000,
    # Anthropic
    "claude-3-opus-20240229": 200_000,
    "claude-3.5-sonnet-20241022": 200_000,
    "claude-3.5-haiku-20241022": 200_000,
    "claude-opus-4-6": 200_000,
    "claude-sonnet-4-20250514": 200_000,
    "claude-haiku-3.5": 200_000,
    # Google
    "gemini-1.5-pro": 1_048_576,
    "gemini-1.5-flash": 1_048_576,
    "gemini-2.0-flash": 1_048_576,
    "gemini-2.5-pro": 1_048_576,
    # Qwen (通义千问)
    "qwen-max": 32_000,
    "qwen-plus": 131_072,
    "qwen-turbo": 1_000_000,
    "qwen2.5": 131_072,
    # 豆包 (字节)
    "doubao-pro-32k": 32_000,
    "doubao-pro-128k": 128_000,
    # GLM (智谱)
    "glm-4": 128_000,
    "glm-4-flash": 128_000,
    # Moonshot (月之暗面)
    "moonshot-v1-8k": 8_192,
    "moonshot-v1-32k": 32_000,
    "moonshot-v1-128k": 128_000,
    # DeepSeek V4: 1M tokens
    "deepseek-v4": 1_000_000,
}


def _resolve_ctx_window(model: "LLMModel") -> int:
    """Resolve context window for a model by exact match, then prefix match."""
    model_name = getattr(model, "model", "") or ""
    # Exact match
    if model_name in MODEL_CONTEXT_WINDOWS:
        return MODEL_CONTEXT_WINDOWS[model_name]
    # Prefix match (e.g. "deepseek-v3-0324" matches "deepseek-v3")
    for key, window in MODEL_CONTEXT_WINDOWS.items():
        if model_name.startswith(key):
            return window
    # Fallback to env var
    return int(os.getenv("ACP_CTX_WINDOW_TOKENS", "131072"))


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

    # 按优先级从高到低匹配
    patterns: list[tuple[str, str]] = [
        (r'insufficient\s*balance', '💰 API 余额不足，请前往控制台充值后重试。'),
        (r'invalid.*api[\s_-]*key|unauthorized', '🔑 API 密钥无效或已过期，请检查 API 设置。'),
        (r'rate[\s_-]*limit|too\s*many\s*request', '⏳ 请求过于频繁，请稍后重试。'),
        (r'connection[\s_-]*refused', '🔌 无法连接到模型服务，请检查网络或服务状态。'),
        (r'time[-\s]?out|timed\s*out', '⏱️ 模型响应超时，请稍后重试。'),
        (r'context[\s_-]*length[\s_-]*exceed', '📏 上下文超过模型限制，请缩短对话或减少附加内容。'),
        (r'internal[\s_-]*server[\s_-]*error|internal\s+error', '🖥️ 模型服务暂时异常，请稍后重试或联系管理员。'),
        # HTTP 400 通常由消息格式错误引起（如截断破坏 tool_calls 配对）
        (r'invalid_request_error|tool_calls.*must\s+be\s+followed|tool.*must\s+be\s+a\s+response',
         '🔄 上下文消息格式异常（历史过长被截断导致工具调用配对丢失），请开启新会话重试。'),
    ]

    for pattern, friendly in patterns:
        if re.search(pattern, text_lower):
            return friendly

    # 5xx 状态码兜底
    status_match = re.search(r'HTTP\s+(\d{3})', error_text)
    if status_match and status_match.group(1).startswith('5'):
        return f'🖥️ 模型服务暂时异常（HTTP {status_match.group(1)}），请稍后重试或联系管理员。'

    # 无法识别时返回摘要（500 字符足以包含完整 JSON 错误体）
    short = error_text[:500].replace('\n', ' ')
    return f'⚠️ 调用模型出错，请联系管理员。错误摘要: {short}'


def _repair_truncated_messages(messages: list) -> list:
    """修复截断产生的孤儿 tool_calls: 删除缺少 tool_result 的 assistant(tool_calls)。

    CTX-GUARD 截断 `api_messages[:_half] + api_messages[-_half:]` 可能从中间
    切断 assistant(tool_calls) → tool(result) 链。此函数扫描并移除孤立的
    assistant 消息(其 tool_calls 无对应 tool 响应), 防止 LLM API 400 错误。
    """
    # 收集所有 tool 消息的 tool_call_id
    tool_result_ids = {
        m.tool_call_id for m in messages
        if m.role == "tool" and getattr(m, "tool_call_id", None)
    }
    repaired = []
    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            # 过滤掉无对应 tool_result 的 tool_call
            valid_calls = [
                tc for tc in m.tool_calls
                if tc.get("id", "") in tool_result_ids
            ]
            if not valid_calls:
                # 全部孤立 — 丢弃此 assistant 消息
                continue
            if len(valid_calls) != len(m.tool_calls):
                # 部分孤立 — 仅保留有效调用
                m.tool_calls = valid_calls
            repaired.append(m)
        elif m.role == "tool":
            tid = getattr(m, "tool_call_id", None)
            if tid and tid not in _collect_all_tool_call_ids(messages):
                # 孤立的 tool 响应(对应 assistant 已移除) — 丢弃
                continue
            repaired.append(m)
        else:
            repaired.append(m)
    removed = len(messages) - len(repaired)
    if removed:
        orphaned_tool_names = [
            tc.get("function", {}).get("name", "?")
            for m in messages
            if m.role == "assistant" and m.tool_calls
            for tc in m.tool_calls
            if tc.get("id", "") not in tool_result_ids
        ][:10]
        logger.warning(
            f"[CTX-GUARD-REPAIR] removed {removed} orphaned msgs "
            f"tools={orphaned_tool_names}"
        )


def _collect_all_tool_call_ids(messages: list) -> set:
    """收集所有 assistant 消息中的 tool_call id。"""
    ids = set()
    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            for tc in m.tool_calls:
                ids.add(tc.get("id", ""))
    return ids


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
# 上下文压缩管道 (v4.1 — 类型感知压缩 + 指标修正)
# ═══════════════════════════════════════════════════════════════════════════════
_TOOL_MIN, _JSON_SMALL, _SEARCH_MIN, _LOG_MIN = 512, 20, 30, 40
_TEXT_MIN, _TEXT_MAX = 4096, 8192
_CACHE_MAX, _BREAKER_MAX, _BREAKER_COOLDOWN = 200, 3, 60.0
_CCR_MAX = 500

_compress_cache: OrderedDict[str, str] = OrderedDict()
_ccr_store: OrderedDict[str, str] = OrderedDict()
_breaker_failures, _breaker_open_until = 0, 0.0

_ERROR_KW = frozenset({"error","failed","failure","fatal","critical","panic","exception",
    "traceback","stack trace","segfault","abort","timeout","timed out","crashed","killed",
    "terminated","denied","refused","invalid","unavailable","unreachable","corrupt",
    "corrupted","overflow","deadlock","unauthorized","forbidden","access denied","deprecated"})
CCR_SENTINEL_KEY = "_ccr_dropped"

_GREP_RE = re.compile(r'^(?:\.{0,2}/)?[^\s:]+:\d+:|^\x1b\[[0-9;]*m', re.MULTILINE)
_LOG_RE = re.compile(r'\d{4}[-/]\d{2}[-/]\d{2}[T\s]\d{2}:\d{2}|\[\d{4}-\d{2}-\d{2}|\b(ERROR|CRITICAL|FATAL|WARN|WARNING|INFO|DEBUG|TRACE)\b')
_SIG_RE = re.compile(r'^\s*(def |class |fn |function |public |private |protected |export |async def |async fn )')
_STOP_RE = re.compile(r'\b(the|a|an|is|are|was|were|be|been|being|have|has|had|do|does|did|will|would|shall|should|may|might|can|could|to|of|in|for|on|with|at|by|from|as|into|through|during|and|but|or|nor|not|so|yet|both|either|neither|this|that|these|those|it|its|they|them|their)\b', re.IGNORECASE)
_CODE_KW = ("import ","from ","def ","class ","fn ","function ","// ","/*","export ","package ","public ","private ","use ","mod ","struct ","enum ","interface ","impl ")
_IMPORTANCE_RE = re.compile(r'(error|fail|exception|panic|traceback|segfault|abort|timeout|crash|denied|refused|forbidden|deadlock|corrupt|overflow|TODO|FIXME|HACK|BUG|WARNING|DEPRECATED)', re.IGNORECASE)


def _breaker_is_open() -> bool:
    global _breaker_failures, _breaker_open_until
    now = time.monotonic()
    if _breaker_open_until > 0 and now >= _breaker_open_until:
        _breaker_failures = 0; _breaker_open_until = 0.0
    return _breaker_open_until > 0


def _breaker_record_failure() -> None:
    global _breaker_failures, _breaker_open_until
    _breaker_failures += 1
    if _breaker_failures >= _BREAKER_MAX:
        _breaker_open_until = time.monotonic() + _BREAKER_COOLDOWN
        logger.warning(f"[CTX-COMPRESS] breaker OPEN — bypass {_BREAKER_COOLDOWN}s")


def _breaker_record_success() -> None:
    global _breaker_failures, _breaker_open_until
    _breaker_failures = 0; _breaker_open_until = 0.0


def _est_tokens(msgs: list) -> int:
    t = 0
    for m in msgs:
        c = getattr(m, 'content', None)
        if isinstance(c, str):
            t += len(c)
        elif isinstance(c, list):
            for p in c:
                if isinstance(p, dict) and p.get("type") == "text":
                    t += len(p.get("text", ""))
        if tc := getattr(m, 'tool_calls', None):
            t += len(json.dumps(tc, default=str))
        if dc := getattr(m, 'dynamic_content', None):
            t += len(dc)
    return max(t // 3, 1)


def _trunc(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[:n] + f"\n... ({len(s) - n} more chars)"


def _work_paths(msgs: list) -> set[str]:
    ps: set[str] = set()
    n = 0
    for m in reversed(msgs):
        if getattr(m, 'role', None) != "assistant":
            continue
        for tc in (getattr(m, 'tool_calls', None) or []):
            a = tc.get("function", {}).get("arguments", "{}")
            if isinstance(a, str):
                try:
                    a = json.loads(a)
                except json.JSONDecodeError:
                    continue
            for k in ("path", "file_path", "filePath", "file"):
                if v := a.get(k):
                    ps.add(str(v))
            n += 1
            if n >= 20:
                return ps
    return ps


def _isolate(msg, wp: set[str]) -> bool:
    if getattr(msg, 'role', None) == "system":
        return True
    c = getattr(msg, 'content', None)
    if isinstance(c, str):
        lo = c.lower()
        if any(kw in lo for kw in _ERROR_KW):
            return True
        if wp and any(p in lo for p in wp):
            return True
    return False


def _detect(content: str) -> str:
    s = content.strip()
    if not s:
        return "empty"
    if s[0] in ("{", "["):
        try:
            json.loads(s)
            return "json"
        except (json.JSONDecodeError, ValueError):
            pass
    h = s.split("\n")[:10]
    if sum(1 for L in h if _GREP_RE.search(L)) >= max(len(h) * 0.5, 2):
        return "search"
    ls = s.split("\n")[:30]
    if sum(1 for L in ls if _LOG_RE.search(L)) >= max(len(ls) * 0.3, 3):
        return "log"
    cs = s.split("\n")[:20]
    if sum(1 for L in cs if L.lstrip().startswith(_CODE_KW)) >= 3:
        return "code"
    return "text"


def _compress_cached(content: str) -> str:
    h = hashlib.sha256(content.encode()).hexdigest()
    if h in _compress_cache:
        return _compress_cache[h]
    ct = _detect(content)
    result = _dispatch(content, ct)
    if result is not content:
        if len(_compress_cache) >= _CACHE_MAX:
            _compress_cache.popitem(last=False)
        _compress_cache[h] = result
    return result


def _dispatch(c: str, t: str) -> str:
    if t == "json":
        return _json(c)
    if t == "search":
        return _search(c)
    if t == "log":
        return _log(c)
    if t == "code":
        return _code(c)
    if t == "text":
        return _text(c)
    return c


def _json(content: str) -> str:
    try:
        d = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return _trunc(content, 4096)
    if isinstance(d, list):
        n = len(d)
        if n <= _JSON_SMALL:
            return content
        errors = [i for i in d[:min(n, 500)] if isinstance(i, dict)
                  and any(kw in str(i).lower() for kw in ("error", "fail", "exception"))]
        keys = {k for i in d[:min(n, 100)] if isinstance(i, dict) for k in i}
        kept = list(d[:5])
        if errors:
            kept.append({"_pinned_errors": len(errors)})
        kept += list(d[-5:])
        dropped = n - len(kept)
        if dropped > 0:
            h = hashlib.sha256(content.encode()).hexdigest()[:12]
            if len(_ccr_store) >= _CCR_MAX:
                _ccr_store.popitem(last=False)
            _ccr_store[h] = content
            kept.append({CCR_SENTINEL_KEY: f"ccr:{h} {dropped}_rows {len(content)}B"})
        return json.dumps({"_total": n, "_fields": sorted(keys)[:20], "_sample": kept},
                          ensure_ascii=False)
    if isinstance(d, dict):
        c = {}
        for k, v in d.items():
            if isinstance(v, str) and len(v) > 200:
                c[k] = v[:200] + "..."
            elif isinstance(v, (list, tuple)) and len(v) > 20:
                c[k] = f"[{len(v)} items]"
            elif isinstance(v, dict):
                c[k] = f"{{...{len(v)} keys...}}"
            else:
                c[k] = v
        return json.dumps(c, ensure_ascii=False)
    return _trunc(content, 4096)


def _search(content: str) -> str:
    lines = content.strip().split("\n")
    if len(lines) <= _SEARCH_MIN:
        return content
    fc, important = {}, []
    for line in lines:
        m = re.match(r'^(?:\.{0,2}/)?([^\s:]+(?:\.[a-zA-Z]+)?):\d+:', line)
        if m:
            fc[m.group(1)] = fc.get(m.group(1), 0) + 1
        if _IMPORTANCE_RE.search(line):
            important.append(line)
    p = [f"[grep: {len(lines)} matches / {len(fc)} files]",
         "Top: " + ", ".join(f"{f}({c})" for f, c in sorted(fc.items(), key=lambda x: -x[1])[:10])]
    if important:
        p.append(f"\n--- Highlights ({len(important)}) ---")
        p.extend(important[:20])
    p.append("\n--- Head ---")
    p.extend(lines[:5])
    p.append("\n--- Tail ---")
    p.extend(lines[-5:])
    return "\n".join(p)


def _log(content: str) -> str:
    lines = content.strip().split("\n")
    if len(lines) <= _LOG_MIN:
        return content
    im, it = [], False
    for line in lines:
        st = line.lstrip()
        is_t = (len(st) < len(line) or "Traceback" in line
                or st.startswith('File "') or re.match(r'^\s+at\s', line))
        if is_t:
            if not it:
                it = True
            im.append(line)
            continue
        it = False
        if re.search(r'\b(ERROR|CRITICAL|FATAL|WARN|WARNING)\b', line):
            im.append(line)
    r = list(lines[:20])
    r.append(f"\n... ({max(0, len(lines) - 40)} lines omitted) ...\n")
    if im:
        r.append(f"--- Alerts ({len(im)}) ---")
        r.extend(im[:30])
    r.append("--- Last 20 ---")
    r.extend(lines[-20:])
    return "\n".join(r)


def _code(content: str) -> str:
    lines = content.split("\n")
    r, ib, bi = [], False, 0
    for line in lines:
        s = line.strip()
        if not s:
            r.append("")
            continue
        if s.startswith(("import ", "from ", "use ", "require ", "#include", "using ")):
            r.append(line)
            continue
        if _SIG_RE.match(s):
            r.append(line)
            ib = True
            bi = len(line) - len(line.lstrip())
            continue
        if ib:
            cur = len(line) - len(line.lstrip()) if s else 0
            if cur <= bi and s:
                ib = False
                r.append(line)
            elif s.startswith(("return ", "raise ", "yield ", "throw ")):
                r.append(line)
        else:
            r.append(line)
    c = "\n".join(r)
    if len(c) > 4096:
        cl = c.split("\n")
        c = "\n".join(
            cl[:50] + [f"\n... ({max(0, len(cl) - 100)} lines omitted) ...\n"] + cl[-50:]
        )
    return c


def _text(content: str) -> str:
    if len(content) <= _TEXT_MIN:
        return content
    lines = content.strip().split("\n")
    if len(lines) <= 20:
        return _trunc(content, _TEXT_MAX)
    sc = []
    for i, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue
        v = len(s) + sum(1 for c in s if c.isupper()) * 2
        v += sum(1 for c in s if c in "{}[]()<>|&^%$#@!:=;")
        v -= len(_STOP_RE.findall(s)) * 5
        sc.append((i, max(v, 1), line))
    if not sc:
        return content
    sc.sort(key=lambda x: x[1], reverse=True)
    k = max(int(len(sc) * 0.5), 30)
    kp = sorted(sc[:k], key=lambda x: x[0])
    return _trunc("\n".join(ln for _, _, ln in kp), _TEXT_MAX)


def _ctx_compress(api_messages: list, ctx_window: int) -> list:
    from app.services.llm.client import LLMMessage

    if _breaker_is_open() or _est_tokens(api_messages) <= ctx_window * 0.80:
        return api_messages

    wp = _work_paths(api_messages)
    before = _est_tokens(api_messages)
    compressed = 0
    fallback = 0
    result = []

    try:
        for msg in api_messages:
            if _isolate(msg, wp):
                result.append(msg)
                continue
            role = getattr(msg, 'role', None)
            content = getattr(msg, 'content', None)
            if role == "tool" and isinstance(content, str) and len(content) > _TOOL_MIN:
                try:
                    nc = _compress_cached(content)
                    if content.strip() and isinstance(nc, str) and not nc.strip():
                        nc = content
                        logger.warning("[CTX-COMPRESS] empty output prevented")
                    if nc is not content:
                        compressed += 1
                    result.append(LLMMessage(
                        role="tool", content=nc,
                        tool_call_id=getattr(msg, 'tool_call_id', None),
                    ))
                except Exception:
                    fallback += 1
                    result.append(msg)
            else:
                result.append(msg)

        after = _est_tokens(result)
        if compressed or fallback:
            logger.info(
                f"[CTX-COMPRESS] {compressed} ok {fallback} fb "
                f"tokens: {before}→{after} "
                f"cache={len(_compress_cache)} ccr={len(_ccr_store)}"
            )
        if fallback == 0:
            _breaker_record_success()
        else:
            _breaker_record_failure()

        return _repair_truncated_messages(result)
    except Exception:
        _breaker_record_failure()
        logger.exception("[CTX-COMPRESS] pipeline failed, returning original")
        return api_messages


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
) -> str:
    """Process a single tool call and return result."""
    fn = tc["function"]
    tool_name = fn["name"]
    raw_args = fn.get("arguments", "{}")
    logger.info(f"[LLM] Calling tool: {tool_name}({json.dumps(raw_args, ensure_ascii=False)[:100]})")

    try:
        args = json.loads(raw_args) if raw_args else {}
    except json.JSONDecodeError:
        args = {}

    # Guard: check if tool requires arguments
    should_execute, error_msg = _check_tool_requires_args(tool_name, args)
    if not should_execute:
        return error_msg

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
        except Exception:
            pass

    # Execute tool — pass on_output for execute_code streaming
    _on_output = on_code_output if tool_name in ("execute_code", "execute_code_e2b") else None
    tool_start = time.monotonic()
    result = await execute_tool(
        tool_name, args,
        agent_id=agent_id,
        user_id=user_id or agent_id,
        session_id=session_id,
        on_output=_on_output,
    )
    tool_elapsed = time.monotonic() - tool_start
    logger.info("[LSP4J-PERF] tool: {} elapsed={:.3f}s{}", tool_name, tool_elapsed, _perf_channel_suffix())
    logger.info("[LLM-TOOL-RESULT] tool={} len={} result=\n{}", tool_name, len(str(result)), str(result))

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
        except Exception:
            pass
    
    # 工具结果截断：防止大结果（git 20KB+、构建日志 8KB+）膨胀 LLM 上下文
    # 来自 openhuman DEFAULT_TOOL_RESULT_BUDGET_BYTES = 16384
    _TOOL_RESULT_BUDGET_BYTES = 16384
    if isinstance(tool_content, str):
        _original_len = len(tool_content.encode("utf-8"))
        if _original_len > _TOOL_RESULT_BUDGET_BYTES:
            tool_content = tool_content[:16384] + f"\n…(截断 {_original_len - 16384} 字节)"
            logger.info(
                f"[TOOL-BUDGET] 截断 tool={tool_name} original_bytes={_original_len} "
                f"→ {_TOOL_RESULT_BUDGET_BYTES} session={session_id}"
            )

    api_messages.append(LLMMessage(
        role="tool",
        tool_call_id=tc["id"],
        content=tool_content,
    ))
    return ""



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
    cancel_event: asyncio.Event | None = None,
    skip_tools: bool = False,
    on_code_output=None,
    parallel_tools_extra_readonly: set[str] | None = None,
    tool_warning_mode: str = "default",
    current_user_name_override: str | None = None,
) -> str:
    """Call LLM via unified client with function-calling tool loop."""
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

    # Build rich prompt with soul, memory, skills, relationships
    from app.services.agent_context import build_agent_context
    # Look up current user's display name so the agent knows who it's talking to
    static_prompt, dynamic_prompt = await build_agent_context(agent_id, agent_name, role_description, current_user_name=_user_name)
    # 诊断日志: 确认智能体记忆/soul/skill 已注入 system prompt
    logger.info(
        "[CTX-LOAD] agent={} static_prompt_chars={} dynamic_prompt_chars={}{}",
        agent_id, len(static_prompt), len(dynamic_prompt), _perf_channel_suffix(),
    )

    # Load tools dynamically from DB. `skip_tools=True` is set by the WS
    # handler on the onboarding greeting turn; keep the runtime-level `finish`
    # tool available so every turn still has an explicit stop signal.
    if skip_tools:
        tools_for_llm = [FINISH_TOOL_DEFINITION]
    else:
        from app.services.agent_tools import AGENT_TOOLS
        tools_for_llm = await get_agent_tools_for_llm(agent_id) if agent_id else AGENT_TOOLS
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
    _consecutive_guard_failures = 0
    _ctx_pressure_warned = False
    _unsaved_usage = TokenUsage()

    # 中间轮次的 on_chunk 路由到 thinking：只有 finish 的内容才是用户可见正文。
    # finish 内容通过 on_tool_delta 流式推送（若 on_tool_delta 非空），
    # 或通过 finish_call.valid 分支直接调用原始 on_chunk（若无 on_tool_delta）。
    # 中间轮次模型输出的纯文本（未调 finish）路由到 on_thinking，
    # 前端将其显示为可折叠的思考内容，不会被后续 done 消息覆盖。
    _original_on_chunk = on_chunk
    _original_on_thinking = on_thinking

    async def _suppressed_on_chunk(text: str) -> None:
        """中间轮次的文本路由到 thinking 回调，避免 done 覆盖正文。"""
        if _original_on_thinking:
            await _original_on_thinking(text)
        # 若无 on_thinking 回调，文本被丢弃（至少不泄漏为正文）

    if on_chunk is not None:
        on_chunk = _suppressed_on_chunk

    # FINISH_PROTOCOL_REMINDER 注入标记：同一轮 call_llm 调用中仅注入一次到 system prompt
    _reminder_injected = False

    # Tool-calling loop
    for round_i in range(_max_tool_rounds):
        # 取消检查：若 cancel_event 已设置，立即中断工具循环
        if cancel_event and cancel_event.is_set():
            logger.info("[LLM] cancel_event 已设置，中断工具循环（round={}）", round_i)
            break

        # P0: 最后一轮仅允许 finish 工具，防止 Agent 无限制工具循环
        _is_last_round = (round_i == _max_tool_rounds - 1)
        _round_tools = tools_for_llm
        if _is_last_round and tools_for_llm:
            _finish_tool = next((t for t in tools_for_llm if isinstance(t, dict) and t.get("function", {}).get("name") == "finish"), None)
            if _finish_tool is None:
                _finish_tool = next((t for t in tools_for_llm if hasattr(t, 'function') and getattr(t.function, 'name', None) == "finish"), None)
            _round_tools = [_finish_tool] if _finish_tool else tools_for_llm
            logger.warning(
                f"[LAST-ROUND] round={round_i + 1}/{_max_tool_rounds} "
                f"tools_before={len(tools_for_llm)} tools_after={len(_round_tools)} session={session_id}"
            )
        # [LSP4J-PERF] 轮次开始计时
        round_start = time.monotonic()
        logger.info(
            "[LSP4J-PERF] Round {}/{} start, context_tokens={}{}",
            round_i + 1,
            _max_tool_rounds,
            _accumulated_usage.input_tokens,
            _perf_channel_suffix(),
        )

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

        try:
            # 上下文利用率检查：使用当前 api_messages 估算而非累计 input_tokens
            _ctx_window = _resolve_ctx_window(model)
            _ctx_ratio = _est_tokens(api_messages) / _ctx_window
            if _ctx_ratio >= 0.90:
                logger.warning(
                    f"[CTX-GUARD] context 90%: tokens={_est_tokens(api_messages)}"
                    f"/{_ctx_window} ({_ctx_ratio:.1%}) model={getattr(model, 'model', '?')} session={session_id}"
                )
                if not _ctx_pressure_warned:
                    _ctx_pressure_warned = True
                    api_messages.append(LLMMessage(
                        role="user",
                        content=(
                            f"⚠️ 上下文利用率已达 {_ctx_ratio:.0%}（~{_est_tokens(api_messages)}/{_ctx_window} tokens）。"
                            "请精简后续输出，优先使用 finish 总结当前进度，避免在剩余轮次中发起高成本工具调用。"
                        ),
                    ))
                if _ctx_ratio >= 0.95:
                    _consecutive_guard_failures += 1
                    if _consecutive_guard_failures >= 3:
                        logger.error(
                            f"[CTX-GUARD] BREAKER: {_consecutive_guard_failures} consecutive failures, forcing finish"
                        )
                        api_messages.append(LLMMessage(
                            role="user",
                            content="上下文已严重超限，请立即调用 finish 结束。",
                        ))
                    else:
                        # v4.1: 类型感知压缩替代暴力截断
                        api_messages = _ctx_compress(api_messages, ctx_window=_ctx_window)
                        logger.warning(
                            f"[CTX-COMPRESS] compressed: est_tokens={_est_tokens(api_messages)} "
                            f"failures={_consecutive_guard_failures}/3"
                        )
                else:
                    _consecutive_guard_failures = 0
            response = await asyncio.wait_for(
                client.stream(
                    messages=api_messages,
                    tools=_round_tools if _round_tools else None,
                    temperature=model.temperature,
                    max_tokens=max_tokens,
                    on_chunk=on_chunk,
                    on_tool_delta=on_tool_delta,
                    on_thinking=on_thinking,
                ),
                timeout=float(os.getenv("ACP_LLM_STREAM_TIMEOUT", "30")),  # 默认 30s (hermes-agent 对齐), env 可覆盖
            )
            llm_time = time.monotonic() - round_start
        except asyncio.TimeoutError:
            _elapsed = time.monotonic() - round_start
            logger.warning(
                f"[LLM-TIMEOUT] stream timed out after {_elapsed:.1f}s "
                f"round={round_i + 1} session={session_id}"
            )
            api_messages.append(LLMMessage(
                role="user",
                content="LLM 推理超时（超过 60 秒）。请立即调用 finish 结束当前轮次。",
            ))
            continue
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

        # Log round completion with estimated (not raw) token usage
        logger.info(
            "[LSP4J-PERF] Round {}: LLM stream done "
            "llm_time={:.2f}s tool_calls={} "
            "input_tokens={} output_tokens={}{}",
            round_i + 1, llm_time, len(response.tool_calls or []),
            getattr(_usage_this_round, "input_tokens", 0),
            getattr(_usage_this_round, "output_tokens", 0),
            _perf_channel_suffix(),
        )
        # Plain assistant text is not a stop condition. The model must finish
        # explicitly via finish(content=...).
        if not response.tool_calls:
            if response.content:
                api_messages.append(LLMMessage(role="assistant", content=response.content))
            # 将 finish 协议提醒注入 system prompt 的 dynamic_content 而非作为 user 消息。
            # 作为 user 消息会导致模型"回复"这条提醒（例如 "Acknowledged..."），
            # 而不是继续回答用户的原始问题。注入 system prompt 让模型将其理解为元指令。
            if not _reminder_injected:
                api_messages[0].dynamic_content = (
                    (api_messages[0].dynamic_content or "") + "\n\n" + FINISH_PROTOCOL_REMINDER
                )
                _reminder_injected = True
            continue

        # Execute tool calls
        logger.info(f"[LLM] Round {round_i+1}: {len(response.tool_calls)} tool call(s)")
        # 当 LLM 返回 0 个 tool_calls 时 (纯思考轮次), 向客户端推送状态提示,
        # 消除"黑屏等待"体验。回调失败不应中断 LLM 流程。
        if len(response.tool_calls or []) == 0 and on_thinking:
            try:
                await on_thinking(f"第 {round_i+1} 轮思考中… | Thinking round {round_i+1}…")
            except Exception:
                pass
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
                # finish(content=...) 才是用户可见正文；中间轮次的 on_chunk 已被 _suppressed_on_chunk 吞掉
                # 若调用方已通过 on_tool_delta 流式推送 finish 内容（如 WebSocket chat），
                # 此处不再重复调用 _original_on_chunk，避免前端收到重复 chunk 导致内容闪烁
                _already_streamed_via_delta = on_tool_delta is not None
                if _original_on_chunk and finish_call.content and not _already_streamed_via_delta:
                    await _original_on_chunk(finish_call.content)
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

        # ── 只读工具集合: 无副作用, 可安全并行执行 ──
        # 约束: 此集合中的工具不会触发 on_code_output (无 execute_code 等代码执行)
        _READONLY_TOOLS: frozenset[str] = frozenset({
            "read_file", "search_files", "search_file",
            "list_files", "list_dir", "find_files",
            "web_search", "duckduckgo_search", "google_search",
            "read_webpage", "jina_read", "jina_search",
            "read_document", "exa_search", "tavily_search",
        })
        # 并行读取并发上限: 防止极端场景 (LLM 产生 20+ 个 read_file) 导致文件描述符耗尽。
        # 参考 LangChain gather_with_concurrency + Semaphore 模式。
        _MAX_READ_CONCURRENCY = 10

        # ── LSP4J 路径守卫: 插件端 WebSocket 单通道, ToolInvokeProcessor 同步调用无锁保护 ──
        # 并发发送多个工具请求可能导致 CountDownLatch 状态混乱。
        # 检测到活跃 LSP4J 连接时回退串行执行。
        _lsp4j_active = False
        try:
            from app.plugins.clawith_lsp4j.jsonrpc_router import get_active_router
            _lsp4j_active = await get_active_router((str(user_id), str(agent_id))) is not None
        except ImportError:
            pass  # LSP4J 插件未安装, 正常跳过
        except Exception as exc:
            # DB 异常/路由表损坏不应导致守卫误判 (false negative → 并行发给不兼容插件)
            logger.warning("[PERF] LSP4J 守卫检测异常, 保守假定活跃回退串行: {}", exc)
            _lsp4j_active = True

        if _lsp4j_active:
            # LSP4J 路径: 保持原有串行逻辑, 不做并行化
            for tc in sanitized_tool_calls or []:
                tool_error = await _process_tool_call(
                    tc=tc, api_messages=api_messages,
                    agent_id=agent_id, user_id=user_id,
                    session_id=session_id, supports_vision=supports_vision,
                    on_tool_call=on_tool_call, on_code_output=on_code_output,
                    full_reasoning_content=full_reasoning_content,
                    allowed_tool_names=allowed_tool_names,
                )
                if tool_error:
                    api_messages.append(LLMMessage(
                        role="tool", content=tool_error,
                        tool_call_id=tc.get("id", ""),
                    ))
                tool_time = time.monotonic() - round_start - llm_time
                logger.info(
                    "[LSP4J-PERF] Round {}: tools done "
                    "tool_time={:.2f}s total={:.2f}s{}",
                    round_i + 1, tool_time, time.monotonic() - round_start,
                    _perf_channel_suffix(),
                )
        else:
            # ACP / WebUI / IM 路径: 只读工具并行执行
            # 将工具调用分为只读组(可并行)和写入组(需串行, 因为有副作用)
            read_items: list[tuple[dict, str]] = []
            write_items: list[tuple[dict, str]] = []

            # ── 读写冲突检测: 收集写入路径, 检查只读工具是否涉及相同文件 ──
            # 极端情况: LLM 在同轮发出 read_file("a.txt") + write_file("a.txt"),
            # 若 LLM 意图"先写再读验证", 并行会破坏顺序。检测到冲突时全部回退串行。
            _write_paths: set[str] = set()
            _has_conflict = False
            for tc in sanitized_tool_calls or []:
                fn = tc.get("function", {}) or {}
                tname = fn.get("name", "")
                if tname not in _READONLY_TOOLS:
                    try:
                        args = json.loads(fn.get("arguments", "{}"))
                        _p = args.get("path") or args.get("file_path") or args.get("source")
                        if _p:
                            _write_paths.add(str(_p))
                    except json.JSONDecodeError:
                        # 写入工具 arguments 解析失败, 保守假定有冲突 (回退串行防 data race)
                        logger.warning("[PERF] 工具 {} arguments JSON 解析失败, 保守回退串行: {}",
                                       tname, fn.get("arguments", "")[:100])
                        _has_conflict = True
                        break

            if not _has_conflict:
                for tc in sanitized_tool_calls or []:
                    fn = tc.get("function", {}) or {}
                    tname = fn.get("name", "")
                    if tname in _READONLY_TOOLS:
                        try:
                            args = json.loads(fn.get("arguments", "{}"))
                            _rp = args.get("path") or args.get("file_path")
                            if _rp and str(_rp) in _write_paths:
                                _has_conflict = True
                                break
                        except json.JSONDecodeError:
                            # 只读工具参数解析失败, 保守回退 (无法确定是否冲突)
                            logger.warning("[PERF] 只读工具 {} arguments JSON 解析失败, 保守回退串行",
                                           tname)
                            _has_conflict = True
                            break

            if _has_conflict:
                # 读写冲突: 全部回退串行
                logger.warning(
                    "[PERF] Round {}: 检测到读写工具操作相同文件 paths={}, 回退串行执行",
                    round_i + 1, list(_write_paths)[:5],
                )
                for tc in sanitized_tool_calls or []:
                    tool_error = await _process_tool_call(
                        tc=tc, api_messages=api_messages,
                        agent_id=agent_id, user_id=user_id,
                        session_id=session_id, supports_vision=supports_vision,
                        on_tool_call=on_tool_call, on_code_output=on_code_output,
                        full_reasoning_content=full_reasoning_content,
                        allowed_tool_names=allowed_tool_names,
                    )
                    if tool_error:
                        api_messages.append(LLMMessage(
                            role="tool", content=tool_error,
                            tool_call_id=tc.get("id", ""),
                        ))
            else:
                # 正常路径: 分类执行
                for tc in sanitized_tool_calls or []:
                    fn = tc.get("function", {}) or {}
                    tool_name = fn.get("name", "")
                    if tool_name in _READONLY_TOOLS:
                        read_items.append((tc, tool_name))
                    else:
                        write_items.append((tc, tool_name))

                # Step 1: 并行执行只读工具
                if read_items:
                    _t_read_start = time.monotonic()
                    # Semaphore 在调用内创建 (非模块级), 防止跨会话竞争配额
                    _read_semaphore = asyncio.Semaphore(
                        min(_MAX_READ_CONCURRENCY, max(len(read_items), 1))
                    )

                    async def _gated_read(tc, _tname):
                        async with _read_semaphore:
                            return await _process_tool_call(
                                tc=tc, api_messages=api_messages,
                                agent_id=agent_id, user_id=user_id,
                                session_id=session_id, supports_vision=supports_vision,
                                on_tool_call=on_tool_call,
                                full_reasoning_content=full_reasoning_content,
                                allowed_tool_names=allowed_tool_names,
                            )

                    read_tasks = [_gated_read(tc, tname) for tc, tname in read_items]
                    results = await asyncio.gather(*read_tasks, return_exceptions=True)
                    for (tc, tname), result in zip(read_items, results):
                        if isinstance(result, BaseException):
                            # 不将完整异常信息暴露给 LLM (可能含文件路径/堆栈), 仅记录日志
                            logger.error("[TOOL-ERROR] {} 执行异常: {}", tname, result)
                            api_messages.append(LLMMessage(
                                role="tool",
                                content=f"[Error] {tname} 执行失败",
                                tool_call_id=tc.get("id", ""),
                            ))
                        elif result:
                            api_messages.append(LLMMessage(
                                role="tool", content=result,
                                tool_call_id=tc.get("id", ""),
                            ))
                    logger.info(
                        "[LSP4J-PERF] Round {}: parallel read tools done "
                        "count={} elapsed={:.3f}s{}",
                        round_i + 1, len(read_items),
                        time.monotonic() - _t_read_start, _perf_channel_suffix(),
                    )

                # Step 2: 串行执行写入工具 (保持顺序, 因为有副作用)
                # on_code_output 只在 write_items 循环中使用, 避免并行读取时输出混乱
                for tc, tname in write_items:
                    _t_write_start = time.monotonic()
                    tool_error = await _process_tool_call(
                        tc=tc, api_messages=api_messages,
                        agent_id=agent_id, user_id=user_id,
                        session_id=session_id, supports_vision=supports_vision,
                        on_tool_call=on_tool_call, on_code_output=on_code_output,
                        full_reasoning_content=full_reasoning_content,
                        allowed_tool_names=allowed_tool_names,
                    )
                    logger.info(
                        "[LSP4J-PERF] Round {}: write tool {} done elapsed={:.3f}s{}",
                        round_i + 1, tname, time.monotonic() - _t_write_start,
                        _perf_channel_suffix(),
                    )
                    if tool_error:
                        api_messages.append(LLMMessage(
                            role="tool", content=tool_error,
                            tool_call_id=tc.get("id", ""),
                        ))

            # 所有工具执行完成
            tool_time = time.monotonic() - round_start - llm_time
            logger.info(
                "[LSP4J-PERF] Round {}: all tools done "
                "tool_time={:.2f}s total={:.2f}s{}",
                round_i + 1, tool_time, time.monotonic() - round_start,
                _perf_channel_suffix(),
            )

    # Record tokens even on "too many rounds" exit
    if agent_id and _accumulated_usage.total_tokens > 0:
        await record_token_usage(agent_id, _accumulated_usage)
    if agent_id and _unsaved_usage.total_tokens > 0:
        await record_token_usage(agent_id, _unsaved_usage)
    _final_ratio = _accumulated_usage.input_tokens / _ctx_window if _ctx_window else 0
    if _final_ratio >= 0.5:
        logger.info(
            f"[CTX-GUARD] post-stream session={session_id} "
            f"utilization={_final_ratio:.1%} context_window={_ctx_window}"
        )
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
    cancel_event: asyncio.Event | None = None,
    skip_tools: bool = False,
    parallel_tools_extra_readonly: set[str] | None = None,
    tool_warning_mode: str = "default",
    on_code_output=None,
    current_user_name_override: str | None = None,
    max_tool_rounds_override: int | None = None,  # 可选覆盖, 不传则走 WebUI agent.max_tool_rounds
) -> str:
    """Call LLM with automatic failover support.

    Args:
        cancel_event: 可选的取消事件，透传至 call_llm 中断工具循环和流式输出。
    """
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
        cancel_event=cancel_event,
        skip_tools=skip_tools,
        parallel_tools_extra_readonly=parallel_tools_extra_readonly,
        tool_warning_mode=tool_warning_mode,
        on_code_output=on_code_output,
        current_user_name_override=current_user_name_override,
        max_tool_rounds_override=max_tool_rounds_override,
    )

    # Check if we need to failover
    if not is_retryable_error(primary_result):
        # 区分两种情况：正常响应（finish 返回的用户可见文本）vs 不可重试的 LLM 错误
        if primary_result.startswith(("[LLM Error]", "[LLM call error]", "[Error]")):
            logger.warning(f"[Failover] Canceled: Primary model returned a non-retryable error: {primary_result[:150]}")
            return _format_friendly_error(primary_result)
        # 正常响应：无错误，无需 failover
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
        cancel_event=cancel_event,
        skip_tools=skip_tools,
        parallel_tools_extra_readonly=parallel_tools_extra_readonly,
        tool_warning_mode=tool_warning_mode,
        on_code_output=on_code_output,
        current_user_name_override=current_user_name_override,
        max_tool_rounds_override=max_tool_rounds_override,
    )

    # Combine error messages if fallback also failed
    if is_retryable_error(fallback_result) or fallback_result.startswith("⚠️") or fallback_result.startswith("[Error]"):
        return _format_friendly_error(primary_result)

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
        return _format_friendly_error(error_msg)


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
    allowed_tool_names = _allowed_tool_names(tools_for_llm)

    async def _try_model(model: LLMModel) -> tuple[str, bool, bool]:
        """Try to complete with a model. Returns (response, success, tool_executed)."""
        _accumulated_usage = TokenUsage()
        _unsaved_usage = TokenUsage()
        tool_executed = False
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

                if not response.tool_calls:
                    if response.content:
                        api_messages.append(LLMMessage(role="assistant", content=response.content))
                    api_messages.append(LLMMessage(role="user", content=FINISH_PROTOCOL_REMINDER))
                    continue

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

                for tc in sanitized_tool_calls or []:
                    fn = tc["function"]
                    tool_name = fn["name"]
                    raw_args = fn.get("arguments", "{}")
                    try:
                        args = parse_tool_arguments(raw_args)
                    except json.JSONDecodeError:
                        args = {}

                    tool_executed = True
                    if tool_name not in allowed_tool_names:
                        logger.warning(f"[call_agent_llm_with_tools] Blocked disabled tool call: {tool_name} agent_id={agent_id}")
                        result = _tool_not_enabled_message(tool_name)
                    else:
                        result = await execute_tool(
                            tool_name, args,
                            agent_id=agent_id,
                            user_id=agent.creator_id,
                            session_id=session_id,
                        )
                    api_messages.append(LLMMessage(
                        role="tool",
                        tool_call_id=tc["id"],
                        content=str(result),
                    ))

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
