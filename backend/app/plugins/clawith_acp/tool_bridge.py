"""ACP 工具桥 — 文件操作 + 终端代理。

通过当前活跃的 AcpHandler 将工具调用代理到 IDE 插件执行。

ContextVar:
- current_acp_handler: AcpHandler | None — 活跃的 ACP 会话 handler
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
import shlex
import time
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

from loguru import logger

from app.plugins.clawith_acp.turn_budget import (
    PERMISSION_GATED_METHODS,
    get_turn_budget,
)
from app.services.llm.tool_execution_policy import WORKSPACE_WRITE_TOOLS
from app.plugins.clawith_acp.acp_routes import ACP_METHOD_MAP
from app.plugins.clawith_acp.coalesce_keys import METHODS_FOR_COALESCE, normalize_coalesce_key
from app.plugins.clawith_acp.search_dedup import (
    get_cached_search,
    invalidate_search_cache_for_session,
    normalize_search_key,
    search_cache_enabled,
    store_search_result,
)

# ── 进行中读请求合并: 对同一 (session, method, params) 的并行读请求只发一次 IDE 调用 ──
_METHODS_FOR_COALESCE = METHODS_FOR_COALESCE
_inflight: dict[str, asyncio.Task[Any]] = {}
_inflight_lock = asyncio.Lock()


async def coalesce_or_execute(
    method: str,
    path: str,
    session_id: str,
    executor: Callable[[], Any],
    args: dict | None = None,
) -> Any:
    """对同一 (session, method, path) 合并并行读请求，复用已有 Task 结果。"""
    if method not in _METHODS_FOR_COALESCE:
        return await executor()

    key = normalize_coalesce_key(session_id, method, path, args)
    if not key:
        return await executor()

    async with _inflight_lock:
        existing = _inflight.get(key)
        if existing is not None:
            if not existing.done():
                logger.info("[ACP-COALESCE] hit key={} method={} — 复用已有请求", key, method)
                waiter = existing
            else:
                try:
                    return existing.result()
                except Exception:
                    _inflight.pop(key, None)
                    waiter = None
        else:
            waiter = None
        if waiter is None:
            task = asyncio.create_task(executor())
            _inflight[key] = task
            logger.debug("[ACP-COALESCE] new key={} method={} — 创建新请求", key, method)
        else:
            task = waiter

    try:
        return await task
    except asyncio.CancelledError:
        # 取消不清理 key — 让原始 Task 继续运行, 其他等待者仍可复用其返回值
        logger.debug("[ACP-COALESCE] cancelled key={} — 保留原始 Task", key)
        raise
    except BaseException:
        async with _inflight_lock:
            if _inflight.get(key) is task:       # identity check 防止误删重连后同一 key 的新 Task
                del _inflight[key]
                logger.debug("[ACP-COALESCE] error cleanup key={}", key)
        raise
    else:
        async with _inflight_lock:
            if _inflight.get(key) is task:
                del _inflight[key]
                logger.debug("[ACP-COALESCE] done cleanup key={}", key)


# ── 礼貌延迟: 请求间 16ms 最小间隔, 给 EDT 调度留空隙 ──
# 原理:
#   - PSI 缓存预热: 请求 A 加载缓存 → 间隔确保缓存热 → 请求 B 命中热缓存
#   - VFS 刷新合并: 间隙确保前一个 refresh 已完成
#   - EDT 调度空隙: 16ms (1 EDT frame @ 60fps) 允许 UI 事件处理
# 多 Agent 隔离: _last_send_ts + _polite_lock 是 AcpHandler 实例字段,
# Agent A 和 Agent B 各自维护发送间隔, 互不干扰。
# env ACP_POLITENESS_ENABLED=true (默认开启), ACP_POLITENESS_DELAY_S=0.016

_POLITENESS_ENABLED: bool = os.getenv("ACP_POLITENESS_ENABLED", "true").lower() == "true"
_POLITENESS_DELAY_S: float = float(os.getenv("ACP_POLITENESS_DELAY_S", "0.016"))  # 16ms

# fast path: 不消耗 VFS/PSI 的方法跳过礼貌延迟
_FAST_METHODS: frozenset[str] = frozenset({
    "ide/index_status", "ide/active_file", "fs/diagnostics",
})


async def _send_with_budget(handler: Any, method: str, params: dict[str, Any], timeout: float) -> Any:
    """send_request + TurnBudget deadline 裁剪；权限 RPC 期间暂停 compute 计时。"""
    budget = get_turn_budget()
    is_hitl = method in PERMISSION_GATED_METHODS
    if budget is not None:
        timeout = budget.cap_timeout(timeout, for_hitl=is_hitl)
    if is_hitl and budget is not None:
        budget.suspend_for_hitl(method)
        try:
            return await handler.send_request(method, params, timeout)
        finally:
            budget.resume_from_hitl()
    return await handler.send_request(method, params, timeout)


async def polite_send(
    handler: Any,          # AcpHandler instance
    method: str,
    params: dict[str, Any],
    timeout: float,
) -> Any:
    """带礼貌延迟的 send_request 包装。

    使用 handler 实例上的 _last_send_ts 和 _polite_lock，
    保证多 Agent 之间的礼貌延迟互相隔离。

    跳过策略:
      - 快路径方法 (index_status/active_file/diagnostics): 不消耗资源, 跳过延迟
      - 写操作方法: 用户期望低延迟, 跳过延迟
      - 其他方法: 应用 16ms 最小间隔
    """
    if not _POLITENESS_ENABLED:
        return await _send_with_budget(handler, method, params, timeout)

    # 写操作的方法族 (来自 _METHOD_FAMILIES)
    _write_methods_for_polite: frozenset[str] = frozenset({
        "fs/write_text_file", "fs/edit_text_file", "fs/write_text_files",
        "fs/convert_java_to_kotlin", "fs/refactor_rename", "fs/safe_delete",
        "fs/move_file", "fs/reformat_code", "fs/optimize_imports",
    })

    if method in _FAST_METHODS or method in _write_methods_for_polite:
        return await _send_with_budget(handler, method, params, timeout)

    # 计算与上次发送的间隔, 必要时等待
    async with handler._polite_lock:
        now = time.monotonic()
        gap = _POLITENESS_DELAY_S - (now - handler._last_send_ts)
        if gap > 0:
            handler._last_send_ts = now + gap   # 预先更新, 防止下一个请求过早
        else:
            handler._last_send_ts = now

    if gap > 0:
        logger.debug(
            "[ACP-POLITE] delay={:.0f}ms method={} — 给 EDT 调度留空隙",
            gap * 1000, method,
        )
        await asyncio.sleep(gap)

    return await _send_with_budget(handler, method, params, timeout)


# ── 延迟基线追踪 (第二梯队): 每方法族 RTT 采样 + P50/P95/P99 定期输出 ──
# 用途: Phase 0+1 上线后收集 24h 生产数据, 验证修复效果 + 为 Phase 2a Vegas 调参提供基线
# env ACP_BASELINE_ENABLED=true (默认开启), ACP_BASELINE_FLUSH_SAMPLES=100, ACP_BASELINE_FLUSH_SEC=300

_BASELINE_ENABLED: bool = os.getenv("ACP_BASELINE_ENABLED", "true").lower() == "true"
_BASELINE_MAX_SAMPLES: int = int(os.getenv("ACP_BASELINE_MAX_SAMPLES", "10000"))
_BASELINE_FLUSH_SAMPLES: int = int(os.getenv("ACP_BASELINE_FLUSH_SAMPLES", "100"))
_BASELINE_FLUSH_SEC: float = float(os.getenv("ACP_BASELINE_FLUSH_SEC", "300"))

# per-family ring buffer: {family: [rtt_ms, ...]}
_baseline_samples: dict[str, list[float]] = {}
_baseline_count: int = 0
_baseline_last_flush: float = time.monotonic()


def _baseline_family(method: str) -> str:
    """将 ACP method 映射到方法族 (与 Phase 2a _METHOD_FAMILIES 对齐)。"""
    if method in ("fs/read_text_file", "ide/index_status", "ide/active_file",
                   "fs/file_structure", "fs/diagnostics", "fs/get_documentation"):
        return "fast_read"
    if method in ("fs/find_file", "fs/find_class", "fs/find_symbol", "fs/search_text"):
        return "fast_search"
    if method in ("fs/find_references", "fs/find_definition", "fs/find_implementations",
                   "fs/find_super_methods", "fs/call_hierarchy", "fs/type_hierarchy"):
        return "heavy_search"
    if method in ("fs/list_directory",):
        return "listing"
    if method in ("fs/write_text_file", "fs/edit_text_file", "fs/write_text_files",
                   "fs/convert_java_to_kotlin", "fs/refactor_rename", "fs/safe_delete",
                   "fs/move_file", "fs/reformat_code", "fs/optimize_imports"):
        return "write"
    if method in ("ide/build_project",):
        return "build"
    return "other"


def record_baseline_rtt(method: str, rtt_s: float) -> None:
    """记录一次 ACP 方法调用的 RTT 到基线追踪器。

    在 _try_acp_execute 的 send_request 返回处调用。
    仅当 ACP_BASELINE_ENABLED=true 时记录。
    """
    if not _BASELINE_ENABLED:
        return
    family = _baseline_family(method)
    rtt_ms = rtt_s * 1000
    if family not in _baseline_samples:
        _baseline_samples[family] = []
    samples = _baseline_samples[family]
    samples.append(rtt_ms)
    # 环形缓冲: 超过上限时淘汰最早一半
    if len(samples) > _BASELINE_MAX_SAMPLES:
        _baseline_samples[family] = samples[len(samples) // 2:]
    global _baseline_count
    _baseline_count += 1
    # 每 N 个样本或每 M 秒输出一次统计
    if _baseline_count >= _BASELINE_FLUSH_SAMPLES or (
        time.monotonic() - _baseline_last_flush > _BASELINE_FLUSH_SEC
    ):
        flush_baseline_stats()


def flush_baseline_stats() -> None:
    """输出各方法族的 P50/P95/P99 延迟统计到日志。"""
    global _baseline_count, _baseline_last_flush
    if not _BASELINE_ENABLED or not _baseline_samples:
        return
    _baseline_count = 0
    _baseline_last_flush = time.monotonic()
    parts: list[str] = []
    for family in sorted(_baseline_samples.keys()):
        samples = _baseline_samples[family]
        if not samples:
            continue
        s = sorted(samples)
        n = len(s)
        p50 = s[int(n * 0.50)] if n > 0 else 0
        p95 = s[int(n * 0.95)] if n > 1 else s[0]
        p99 = s[int(n * 0.99)] if n > 1 else s[0]
        parts.append(
            f"{family}: n={n} p50={p50:.0f}ms p95={p95:.0f}ms p99={p99:.0f}ms"
        )
    if parts:
        logger.info("[ACP-BASELINE] {} | total_samples={}", " | ".join(parts),
                     sum(len(v) for v in _baseline_samples.values()))


# ── 失败路径去重: 同路径 read_file 失败 ≥3 次时拦截 ──
_failed_paths: dict[str, int] = {}
_FAILED_PATH_BLOCK = 3

current_acp_handler: ContextVar[Any | None] = ContextVar("current_acp_handler", default=None)

# region agent log
_AGENT_DEBUG_LOG_PATH = "/Users/shubinzhang/Documents/agent/.cursor/debug-9e9222.log"
_AGENT_DEBUG_SESSION_ID = "9e9222"


def _redact_debug_value(value: Any, project_root: str = "") -> Any:
    """调试日志仅保留判断结果, 本机绝对路径统一脱敏。"""
    home = os.path.expanduser("~")
    if isinstance(value, str):
        redacted = value
        if project_root:
            redacted = redacted.replace(project_root, "<PROJECT_ROOT>")
        if home:
            redacted = redacted.replace(home, "<HOME>")
        return redacted
    if isinstance(value, list):
        return [_redact_debug_value(item, project_root) for item in value]
    if isinstance(value, dict):
        nested_root = str(value.get("cwd") or project_root or "")
        return {key: _redact_debug_value(item, nested_root) for key, item in value.items()}
    return value


def _agent_debug_log(run_id: str, hypothesis_id: str, location: str, message: str, data: dict[str, Any]) -> None:
    """写入本轮调试 NDJSON; 只记录路径/状态, 不记录文件内容或密钥。"""
    payload = {
        "sessionId": _AGENT_DEBUG_SESSION_ID,
        "id": f"log_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}",
        "timestamp": int(time.time() * 1000),
        "runId": run_id,
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": _redact_debug_value(data),
    }
    try:
        os.makedirs(os.path.dirname(_AGENT_DEBUG_LOG_PATH), exist_ok=True)
        with open(_AGENT_DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.debug("[ACP-DEBUG] {} write_failed={} payload={}", message, type(exc).__name__, payload)
# endregion

# ACP 协议方法映射（见 acp_routes.ACP_METHOD_MAP）
_ACP_METHOD_MAP = ACP_METHOD_MAP

# ── 辅助函数 ──


def _get_session_id(handler) -> str:
    """从 ACP handler 提取 session_id。"""
    return getattr(handler, "session_id", "")


# ── ACP 参数构建器（每个 builder 接收 (tool_name, args, handler, session_id, path) ──
# ── 返回 dict[str,Any]（参数）或 str（错误消息）                            ──


async def _build_list_directory_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 list_directory 参数。"""
    # 路径穿越检测：拒绝包含 .. 的路径
    if ".." in (path or ""):
        return "❌ 路径中不允许包含 '..' 目录穿越"
    depth = max(1, min(args.get("depth", 3), 10))
    limit = max(10, min(args.get("limit", 500), 2000))
    return {"sessionId": session_id, "path": path, "depth": depth, "limit": limit}


async def _build_find_file_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 find_file 参数 — 按文件名搜索。"""
    query = args.get("query", "")
    if not query:
        return "❌ find_file 缺少 query 参数"
    params: dict[str, Any] = {
        "sessionId": session_id,
        "query": query,
        "scope": args.get("scope", "project_files"),
        "pageSize": max(1, min(args.get("pageSize") or args.get("page_size", 25), 500)),
    }
    cursor = args.get("cursor")
    if cursor:
        params["cursor"] = cursor
    return params


async def _build_search_text_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 search_text 参数 — 在 IDE 中搜索文本。"""
    query = args.get("query", "")
    if not query:
        return "❌ search_text 缺少 query 参数"
    params: dict[str, Any] = {
        "sessionId": session_id,
        "query": query,
        "regex": args.get("regex", False),
        "context": args.get("context", "all"),
        "caseSensitive": args.get("caseSensitive", True),
        "pageSize": max(1, min(args.get("pageSize") or args.get("page_size", 100), 500)),
    }
    file_pattern = args.get("filePattern") or args.get("file_pattern")
    if file_pattern:
        params["filePattern"] = file_pattern
    cursor = args.get("cursor")
    if cursor:
        params["cursor"] = cursor
    return params


async def _build_find_class_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 find_class 参数。"""
    query = args.get("query", "")
    if not query:
        return "❌ find_class: query 不能为空"
    params: dict[str, Any] = {
        "sessionId": session_id,
        "query": query,
        "scope": args.get("scope", "project_files"),
        "pageSize": int(args.get("pageSize", 25)),
    }
    if args.get("language"):
        params["language"] = args["language"]
    if args.get("matchMode"):
        params["matchMode"] = args["matchMode"]
    if args.get("cursor"):
        params["cursor"] = args["cursor"]
    logger.debug(f"[ACP] find_class query={query} scope={params['scope']}")
    return params


async def _build_find_symbol_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 find_symbol 参数。"""
    query = args.get("query", "")
    if not query:
        return "❌ find_symbol: query 不能为空"
    params: dict[str, Any] = {
        "sessionId": session_id,
        "query": query,
        "scope": args.get("scope", "project_files"),
        "pageSize": int(args.get("pageSize", 25)),
    }
    if args.get("language"):
        params["language"] = args["language"]
    if args.get("cursor"):
        params["cursor"] = args["cursor"]
    logger.debug(f"[ACP] find_symbol query={query} scope={params['scope']}")
    return params


async def _build_index_status_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 index_status 参数。"""
    logger.info("[ACP] index_status")
    return {"sessionId": session_id}


async def _build_ide_screenshot_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 ide_screenshot 参数。无需额外参数，仅传 sessionId。"""
    return {"sessionId": session_id}


async def _build_read_text_file_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 read_text_file 参数。"""
    params: dict[str, Any] = {"sessionId": session_id, "path": path}
    line = args.get("line")
    if line is not None:
        params["line"] = int(line)
    limit = args.get("limit")
    if limit is not None:
        params["limit"] = int(limit)
    return params


async def _build_edit_file_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 edit_text_file 参数 — 将补丁参数发送到 IDE 端原位替换。

    不再走「读文件 → 本地替换 → 全量写回」路径（存在并发覆盖风险），
    而是传递 oldString/newString/replaceAll 到 IDE 插件，由 IDE 用
    Document.replaceString() 执行原子原位替换，消除窗口期竞争。
    """
    old_str = args.get("old_string") or args.get("old_str", "")
    new_str = args.get("new_string") or args.get("new_str", "")
    if not old_str:
        return "❌ edit_file 缺少 old_string 参数"
    replace_all = args.get("replace_all", False)
    logger.debug(
        "[ACP] edit_file path={} old_len={} new_len={} replace_all={}",
        path, len(old_str), len(new_str), replace_all,
    )
    return {
        "sessionId": session_id,
        "path": path,
        "oldString": old_str,
        "newString": new_str,
        "replaceAll": replace_all,
    }


async def _build_write_text_file_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 write_text_file 参数。"""
    content = args.get("content", "")
    content_len = len(content) if content else 0
    logger.info(
        "[ACP-WRITE] build_params tool={} path={} content_len={} session={}",
        tool_name, path, content_len, session_id,
    )
    return {"sessionId": session_id, "path": path, "content": content}


async def _build_find_references_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 find_references 参数 — 查找符号的所有引用。"""
    params: dict[str, Any] = {
        "sessionId": session_id,
        "scope": args.get("scope", "project_files"),
        "pageSize": int(args.get("pageSize", 100)),
    }
    if args.get("file"): params["file"] = args["file"]
    if args.get("line"): params["line"] = int(args["line"])
    if args.get("column"): params["column"] = int(args["column"])
    if args.get("language"): params["language"] = args["language"]
    if args.get("symbol"): params["symbol"] = args["symbol"]
    if args.get("cursor"): params["cursor"] = args["cursor"]
    logger.debug(f"[ACP] find_references file={args.get('file')}")
    return params


async def _build_find_definition_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 find_definition 参数 — 导航到符号声明。"""
    params: dict[str, Any] = {"sessionId": session_id}
    if args.get("file"): params["file"] = args["file"]
    if args.get("line"): params["line"] = int(args["line"])
    if args.get("column"): params["column"] = int(args["column"])
    if args.get("language"): params["language"] = args["language"]
    if args.get("symbol"): params["symbol"] = args["symbol"]
    logger.debug(f"[ACP] find_definition file={args.get('file')}")
    return params


async def _build_find_implementations_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 find_implementations 参数 — 查找接口/抽象方法的所有实现。"""
    params: dict[str, Any] = {"sessionId": session_id}
    if args.get("file"): params["file"] = args["file"]
    if args.get("line"): params["line"] = int(args["line"])
    if args.get("column"): params["column"] = int(args["column"])
    if args.get("language"): params["language"] = args["language"]
    if args.get("symbol"): params["symbol"] = args["symbol"]
    if args.get("scope"): params["scope"] = args["scope"]
    if args.get("cursor"): params["cursor"] = args["cursor"]
    params["pageSize"] = int(args.get("pageSize", 100))
    logger.debug(f"[ACP] find_implementations file={args.get('file')}")
    return params


async def _build_find_super_methods_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 find_super_methods 参数 — 查找方法重写链。"""
    params: dict[str, Any] = {"sessionId": session_id}
    if args.get("file"): params["file"] = args["file"]
    if args.get("line"): params["line"] = int(args["line"])
    if args.get("column"): params["column"] = int(args["column"])
    if args.get("language"): params["language"] = args["language"]
    if args.get("symbol"): params["symbol"] = args["symbol"]
    has_position = all(params.get(key) is not None for key in ("file", "line", "column"))
    symbol = str(params.get("symbol") or "").strip()
    if symbol and "#" not in symbol:
        return (
            "⚠️ find_super_methods 需要方法符号, 不是类符号。"
            "请传 symbol='com.example.Class#methodName'，或传 file+line+column 定位到方法体内。"
        )
    if not has_position and not symbol:
        return (
            "⚠️ find_super_methods 参数不足。"
            "请先用 find_definition/open_file 定位方法，再传 file+line+column；"
            "或传 symbol='com.example.Class#methodName'。"
        )
    logger.debug(f"[ACP] find_super_methods file={args.get('file')}")
    return params


async def _build_call_hierarchy_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 call_hierarchy 参数 — 分析调用层次。"""
    params: dict[str, Any] = {"sessionId": session_id}
    if args.get("file"): params["file"] = args["file"]
    if args.get("line"): params["line"] = int(args["line"])
    if args.get("column"): params["column"] = int(args["column"])
    if args.get("language"): params["language"] = args["language"]
    if args.get("symbol"): params["symbol"] = args["symbol"]
    if args.get("direction"): params["direction"] = args["direction"]
    params["depth"] = int(args.get("depth", 3))
    if args.get("scope"): params["scope"] = args["scope"]
    logger.debug(f"[ACP] call_hierarchy file={args.get('file')} direction={args.get('direction')}")
    return params


async def _build_type_hierarchy_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 type_hierarchy 参数 — 获取类型继承层次。"""
    params: dict[str, Any] = {"sessionId": session_id}
    if args.get("className"): params["className"] = args["className"]
    if args.get("file"): params["file"] = args["file"]
    if args.get("line"): params["line"] = int(args["line"])
    if args.get("column"): params["column"] = int(args["column"])
    if args.get("scope"): params["scope"] = args["scope"]
    logger.debug(f"[ACP] type_hierarchy className={args.get('className')}")
    return params


async def _build_diagnostics_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 diagnostics 参数 — 获取 IDE 诊断。"""
    params: dict[str, Any] = {
        "sessionId": session_id,
        "severity": args.get("severity", "all"),
        "includeBuildErrors": args.get("includeBuildErrors", False),
        "includeTestResults": args.get("includeTestResults", False),
        "maxBuildErrors": int(args.get("maxBuildErrors", 100)),
        "maxTestResults": int(args.get("maxTestResults", 100)),
    }
    if args.get("file"): params["file"] = args["file"]
    if args.get("startLine"): params["startLine"] = int(args["startLine"])
    if args.get("endLine"): params["endLine"] = int(args["endLine"])
    logger.debug(f"[ACP] diagnostics file={args.get('file')} severity={params['severity']}")
    return params


async def _build_refactor_rename_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 refactor_rename 参数 — 安全重命名符号。"""
    new_name = args.get("newName", "")
    if not new_name:
        return "❌ refactor_rename: newName 不能为空"
    params: dict[str, Any] = {"sessionId": session_id, "newName": new_name}
    if args.get("file"): params["file"] = args["file"]
    if args.get("line"): params["line"] = int(args["line"])
    if args.get("column"): params["column"] = int(args["column"])
    if args.get("overrideStrategy"): params["overrideStrategy"] = args["overrideStrategy"]
    if args.get("relatedRenamingStrategy"): params["relatedRenamingStrategy"] = args["relatedRenamingStrategy"]
    logger.debug(f"[ACP] refactor_rename file={args.get('file')} newName={new_name}")
    return params


async def _build_move_file_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 move_file 参数 — 移动文件。"""
    src = args.get("file", "")
    dst = args.get("destination", "")
    if not src or not dst:
        return "❌ move_file: file 和 destination 不能为空"
    return {
        "sessionId": session_id,
        "file": src,
        "destination": dst,
    }


async def _build_reformat_code_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 reformat_code 参数 — 格式化代码。"""
    f_path = args.get("file", "")
    if not f_path:
        return "❌ reformat_code: file 不能为空"
    params: dict[str, Any] = {"sessionId": session_id, "file": f_path}
    if args.get("startLine"): params["startLine"] = int(args["startLine"])
    if args.get("endLine"): params["endLine"] = int(args["endLine"])
    params["optimizeImports"] = args.get("optimizeImports", True)
    params["rearrangeCode"] = args.get("rearrangeCode", True)
    logger.debug(f"[ACP] reformat_code file={f_path}")
    return params


async def _build_optimize_imports_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 optimize_imports 参数 — 仅优化导入。"""
    f_path = args.get("file", "")
    if not f_path:
        return "❌ optimize_imports: file 不能为空"
    logger.debug(f"[ACP] optimize_imports file={f_path}")
    return {"sessionId": session_id, "file": f_path}


async def _build_safe_delete_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 safe_delete 参数 — 安全删除符号或文件（插件侧 camelCase targetType）。"""
    f_path = path or args.get("path") or args.get("file") or args.get("file_path") or ""
    if not f_path:
        return "❌ safe_delete: file/path 不能为空"
    if tool_name == "delete_file":
        target_type = "file"
    else:
        target_type = args.get("targetType") or args.get("target_type", "symbol")
    params: dict[str, Any] = {
        "sessionId": session_id,
        "path": f_path,
        "file": f_path,
        "targetType": target_type,
        "force": args.get("force", False),
    }
    if args.get("line"):
        params["line"] = int(args["line"])
    if args.get("column"):
        params["column"] = int(args["column"])
    logger.debug(f"[ACP] safe_delete file={f_path} targetType={target_type}")
    return params


async def _build_convert_java_to_kotlin_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 convert_java_to_kotlin 参数 — Java 转 Kotlin。"""
    files = args.get("files", [])
    if not files or not isinstance(files, list):
        return "❌ convert_java_to_kotlin: files 数组不能为空"
    logger.debug(f"[ACP] convert_java_to_kotlin files={files}")
    return {"sessionId": session_id, "files": files}


async def _build_sync_files_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 sync_files 参数 — 同步文件系统。"""
    params: dict[str, Any] = {"sessionId": session_id}
    if args.get("paths"):
        params["paths"] = args["paths"]
    logger.info("[ACP] sync_files")
    return params


async def _build_active_file_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 active_file 参数 — 获取当前活动文件。"""
    logger.info("[ACP] active_file")
    return {"sessionId": session_id}


async def _build_open_file_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 open_file 参数 — 在 IDE 中打开文件。"""
    f_path = args.get("file", "")
    if not f_path:
        return "❌ open_file: file 不能为空"
    params: dict[str, Any] = {"sessionId": session_id, "file": f_path}
    if args.get("line"): params["line"] = int(args["line"])
    if args.get("column"): params["column"] = int(args["column"])
    logger.debug(f"[ACP] open_file file={f_path}")
    return params


async def _build_file_structure_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 file_structure 参数 — 获取文件结构。"""
    f_path = args.get("file", "")
    if not f_path:
        return "❌ file_structure: file 不能为空"
    logger.debug(f"[ACP] file_structure file={f_path}")
    return {"sessionId": session_id, "file": f_path}


async def _build_project_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 build_project 参数 — 编译项目。"""
    logger.debug(f"[ACP] build_project rebuild={args.get('rebuild')}")
    params: dict[str, Any] = {
        "sessionId": session_id,
        "rebuild": args.get("rebuild", False),
        "includeRawOutput": args.get("includeRawOutput", False),
    }
    if args.get("timeoutSeconds") is not None:
        params["timeoutSeconds"] = int(_bounded_build_timeout(args["timeoutSeconds"]))
    return params


def _bounded_build_timeout(raw: Any | None) -> float:
    """限制 build_project 超时, 避免 LLM 输入极大值长期占用 IDE。"""
    try:
        seconds = float(raw if raw is not None else os.getenv("ACP_BUILD_TIMEOUT", "180"))
    except (TypeError, ValueError):
        seconds = 180.0
    return min(max(seconds, 30.0), 600.0)


def _permission_timeout_seconds() -> float:
    """权限弹窗类 RPC 超时 — 与 IDE requestPermissions 120s 对齐。"""
    return float(os.getenv("ACP_PERMISSION_TIMEOUT", "120"))


def _timeout_for_acp_method(method: str, params: dict[str, Any]) -> float:
    """按 ACP 方法类型选择超时; build_project 不能沿用文件工具 15s 默认值。"""
    if method == "fs/safe_delete":
        return _permission_timeout_seconds()
    if method == "fs/write_text_file":
        return float(os.getenv("ACP_FS_WRITE_TIMEOUT", "60"))
    if method == "fs/edit_text_file":
        return float(os.getenv("ACP_FS_EDIT_TIMEOUT", "30"))
    if method == "ide/build_project":
        return _bounded_build_timeout(params.get("timeoutSeconds"))
    return float(os.getenv("ACP_FS_TIMEOUT", "15"))


async def _build_get_documentation_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 get_documentation 参数 — 获取符号文档。"""
    class_name = args.get("className", "")
    if not class_name:
        return "❌ get_documentation: className 不能为空"
    params: dict[str, Any] = {"sessionId": session_id, "className": class_name}
    if args.get("memberName"):
        params["memberName"] = args["memberName"]
    logger.debug(f"[ACP] get_documentation className={class_name}")
    return params


async def _build_apply_quickfix_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 apply_quickfix 参数 — 应用快速修复。"""
    f_path = args.get("file", "")
    if not f_path:
        return "❌ apply_quickfix: file 不能为空"
    fix_name = args.get("fixName", "")
    if not fix_name:
        return "❌ apply_quickfix: fixName 不能为空"
    params: dict[str, Any] = {
        "sessionId": session_id, "file": f_path,
        "line": int(args.get("line", 1)), "column": int(args.get("column", 1)),
        "fixName": fix_name,
    }
    logger.debug(f"[ACP] apply_quickfix file={f_path} fixName={fix_name}")
    return params


async def _build_git_status_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 git/status 参数 — 查看 Git 状态。"""
    logger.info("[ACP] git_status")
    return {"sessionId": session_id, "verbose": args.get("verbose", False)}


async def _build_git_diff_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 git/diff 参数 — 查看 Git 差异。"""
    params: dict[str, Any] = {
        "sessionId": session_id,
        "staged": args.get("staged", False),
        "statOnly": args.get("stat_only", False),
    }
    if args.get("commit"):
        params["commit"] = args["commit"]
    if args.get("path"):
        params["path"] = args["path"]
    logger.debug(f"[ACP] git_diff staged={params['staged']}")
    return params


async def _build_git_stage_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 git/stage 参数 — 暂存文件。"""
    params: dict[str, Any] = {
        "sessionId": session_id,
        "all": args.get("all", False),
    }
    if args.get("paths"):
        params["paths"] = args["paths"]
    logger.debug(f"[ACP] git_stage all={params['all']}")
    return params


async def _build_git_commit_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 git/commit 参数 — 创建提交。"""
    msg = args.get("message", "")
    if not msg:
        return "❌ git_commit: message 不能为空"
    params: dict[str, Any] = {
        "sessionId": session_id, "message": msg,
        "all": args.get("all", True), "amend": args.get("amend", False),
    }
    logger.debug(f"[ACP] git_commit all={params['all']} amend={params['amend']}")
    return params


# ── ACP 参数构建器注册表 ──

_ACP_PARAM_BUILDERS: dict[str, Any] = {
    "fs/list_directory": _build_list_directory_params,
    "fs/find_file": _build_find_file_params,
    "fs/search_text": _build_search_text_params,
    "fs/find_class": _build_find_class_params,
    "fs/find_symbol": _build_find_symbol_params,
    "ide/index_status": _build_index_status_params,
    "fs/read_text_file": _build_read_text_file_params,
    "fs/edit_text_file": _build_edit_file_params,
    "fs/write_text_file": _build_write_text_file_params,
    "fs/find_references": _build_find_references_params,
    "fs/find_definition": _build_find_definition_params,
    "fs/find_implementations": _build_find_implementations_params,
    "fs/find_super_methods": _build_find_super_methods_params,
    "fs/call_hierarchy": _build_call_hierarchy_params,
    "fs/type_hierarchy": _build_type_hierarchy_params,
    "fs/diagnostics": _build_diagnostics_params,
    "fs/refactor_rename": _build_refactor_rename_params,
    "fs/move_file": _build_move_file_params,
    "fs/reformat_code": _build_reformat_code_params,
    "fs/optimize_imports": _build_optimize_imports_params,
    "fs/safe_delete": _build_safe_delete_params,
    "fs/convert_java_to_kotlin": _build_convert_java_to_kotlin_params,
    "ide/sync_files": _build_sync_files_params,
    "ide/active_file": _build_active_file_params,
    "ide/open_file": _build_open_file_params,
    "fs/file_structure": _build_file_structure_params,
    "ide/build_project": _build_project_params,
    "fs/get_documentation": _build_get_documentation_params,
    "ide/apply_quickfix": _build_apply_quickfix_params,
    "git/status": _build_git_status_params,
    "git/diff": _build_git_diff_params,
    "git/stage": _build_git_stage_params,
    "git/commit": _build_git_commit_params,
    "ide/screenshot": _build_ide_screenshot_params,
}


# ── ACP 请求速率限制器 ──

class AcpRateLimiter:
    """ACP 请求频率限制器 — 对读搜索工具进行 60次/分钟 限流。

    使用 sliding window 算法，以 method 为 key 独立计数。
    """

    __slots__ = ("_default_limit", "_window_sec", "_buckets", "_lock")

    def __init__(self, default_limit: int = 60, window_sec: float = 60.0) -> None:
        self._default_limit = default_limit
        self._window_sec = window_sec
        self._lock = asyncio.Lock()
        # method -> list[float]（时间戳列表）
        self._buckets: dict[str, list[float]] = {}

    def _prune(self, method: str, now: float) -> None:
        """清理窗口外的旧时间戳。"""
        timestamps = self._buckets.get(method)
        if not timestamps:
            return
        cutoff = now - self._window_sec
        self._buckets[method] = [t for t in timestamps if t > cutoff]

    async def allow(self, method: str, limit: int | None = None) -> bool:
        """检查是否允许请求。允许则记录时间戳并返回 True。"""
        async with self._lock:
            now = time.time()
            self._prune(method, now)
            limit = limit or self._default_limit
            timestamps = self._buckets.setdefault(method, [])
            if len(timestamps) >= limit:
                return False
            timestamps.append(now)
            return True

    def remaining(self, method: str) -> int:
        """返回当前窗口剩余配额。"""
        now = time.time()
        self._prune(method, now)
        limit = self._default_limit
        return max(0, limit - len(self._buckets.get(method, [])))


_RATE_LIMITER = AcpRateLimiter(default_limit=120)  # 读工具限速 120次/min

# 内联导入：_DANGEROUS_BASH_ALWAYS 和 check_tool_autonomy 引用 agent_tools，
# 延迟导入避免循环依赖（agent_tools 也可能回引用本模块）
_INLINE_IMPORTED: dict[str, Any] = {}

def _lazy_import_agent_tools():
    """延迟加载 agent_tools 中的安全常量和函数，避免模块加载时循环依赖。"""
    if "check_tool_autonomy" not in _INLINE_IMPORTED:
        from app.services.agent_tools import (
            _DANGEROUS_BASH_ALWAYS as _bash_always,
            _DANGEROUS_BASH_NETWORK as _bash_network,
            check_tool_autonomy as _check_autonomy,
        )
        _INLINE_IMPORTED["_DANGEROUS_BASH_ALWAYS"] = _bash_always
        _INLINE_IMPORTED["_DANGEROUS_BASH_NETWORK"] = _bash_network
        _INLINE_IMPORTED["check_tool_autonomy"] = _check_autonomy
        from app.services.agent_tools import _TOOL_AUTONOMY_MAP as _autonomy_map
        _INLINE_IMPORTED["_TOOL_AUTONOMY_MAP"] = _autonomy_map

def _is_within_path(root: str, candidate: str) -> bool:
    """用 commonpath 判断路径边界, 避免 /project2 误匹配 /project。"""
    if not root or not candidate:
        return False
    try:
        root_norm = os.path.normpath(os.path.expanduser(root))
        cand_norm = os.path.normpath(os.path.expanduser(candidate))
        return os.path.commonpath([root_norm, cand_norm]) == root_norm
    except ValueError:
        return False


def _looks_agent_internal_path(path: str) -> bool:
    """只按约定前缀/文件名识别 Agent 内部文件, 不依赖 IDE cwd。"""
    normalized = (path or "").strip().replace("\\", "/")
    if not normalized:
        return False
    agent_prefixes = ("memory/", "skills/", "enterprise_info/")
    if any(normalized.startswith(p) for p in agent_prefixes):
        return True
    agent_files = ("soul.md", "focus.md", "memory.md", "tasks.json", "reflections.md")
    return normalized.split("/")[-1] in agent_files


def _is_project_file(path: str, cwd: str = "") -> bool:
    """判断路径是否是 IDE 项目文件(应走 ACP)还是 agent 自身文件(走本地)。"""
    if not path:
        return False
    normalized = path.strip().replace("\\", "/")
    if _looks_agent_internal_path(normalized):
        return False
    if normalized.startswith("/") or normalized.startswith("~"):
        return bool(cwd) and _is_within_path(cwd, normalized)
    return True  # 其余相对路径 → IDE 项目根


def is_agent_internal_path(path: str) -> bool:
    """Agent 后端内部文件(记忆、技能等), 不应在 IDE 时间线展示工具卡片。"""
    return _looks_agent_internal_path(path)


def _normalize_acp_project_path(raw: str, cwd: str) -> tuple[str, str | None, bool]:
    """把 ACP 项目路径压成相对项目根路径; 越界绝对路径直接拒绝。"""
    path = str(raw or "").strip().replace("\\", "/")
    if not path:
        return "", None, False
    if path.startswith("workspace/"):
        path = path[len("workspace/"):]
    expanded = os.path.expanduser(path)
    if os.path.isabs(expanded):
        if not cwd:
            return path, "IDE 项目根未知, 拒绝项目文件路径", False
        norm_abs = os.path.normpath(expanded)
        if not _is_within_path(cwd, norm_abs):
            return path, f"路径越界: {path} 不在 IDE 项目根 {cwd} 内", False
        rel = os.path.relpath(norm_abs, os.path.normpath(os.path.expanduser(cwd))).replace("\\", "/")
        return rel if rel != "." else ".", None, False
    if is_agent_internal_path(path):
        return path, None, True
    if not cwd:
        return path, "IDE 项目根未知, 拒绝项目文件路径", False
    norm_rel = os.path.normpath(path).replace("\\", "/")
    if norm_rel in ("..",) or norm_rel.startswith("../") or os.path.isabs(norm_rel):
        return path, f"路径不合法: {path}", False
    return norm_rel if norm_rel != "." else ".", None, False


def _normalize_acp_tool_args(tool_name: str, args: dict, cwd: str) -> tuple[dict, str, str | None, bool]:
    """统一归一化 ACP 工具参数里的项目路径, 避免 builder 各自漏处理。"""
    normalized_args = dict(args)
    path_keys = ("path", "file", "file_path", "filePath", "destination")
    primary_path = ""
    internal = False
    for key in path_keys:
        value = normalized_args.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        normalized, error, is_internal = _normalize_acp_project_path(value, cwd)
        if error:
            return normalized_args, value, error, False
        if is_internal:
            internal = True
        normalized_args[key] = normalized
        if not primary_path and key in ("path", "file", "file_path", "filePath"):
            primary_path = normalized
    for key in ("paths", "files"):
        values = normalized_args.get(key)
        if not isinstance(values, list):
            continue
        normalized_values = []
        for value in values:
            if not isinstance(value, str):
                normalized_values.append(value)
                continue
            normalized, error, is_internal = _normalize_acp_project_path(value, cwd)
            if error:
                return normalized_args, value, error, False
            if is_internal:
                internal = True
            normalized_values.append(normalized)
        normalized_args[key] = normalized_values
        if not primary_path and normalized_values:
            primary_path = str(normalized_values[0])
    return normalized_args, primary_path, None, internal


_ALLOWED_EXECUTABLE_PREFIXES = ("/bin/", "/usr/bin/", "/usr/local/bin/", "/opt/homebrew/bin/")
_LEADING_CD_RE = re.compile(r'^\s*cd\s+((?:"[^"]+"|\'[^\']+\'|[^;&|]+))\s*&&\s*(.+)$', re.S)
_ANY_CD_RE = re.compile(r'(?:^|[;&|]\s*)cd\s+((?:"[^"]+"|\'[^\']+\'|[^;&|\s]+))')


def _parse_shell_path_token(token: str) -> str:
    try:
        parts = shlex.split(token)
    except ValueError:
        return token.strip().strip('"\'')
    return parts[0] if parts else ""


def _resolve_command_path(token: str, cwd_norm: str) -> tuple[str, str] | None:
    """识别 shell token 中的路径值，并解析到绝对路径用于边界校验。"""
    if not token or token.startswith("http://") or token.startswith("https://"):
        return None
    value = token.split("=", 1)[1] if "=" in token else token
    if not value or value in (".", "-"):
        return None
    if value.startswith("~") or "$HOME" in value or "${HOME}" in value or "$PWD" in value or "${PWD}" in value:
        return value, value
    expanded = os.path.expanduser(value)
    if os.path.isabs(expanded):
        return value, os.path.normpath(expanded)
    if value == ".." or value.startswith("./") or value.startswith("../") or "/" in value:
        return value, os.path.normpath(os.path.join(cwd_norm, value))
    return None


def _rewrite_token_path(token: str, old_value: str, new_value: str) -> str:
    if "=" in token:
        key, _ = token.split("=", 1)
        return f"{key}={new_value}"
    return new_value


def _guard_acp_command_paths(command: str, cwd: str) -> tuple[str, str | None, dict[str, Any]]:
    """阻止 execute_command 越过 IDE 项目根; 合法项目绝对路径改写为相对路径。"""
    original = str(command or "")
    rewritten = original.strip()
    debug: dict[str, Any] = {"cwd": cwd, "rewritten": False, "absolutePaths": [], "relativePaths": []}
    if not rewritten or not cwd:
        return rewritten, None, debug
    cwd_norm = os.path.normpath(os.path.expanduser(cwd))
    leading_cd = _LEADING_CD_RE.match(rewritten)
    if leading_cd:
        cd_target = _parse_shell_path_token(leading_cd.group(1))
        debug["leadingCdTarget"] = cd_target
        resolved = _resolve_command_path(cd_target, cwd_norm)
        if resolved:
            _, abs_target = resolved
            if not _is_within_path(cwd_norm, abs_target):
                return rewritten, f"命令路径越界: cd 目标不在 IDE 项目根内: {cd_target}", debug
            rel = os.path.relpath(abs_target, cwd_norm).replace("\\", "/")
            rest = leading_cd.group(2).strip()
            rewritten = rest if rel == "." else f"cd {shlex.quote(rel)} && {rest}"
            debug["rewritten"] = True
    for match in _ANY_CD_RE.finditer(rewritten):
        cd_target = _parse_shell_path_token(match.group(1))
        resolved = _resolve_command_path(cd_target, cwd_norm)
        if resolved:
            _, abs_target = resolved
            if not _is_within_path(cwd_norm, abs_target):
                debug["cdTarget"] = cd_target
                return rewritten, f"命令路径越界: cd 目标不在 IDE 项目根内: {cd_target}", debug
    try:
        tokens = shlex.split(rewritten)
    except ValueError:
        return rewritten, "命令解析失败, 拒绝执行含不完整引号的命令", debug
    for index, token in enumerate(tokens):
        resolved = _resolve_command_path(token, cwd_norm)
        if not resolved:
            continue
        raw_value, abs_path = resolved
        if raw_value == abs_path and (raw_value.startswith("~") or "$" in raw_value):
            return rewritten, f"命令路径越界: 禁止使用 shell 展开路径: {token}", debug
        if index == 0 and abs_path.startswith(_ALLOWED_EXECUTABLE_PREFIXES):
            continue
        if os.path.isabs(raw_value):
            debug["absolutePaths"].append(raw_value)
        else:
            debug["relativePaths"].append(raw_value)
        if not _is_within_path(cwd_norm, abs_path):
            if os.path.isabs(raw_value):
                return rewritten, f"命令路径越界: 绝对路径不在 IDE 项目根内: {raw_value}", debug
            return rewritten, f"命令路径越界: 路径不在 IDE 项目根内: {raw_value}", debug
        if os.path.isabs(raw_value):
            rel = os.path.relpath(abs_path, cwd_norm).replace("\\", "/")
            replacement = _rewrite_token_path(token, raw_value, rel if rel != "." else ".")
            if replacement != token:
                rewritten = rewritten.replace(token, shlex.quote(replacement))
            else:
                rewritten = rewritten.replace(raw_value, shlex.quote(rel if rel != "." else "."))
            debug["rewritten"] = True
    return rewritten, None, debug


def _guard_acp_dangerous_command(command: str) -> str | None:
    """共享危险命令拦截; 非流式和流式终端必须一致。"""
    try:
        _lazy_import_agent_tools()
    except Exception as exc:
        logger.warning("[ACP] agent_tools 安全常量加载失败, 使用内置保底规则: {}", type(exc).__name__)
    lower_cmd = str(command or "").strip().lower()
    extra_dangerous = [
        "> /dev/sda", "parted", "fdisk", "dd if=/dev/zero of=/",
        "eval ", "$(", "`", "mkfs.", ":(){ :|:& };:",
    ]
    fallback_network = ["curl ", "wget ", "nc ", "netcat ", "ssh ", "scp ", "rsync ", "telnet "]
    for pattern in extra_dangerous:
        if pattern in lower_cmd:
            return f"❌ 危险命令已被拦截: pattern={pattern}"
    for pattern in _INLINE_IMPORTED.get("_DANGEROUS_BASH_ALWAYS", []):
        if pattern in lower_cmd:
            return f"❌ 危险命令已被拦截: pattern={pattern}"
    network_patterns = _INLINE_IMPORTED.get("_DANGEROUS_BASH_NETWORK", []) or fallback_network
    for pattern in network_patterns:
        if pattern in lower_cmd:
            return f"❌ 网络命令已被拦截: pattern={pattern}"
    return None


# 方案4: Autonomy blocked 阈值 — 同工具被拦截 3 次后引导 LLM 停止重试
_AUTONOMY_STOP_THRESHOLD = 3
_autonomy_counts: dict[str, int] = {}

def _handle_autonomy_blocked(tool_name: str, reason: str) -> str:
    """同工具被 autonomy 反复拦截时, 反馈 LLM 停止重试而非死循环。"""
    c = _autonomy_counts.get(tool_name, 0) + 1
    _autonomy_counts[tool_name] = c
    if c >= _AUTONOMY_STOP_THRESHOLD:
        return (
            f"操作 '{tool_name}' 已被拦截 {c} 次, 请立即停止重试。"
            f"改用其他方式完成目标。拦截原因: {reason[:100]}"
        )
    return (
        f"'{tool_name}' 需要审批 (第 {c}/{_AUTONOMY_STOP_THRESHOLD} 次)。"
        f"已提交审批请求。请勿重复尝试, 先执行其他任务。"
    )



def _list_cache_key_for(session_id: str, cwd: str, path: str, args: dict | None) -> str:
    from app.plugins.clawith_acp.list_dedup import normalize_list_key
    return normalize_list_key(session_id, cwd, path, args)


def _invalidate_list_cache_on_success(handler, tool_name: str, path: str, result: Any) -> None:
    if isinstance(result, dict) and result.get("error"):
        return
    s = str(result)
    if s.startswith("❌") or s.startswith('{"error"'):
        return
    _maybe_invalidate_list_cache(handler, tool_name, path)


def _maybe_invalidate_search_cache(handler) -> None:
    if not search_cache_enabled():
        return
    invalidate_search_cache_for_session(_get_session_id(handler))


def _maybe_invalidate_list_cache(handler, tool_name: str, path: str) -> None:
    from app.plugins.clawith_acp.list_dedup import (
        LIST_CACHE_INVALIDATE_TOOLS,
        invalidate_list_cache_for_path,
    )
    if tool_name not in LIST_CACHE_INVALIDATE_TOOLS:
        return
    session_id = _get_session_id(handler)
    cwd = getattr(handler, "_cwd", "") or ""
    parent = os.path.dirname((path or ".").replace("\\", "/")) or "."
    invalidate_list_cache_for_path(session_id, cwd, parent)
    _maybe_invalidate_search_cache(handler)



_FIND_FILES_DEBUG_LOG = "/Users/shubinzhang/Documents/agent/.cursor/debug-17de78.log"
_FIND_FILES_DEBUG_SESSION = "17de78"


def _find_files_debug_log(hypothesis_id: str, location: str, message: str, data: dict[str, Any]) -> None:
    # region agent log
    payload = {
        "sessionId": _FIND_FILES_DEBUG_SESSION,
        "timestamp": int(time.time() * 1000),
        "runId": "find-files-route",
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": _redact_debug_value(data),
    }
    try:
        os.makedirs(os.path.dirname(_FIND_FILES_DEBUG_LOG), exist_ok=True)
        with open(_FIND_FILES_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass
    # endregion


def _is_broad_glob_pattern(pattern: str) -> bool:
    """判断 find_files 的 glob 是否等价于列目录（避免走 agent storage）。"""
    p = (pattern or "").strip().replace(" ", "")
    if not p:
        return True
    if p in {"**/*", "*", "**", "**/**", "./**/*", "**/*.*"}:
        return True
    if p.endswith("/**") or p.endswith("/**/*"):
        return True
    return False


def _basename_glob(pattern: str) -> str:
  return (pattern or "").replace("\\", "/").split("/")[-1]




def _format_find_file_result(result: Any) -> str:
    if isinstance(result, dict):
        files = result.get("files") or []
        if not files:
            return "(未找到匹配的文件)"
        lines = []
        for f in files[:50]:
            if isinstance(f, dict):
                lines.append(f.get("path", f.get("name", "?")))
            else:
                lines.append(str(f))
        if len(files) > 50:
            lines.append(f"... (共 {len(files)} 个匹配)")
        return "\n".join(lines)
    return str(result)


def _format_search_text_result(result: Any) -> str:
    if isinstance(result, dict):
        matches = result.get("matches") or []
        if not matches:
            return "(未找到匹配的文本)"
        lines = []
        for m in matches[:30]:
            if isinstance(m, dict):
                ctx = m.get("context", "").strip()
                if len(ctx) > 120:
                    ctx = ctx[:117] + "..."
                lines.append(f"{m.get('file', '?')}:{m.get('line', '?')}:{m.get('column', '?')}  {ctx}")
            else:
                lines.append(str(m))
        total = result.get("totalCount", len(matches))
        if total > len(matches):
            lines.append(f"... (共 {total} 个匹配)")
        return "\n".join(lines)
    return str(result)



def _normalize_search_files_file_pattern(dir_path: str, file_pattern: str) -> str | None:
    """将 search_files 的 path + file_pattern 折进 IDE filePattern。"""
    fp = (file_pattern or "*").strip() or "*"
    p = (dir_path or "").strip().replace("\\", "/").strip("/")
    if not p or p == ".":
        return None if fp == "*" else fp
    if fp == "*":
        return f"{p}/**/*"
    if "/" not in fp and not fp.startswith("**"):
        return f"{p}/**/{fp}"
    return f"{p}/{fp}"


async def _try_acp_search_files(args: dict, handler) -> str | None:
    """IDE 会话中将 search_files 从 agent storage 改路由到 search_text。"""
    pattern = (args.get("pattern") or "").strip()
    if not pattern:
        return json.dumps(
            {"error": "Missing required argument 'pattern' for search_files"},
            ensure_ascii=False,
        )
    _cwd = getattr(handler, "_cwd", "")
    raw_path = args.get("path") or "."
    _norm_args, path, normalize_error, internal_path = _normalize_acp_tool_args(
        "search_files", {"path": raw_path, **args}, _cwd,
    )
    if normalize_error:
        return json.dumps({"error": normalize_error}, ensure_ascii=False)
    if internal_path:
        logger.debug("[ACP] search_files internal path, fallback storage path={}", raw_path)
        return None

    file_pattern = str(args.get("file_pattern") or "*")
    folded = _normalize_search_files_file_pattern(path or ".", file_pattern)
    mapped: dict[str, Any] = {
        "query": pattern,
        "regex": True,
        "caseSensitive": not bool(args.get("ignore_case", False)),
    }
    if folded:
        mapped["filePattern"] = folded
    elif file_pattern and file_pattern != "*":
        mapped["filePattern"] = file_pattern
    elif path and path not in (".", ""):
        logger.warning(
            "[ACP] search_files path={} has no IDE directory scope; searching whole project",
            path,
        )

    logger.info(
        "[ACP] route search_files→search_text query_len={} filePattern={}",
        len(pattern),
        mapped.get("filePattern", "*"),
    )
    return await _try_acp_execute("search_text", mapped, handler)



async def _try_acp_find_files(args: dict, handler) -> str | None:
    """IDE 会话中将 find_files 从 agent storage 改路由到 list_files / find_file。"""
    pattern = (args.get("pattern") or "").strip()
    if not pattern:
        pattern = "**/*"
    _cwd = getattr(handler, "_cwd", "")
    raw_path = args.get("path") or "."
    norm_args, path, normalize_error, internal_path = _normalize_acp_tool_args(
        "find_files", {"path": raw_path, **args}, _cwd,
    )
    if normalize_error:
        _find_files_debug_log(
            "H-FIND", "tool_bridge.py:_try_acp_find_files",
            "path normalize rejected",
            {"pattern": pattern, "rawPath": raw_path, "reason": normalize_error},
        )
        return json.dumps({"error": normalize_error}, ensure_ascii=False)
    if internal_path:
        _find_files_debug_log(
            "H-FIND", "tool_bridge.py:_try_acp_find_files",
            "skip internal agent path",
            {"pattern": pattern, "rawPath": raw_path},
        )
        return None

    route = "list_files"
    list_args: dict[str, Any] = {
        "path": path or ".",
        "depth": max(1, min(int(args.get("depth", 3) or 3), 10)),
        "limit": max(10, min(int(args.get("limit", 500) or 500), 2000)),
    }
    find_args: dict[str, Any] | None = None

    if _is_broad_glob_pattern(pattern):
        route = "list_files_broad"
    else:
        base = _basename_glob(pattern)
        if base.startswith("*.") and base.count("*") == 1 and base.count("?") == 0:
            route = "list_files_ext"
            list_args["depth"] = max(list_args["depth"], 5)
        elif base and ("*" in base or "?" in base):
            literal = base.replace("**", "").replace("*", "").replace("?", "").strip(".")
            if len(literal) >= 2:
                route = "find_file"
                find_args = {"query": literal}
        elif base and "*" not in base and "?" not in base:
            route = "find_file"
            find_args = {"query": base}

    _find_files_debug_log(
        "H-FIND", "tool_bridge.py:_try_acp_find_files",
        "route find_files to IDE",
        {"pattern": pattern, "route": route, "path": path or ".", "cwd": _cwd},
    )

    if route == "find_file" and find_args:
        result = await _try_acp_execute("find_file", find_args, handler)
        if result and not result.startswith("❌") and "No files" not in result:
            return result

    result = await _try_acp_execute("list_files", list_args, handler)
    if route == "list_files_ext" and result and not result.startswith("❌"):
        ext = _basename_glob(pattern)[1:]
        lines = [
            ln for ln in result.splitlines()
            if ln.strip() and (ln.rstrip().endswith(ext) or f"{ext} " in ln or f"{ext}(" in ln)
        ]
        if lines:
            header = next((ln for ln in result.splitlines() if ln.strip()), "")
            body = "\n".join(lines[:100])
            return f"📂 Found {len(lines)} item(s) matching '{pattern}':\n{body}" if header else body
    return result



async def _try_acp_execute(tool_name: str, args: dict, handler) -> str | None:
    """通过 ACP 协议执行文件操作。

    返回 None 表示不应由 ACP 处理（如 path 为空或 agent 自身文件）。
    返回字符串表示 ACP 执行结果。
    """
    if tool_name == "find_files":
        return await _try_acp_find_files(args, handler)
    if tool_name == "search_files":
        return await _try_acp_search_files(args, handler)

    method = _ACP_METHOD_MAP.get(tool_name)
    if not method:
        return None

    _cwd = getattr(handler, '_cwd', '')
    raw_path = args.get("path") or args.get("file") or args.get("file_path") or args.get("filePath", "")
    args, path, normalize_error, internal_path = _normalize_acp_tool_args(tool_name, args, _cwd)
    if normalize_error:
        logger.warning("[ACP-PATH] reject tool={} raw_path={} cwd={} reason={}", tool_name, raw_path, _cwd, normalize_error)
        _agent_debug_log(
            "fix-acp-path-guard", "H1", "tool_bridge.py:_try_acp_execute",
            "reject project path",
            {"tool": tool_name, "rawPath": raw_path, "cwd": _cwd, "reason": normalize_error},
        )
        return json.dumps({"error": normalize_error}, ensure_ascii=False)
    if internal_path:
        _agent_debug_log(
            "fix-acp-path-guard", "H1", "tool_bridge.py:_try_acp_execute",
            "skip internal path",
            {"tool": tool_name, "rawPath": raw_path, "normalizedPath": path},
        )
        return None
    _agent_debug_log(
        "fix-acp-path-guard", "H1", "tool_bridge.py:_try_acp_execute",
        "normalize project path",
        {"tool": tool_name, "rawPath": raw_path, "normalizedPath": path, "cwd": _cwd},
    )

    # Autonomy 闸门: 写操作需要经过 check_tool_autonomy 检查
    # 删除类工具由 ACP 插件 DeletePermissionRow 把关，跳过后端 L3 web 审批阻断
    _DELETE_TOOLS_PLUGIN_GATED = frozenset({"delete_file", "safe_delete"})
    if tool_name in WORKSPACE_WRITE_TOOLS and tool_name not in _DELETE_TOOLS_PLUGIN_GATED:
        _lazy_import_agent_tools()
        # 确保 agent_tools.py 的 _TOOL_AUTONOMY_MAP 包含 ACP 写工具映射 (安全审计 V2)
        _autonomy_map = _INLINE_IMPORTED.get("_TOOL_AUTONOMY_MAP", {})
        _acp_autonomy_entries = {
            "edit_file": "write_workspace_files",
            "refactor_rename": "write_workspace_files",
            "safe_delete": "delete_files",
            "reformat_code": "write_workspace_files",
            "optimize_imports": "write_workspace_files",
            "convert_java_to_kotlin": "write_workspace_files",
            "execute_command": "execute_code",
        }
        for k, v in _acp_autonomy_entries.items():
            if k not in _autonomy_map:
                _autonomy_map[k] = v
        _check_fn = _INLINE_IMPORTED.get("check_tool_autonomy")
        if _check_fn is not None:
            _agent_id = getattr(handler, "agent_id", None)
            _user_id = getattr(handler, "user_id", None)
            if _agent_id is not None and _user_id is not None:
                _block = await _check_fn(tool_name, args, _agent_id, _user_id, notify=False)
                if _block is not None:
                    logger.warning(f"[ACP] autonomy blocked: {tool_name} reason={_block[:60]}")
                    return _handle_autonomy_blocked(tool_name, _block)

    if not method:
        return None

    # 速率限制: 读搜索工具受 60次/分钟 限流
    _READ_METHODS = frozenset({
        "fs/read_text_file", "fs/list_directory", "fs/find_file", "fs/search_text",
        "fs/find_class", "fs/find_symbol", "ide/index_status",
        "fs/find_references", "fs/find_definition", "fs/find_implementations",
        "fs/find_super_methods", "fs/call_hierarchy", "fs/type_hierarchy",
        "fs/diagnostics", "ide/sync_files", "ide/active_file", "fs/file_structure",
    })
    if method in _READ_METHODS and not await _RATE_LIMITER.allow(method):
        left = _RATE_LIMITER.remaining(method)
        logger.warning(f"[ACP] rate limited: {method} remaining={left}")
        return f"⚠️ 请求过频繁（{method}），请稍后重试"

    # 写操作独立限速: 10次/60s (安全审计 V7)
    _WRITE_METHODS = frozenset({
        "fs/write_text_file", "fs/edit_text_file",
    })
    if method in _WRITE_METHODS and not await _RATE_LIMITER.allow(method, limit=30):
        logger.warning(f"[ACP] 写操作 {method} 超限")
        return '{"error": "写操作频率超限，请稍后重试"}'

    session_id = _get_session_id(handler)
    conn_id = getattr(handler, "conn_id", "?")

    # Dict dispatch: 从注册表查找参数构建器
    builder = _ACP_PARAM_BUILDERS.get(method)
    if builder is None:
        return None

    result = await builder(tool_name, args, handler, session_id, path)
    if isinstance(result, str):
        return result  # 错误字符串直接返回
    params = result

    t0 = time.perf_counter()
    logger.info(f"[ACP-PERF] fs START tool={tool_name} path={path} session={session_id}")
    try:
        # P2-1: 文件大小阶梯超时; build_project 使用独立长超时。
        _timeout = _timeout_for_acp_method(method, params)
        logger.debug(
            f"[ACP-FS] timeout tier tool={tool_name} method={method} "
            f"timeout={_timeout}s"
        )
        if method == "fs/list_directory":
            from app.plugins.clawith_acp.list_dedup import get_cached_list

            _lk = _list_cache_key_for(session_id, _cwd, path, params)
            _cached_list = get_cached_list(_lk)
            if _cached_list is not None:
                logger.info(
                    "[LIST-DEDUP] cache hit session={} path={}",
                    (session_id or "")[:8],
                    os.path.basename(path or "."),
                )
                return _cached_list
            logger.debug("[ACP] list_files MISS path={}", path or ".")
        if method in ("fs/search_text", "fs/find_file") and search_cache_enabled():
            _sk = normalize_search_key(method, session_id, params)
            _cached_search = get_cached_search(_sk)
            if _cached_search is not None:
                logger.info(
                    "[SEARCH-DEDUP] cache hit session={} method={}",
                    (session_id or "")[:8],
                    method,
                )
                return _cached_search
        result = await coalesce_or_execute(
            method, path, session_id,
            executor=lambda: polite_send(handler, method, params, timeout=_timeout),
            args=params,
        )
        _elapsed = time.perf_counter() - t0
        logger.info(
            f"[ACP-RTT] tool={tool_name} method={method} rtt_ms={_elapsed*1000:.0f} "
            f"result_len={len(str(result))} path={path} session={session_id}"
        )
        # 第二梯队: 基线延迟追踪 (每方法族 P50/P95/P99 统计)
        record_baseline_rtt(method, _elapsed)
        if tool_name in ("write_file", "edit_file", "move_file", "delete_file", "sync_files"):
            _invalidate_list_cache_on_success(handler, tool_name, path, result)
        # P1-6: 检测 IDE 索引未就绪，引导 LLM 等待而非反复重试
        if isinstance(result, str) and ("索引未就绪" in result or "索引构建中" in result):
            logger.warning(f"[ACP] {tool_name} blocked by IDE indexing: {result[:100]}")
            return (
                f"{result}\n\n"
                "⚠️ IDE 正在构建代码索引，所有代码搜索/导航暂时不可用。"
                "请等待索引完成后重试（通常需要 10-60 秒），不要反复重试搜索类工具。"
                "可调用 index_status 检查索引进度。"
            )
        if method == "fs/read_text_file":
            if isinstance(result, dict):
                return result.get("content", "")
            return str(result)
        if method == "fs/list_directory":
            from app.plugins.clawith_acp.list_dedup import (
                format_list_directory_result,
                store_list_result,
            )
            _list_args = {"depth": params.get("depth"), "limit": params.get("limit")}
            _out = format_list_directory_result(result)
            store_list_result(
                _list_cache_key_for(session_id, _cwd, path, _list_args),
                _out,
            )
            logger.info(
                "[ACP] list_files {} entries path={}",
                _out.count("\n") + 1 if _out else 0,
                os.path.basename(path or "."),
            )
            return _out
        if method == "fs/find_file":
            _out = _format_find_file_result(result)
            if search_cache_enabled():
                store_search_result(normalize_search_key(method, session_id, params), _out)
            return _out
        if method == "fs/find_class":
            if isinstance(result, dict):
                classes = result.get("classes") or result.get("files") or result.get("results") or []
                if not classes:
                    return "(未找到匹配的类)"
                lines = []
                for c in classes[:50]:
                    if isinstance(c, dict):
                        lines.append(c.get("qualifiedName", c.get("path", c.get("name", "?"))))
                    else:
                        lines.append(str(c))
                total = result.get("totalCount", len(classes))
                if total > len(classes):
                    lines.append(f"... (共 {total} 个匹配)")
                return "\n".join(lines)
            return str(result)
        if method == "fs/find_symbol":
            if isinstance(result, dict):
                symbols = result.get("results") or result.get("symbols") or []
                if not symbols:
                    return "(未找到匹配的符号)"
                lines = []
                for s in symbols[:50]:
                    if isinstance(s, dict):
                        lines.append(f"{s.get('name', '?')}  ({s.get('kind', '?')})  {s.get('file', s.get('path', ''))}:{s.get('line', '?')}")
                    else:
                        lines.append(str(s))
                total = result.get("totalCount", len(symbols))
                if total > len(symbols):
                    lines.append(f"... (共 {total} 个匹配)")
                return "\n".join(lines)
            return str(result)
        if method == "ide/index_status":
            if isinstance(result, dict):
                dumb = result.get("isDumbMode", False)
                indexing = result.get("isIndexing", False)
                return f"索引状态: isDumbMode={dumb} isIndexing={indexing}"
            return str(result)
        if method == "ide/build_project":
            if isinstance(result, dict):
                success = result.get("success", False)
                errors = result.get("errors", 0)
                warnings = result.get("warnings", 0)
                msgs = result.get("buildMessages", [])
                aborted = result.get("aborted", False)
                elapsed = result.get("durationMs", 0)
                lines = [f"构建结果: success={success} errors={errors} warnings={warnings} aborted={aborted} 耗时={elapsed}ms"]
                if msgs:
                    for m in msgs[:30]:
                        loc = f"{m.get('file', '?')}:{m.get('line', '?')}:{m.get('column', '?')}"
                        lines.append(f"  [{m.get('category','')}] {loc} {m.get('message','')}")
                    if len(msgs) > 30:
                        lines.append(f"  ... (共 {len(msgs)} 条消息)")
                if result.get("truncated"):
                    lines.append("  (结果已截断，仅显示前 100 条)")
                if result.get("rawOutput"):
                    lines.append(f"\n原始编译输出:\n{result['rawOutput']}")
                return "\n".join(lines)
            return str(result)
        if method == "fs/search_text":
            _out = _format_search_text_result(result)
            if search_cache_enabled():
                store_search_result(normalize_search_key(method, session_id, params), _out)
            return _out
        # ── 新增: find_references / find_definition / find_implementations 等结果处理器 ──
        if method == "fs/find_references":
            if isinstance(result, dict):
                usages = result.get("usages", [])
                if not usages:
                    return "(未找到引用)"
                lines = [f"**引用结果** ({result.get('totalCount', 0)} 条):"]
                for u in usages[:50]:
                    f = u.get("file", "?"); l = u.get("line", "?")
                    t = u.get("type", "?"); ctx = (u.get("context") or "")[:80]
                    lines.append(f"- `{f}:{l}` [{t}] {ctx}")
                return "\n".join(lines)
        if method == "fs/find_definition":
            if isinstance(result, dict):
                err = result.get("error")
                if err: return f"⚠️ {err}"
                return (
                    f"**定义**: `{result.get('symbolName', '?')}` → "
                    f"`{result.get('file', '?')}:{result.get('line', '?')}:{result.get('column', '?')}`\n"
                    f"```\n{result.get('preview', '')}\n```"
                )
        if method == "fs/find_implementations":
            if isinstance(result, dict):
                impls = result.get("implementations", [])
                if not impls: return "(未找到实现)"
                lines = [f"**实现列表** ({result.get('totalCount', 0)} 条):"]
                for i in impls[:30]:
                    lines.append(
                        f"- `{i.get('name', '?')}` ({i.get('kind', '?')}, "
                        f"{i.get('language', '')}) → `{i.get('file', '?')}`"
                    )
                return "\n".join(lines)
        if method == "fs/find_super_methods":
            if isinstance(result, dict):
                err = result.get("error")
                if err: return f"⚠️ {err}"
                hierarchy = result.get("hierarchy", [])
                if not hierarchy:
                    return f"**父方法**: `{result.get('method', {}).get('name', '?')}` (无继承链)"
                lines = [f"**方法继承链** ({len(hierarchy)} 层):"]
                for h in hierarchy:
                    prefix = "  " * h.get("depth", 0) + ("└─" if h.get("depth", 0) > 0 else "")
                    lines.append(f"{prefix}`{h.get('containingClass', '?')}#{h.get('name', '?')}`")
                return "\n".join(lines)
        if method == "fs/call_hierarchy":
            if isinstance(result, dict):
                err = result.get("error")
                if err: return f"⚠️ {err}"
                el = result.get("element", {})
                calls = result.get("calls", [])
                lines = [f"**调用层次**: `{el.get('name', '?')}`"]
                for c in calls[:50]:
                    lines.append(f"- `{c.get('name', '?')[:80]}` → `{c.get('file', '?')}:{c.get('line', '?')}`")
                return "\n".join(lines)
        if method == "fs/type_hierarchy":
            if isinstance(result, dict):
                err = result.get("error")
                if err: return f"⚠️ {err}"
                el = result.get("element", {})
                supers = result.get("supertypes", [])
                subs = result.get("subtypes", [])
                lines = [f"**类型层次**: `{el.get('name', '?')}`"]
                if supers:
                    lines.append(f"  超类型 ({len(supers)}): " + ", ".join(
                        s.get("name", "?") for s in supers[:10]))
                if subs:
                    lines.append(f"  子类型 ({len(subs)}): " + ", ".join(
                        s.get("name", "?") for s in subs[:20]))
                return "\n".join(lines)
        if method == "fs/diagnostics":
            if isinstance(result, dict):
                problems = result.get("problems", [])
                intentions = result.get("intentions", [])
                build_errors = result.get("buildErrors") or result.get("buildMessages") or []
                if not problems and not intentions and not build_errors:
                    return "(无诊断问题)"
                lines = [f"**诊断结果** ({len(problems)} 问题, {len(intentions)} 快速修复):"]
                for p in problems[:30]:
                    sev = p.get("severity", "?"); msg = p.get("message", "?")
                    f = p.get("file", "?"); l = p.get("line", "?")
                    lines.append(f"- [{sev}] `{f}:{l}` {msg}")
                if intentions:
                    lines.append(f"\n**快速修复建议**:")
                    for i in intentions[:20]:
                        lines.append(f"- {i.get('description', '?')}")
                if build_errors:
                    lines.append(f"\n**编译错误**:")
                    for be in build_errors[:10]:
                        lines.append(f"- {be.get('message', '?')}")
                return "\n".join(lines)
        if method == "fs/file_structure":
            if isinstance(result, dict):
                err = result.get("error")
                if err: return f"⚠️ {err}"
                return (
                    f"**文件结构** ({result.get('language', '')}):\n"
                    f"```\n{result.get('structure', '')}\n```"
                )
        if method == "fs/get_documentation":
            if isinstance(result, dict):
                err = result.get("error")
                if err: return f"⚠️ {err}"
                doc = result.get("documentation", "")
                return f"**API 文档**:\n```\n{doc}\n```" if doc else "(无文档)"
        if method == "git/status":
            if isinstance(result, dict):
                output = result.get("output", "")
                return output if output else "(无 Git 变更)"
            return str(result)
        if method == "git/diff":
            if isinstance(result, dict):
                output = result.get("output", "")
                return output if output else "(无 diff 输出)"
            return str(result)
        if method == "ide/active_file":
            if isinstance(result, dict):
                files = result.get("files", [])
                if not files:
                    return "(无活动文件)"
                lines = [f"**活动文件** ({result.get('count', len(files))} 个):"]
                for f in files[:10]:
                    lines.append(f"- `{f.get('path', '?')}` ({f.get('language', '?')})")
                return "\n".join(lines)
            return str(result)
        return "文件操作成功"
    except TimeoutError:
        logger.warning(f"[ACP] timeout: {tool_name} {path}, 尝试本地读取")
        if method == "fs/read_text_file" and _cwd:
            cwd_norm = os.path.normpath(os.path.expanduser(_cwd))
            local_path = os.path.normpath(os.path.join(cwd_norm, path))
            if _is_within_path(cwd_norm, local_path) and os.path.isfile(local_path):
                try:
                    with open(local_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    logger.info(f"[ACP] 本地读取成功: {path} ({len(content)} bytes)")
                    return content
                except Exception as e:
                    logger.error(f"[ACP] 本地读取失败: {path}: {e}")
                    return f"⚠️ 文件读取失败: {e}"
        return f"⚠️ IDE 文件操作超时: {path}"
    except Exception as e:
        err_msg = str(e)
        if "-32601" in err_msg:
            # P0-8: fs/list_directory 不被 IDE 支持时，回落本地终端 find 命令枚举目录
            # 避免 list_files 在旧版本 IDE 插件上完全不可用，保持功能降级可用
            if method == "fs/list_directory":
                logger.warning(
                    "[ACP] fs/list_directory 不支持, 回落 terminal find: %s", path
                )
                # H9: shlex.quote 转义路径特殊字符，防止 shell 命令注入
                find_cmd = f"find {shlex.quote(path)} -maxdepth {params.get('depth', 3)} -type f -o -type d | head -{params.get('limit', 200)}"
                return await _try_acp_terminal({"command": find_cmd}, handler)
            logger.warning(
                "[ACP] %s 不支持: %s (IDE 插件需实现对应 handler)",
                tool_name, path
            )
            return f"❌ IDE 插件不支持此操作: {tool_name}（需升级插件版本）"
        if tool_name == "find_super_methods" and "-32602" in err_msg:
            logger.warning("[ACP] find_super_methods 参数/定位无效: path={} err={}", path, err_msg[:120])
            return (
                "⚠️ find_super_methods 未能定位到方法。"
                "请传 file+line+column，且光标位置必须在方法体或方法声明内；"
                "或传 symbol='com.example.Class#methodName'。"
            )
        # ── 文件不存在去重: 同路径失败 ≥3 次时拦截 ──
        if tool_name in ("read_file", "find_file", "list_files") and path:
            key = f"{tool_name}:{path.lower().rstrip('/')}"
            fails = _failed_paths.get(key, 0) + 1
            _failed_paths[key] = fails
            if fails >= _FAILED_PATH_BLOCK:
                logger.warning(
                    "[ACP] {} BLOCKED: path={} fails={}/{}",
                    tool_name, path, fails, _FAILED_PATH_BLOCK
                )
                del _failed_paths[key]
                return (
                    f"⛔ 文件/目录 '{path}' 已被确认不存在 (已尝试 {fails} 次)。\n"
                    f"请用 list_directory 确认正确的目录结构，不要再猜测此路径。"
                )
            elif fails >= 2:
                logger.warning(
                    "[ACP] {} 重复失败: path={} fails={}/{}",
                    tool_name, path, fails, _FAILED_PATH_BLOCK
                )
        logger.error(
            "[ACP-bridge conn={} error: {} path={} session={}: {}",
            conn_id, tool_name, path, session_id, e
        )
        return f"⚠️ IDE 文件操作失败: {e}"


async def _try_acp_terminal(
    args: dict,
    handler,
    policy: "TerminalPolicy | None" = None,
) -> str | None:
    """blocking 终端 — wait_for_exit 后返回完整 output + exitCode。"""
    from .terminal_policy import resolve_terminal_policy

    command = args.get("command", "")
    if not command:
        return None

    _cwd = getattr(handler, '_cwd', '')
    guarded_command, guard_error, guard_debug = _guard_acp_command_paths(command, _cwd)
    _agent_debug_log(
        "fix-acp-path-guard", "H2", "tool_bridge.py:_try_acp_terminal",
        "guard execute command",
        {"cwd": _cwd, "commandLen": len(command), "error": guard_error, **guard_debug},
    )
    if guard_error:
        logger.warning("[ACP-CMD] reject cmd_len={} cwd={} reason={}", len(command), _cwd, guard_error)
        return guard_error
    command = guarded_command
    args = {**args, "command": command}

    guard_msg = _guard_acp_dangerous_command(command)
    if guard_msg:
        logger.warning(f"[ACP] {guard_msg} cmd={command[:80]}")
        return guard_msg

    if policy is None:
        policy = resolve_terminal_policy(command)

    # P0-3: autonomy 审批闸门
    agent_id = getattr(handler, "agent_id", None)
    user_id = getattr(handler, "user_id", None)
    if agent_id is not None and user_id is not None:
        _check_fn = _INLINE_IMPORTED.get("check_tool_autonomy")
        if _check_fn is not None:
            block = await _check_fn("execute_command", args, agent_id, user_id, notify=False)
            if block is not None:
                logger.warning(f"[ACP] autonomy blocked: cmd={command[:80]} reason={block[:60]}")
                return _handle_autonomy_blocked("execute_command", block)

    session_id = getattr(handler, "session_id", "")
    conn_id = getattr(handler, "conn_id", "?")
    terminal_id = ""
    t0 = time.perf_counter()
    wait_timeout = policy.timeout_seconds + 30
    logger.info(
        "[ACP-TERM] BLOCKING START bucket={} timeoutSec={} session={} cmd={}",
        policy.bucket,
        int(policy.timeout_seconds),
        session_id,
        command[:120],
    )
    try:
        create_result = await handler.send_request("terminal/create", {
            "sessionId": session_id,
            "command": command,
        }, timeout=float(os.getenv("ACP_TERMINAL_CREATE_TIMEOUT", "30")))

        if isinstance(create_result, dict):
            terminal_id = create_result.get("terminalId", "")

        if not terminal_id:
            return "终端创建失败"

        # 2. 等待命令执行完毕并读取 exit code
        # P0-4: wait_for_exit 返回的 exitCode 标识命令成败，LLM 需据此判断构建/测试是否通过
        wait_result = await handler.send_request("terminal/wait_for_exit", {
            "sessionId": session_id,
            "terminalId": terminal_id,
        }, timeout=wait_timeout)
        exit_code = -1
        if isinstance(wait_result, dict):
            exit_code = int(wait_result.get("exitCode", -1))

        # 3. 读取终端输出
        output = await handler.send_request("terminal/output", {
            "sessionId": session_id,
            "terminalId": terminal_id,
        }, timeout=float(os.getenv("ACP_TERMINAL_OUTPUT_TIMEOUT", "30")))

        if isinstance(output, dict):
            out_text = output.get("output", "")
        else:
            out_text = str(output) if output else "命令执行完毕，无输出"

        elapsed = time.perf_counter() - t0
        logger.info(
            "[ACP-TERM] BLOCKING DONE bucket={} timeoutSec={} elapsed={:.3f}s "
            "outputLen={} exitCode={} session={} terminalId={}",
            policy.bucket,
            int(policy.timeout_seconds),
            elapsed,
            len(out_text),
            exit_code,
            session_id,
            terminal_id,
        )
        if exit_code != 0:
            return f"❌ 命令失败 (exit={exit_code}):\n{out_text}"
        return out_text

    except TimeoutError:
        logger.error(
            "[ACP-TERM] BLOCKING TIMEOUT bucket={} elapsed={:.3f}s session={} cmd={}",
            policy.bucket,
            time.perf_counter() - t0,
            session_id,
            command[:80],
        )
        return f"⚠️ 终端命令超时: {command[:80]}"
    except Exception as e:
        logger.error(
            "[ACP-TERM] BLOCKING error conn={} session={} cmd={}: {}",
            conn_id,
            session_id,
            command[:80],
            e,
        )
        return f"⚠️ 终端执行失败: {e}"
    finally:
        if terminal_id:
            try:
                await handler.send_request(
                    "terminal/release",
                    {"sessionId": session_id, "terminalId": terminal_id},
                    timeout=10,
                )
            except Exception as rel_err:
                logger.warning(
                    "[ACP-TERM] BLOCKING release failed terminalId={}: {}",
                    terminal_id,
                    rel_err,
                )


async def _try_acp_terminal_streaming(
    args: dict,
    handler,
    cancel_event: asyncio.Event | None = None,
    policy: "TerminalPolicy | None" = None,
) -> str | None:
    """流式 Terminal — 仅 tail -f / watch；不以静默误判 DISAPPEARED。"""
    from .terminal_policy import resolve_terminal_policy

    command = args.get("command", "")
    if not command:
        return None

    _cwd = getattr(handler, '_cwd', '')
    guarded_command, guard_error, guard_debug = _guard_acp_command_paths(command, _cwd)
    _agent_debug_log(
        "fix-acp-path-guard", "H2", "tool_bridge.py:_try_acp_terminal_streaming",
        "guard execute command",
        {"cwd": _cwd, "commandLen": len(command), "error": guard_error, **guard_debug},
    )
    if guard_error:
        logger.warning("[ACP-CMD] reject streaming cmd_len={} cwd={} reason={}", len(command), _cwd, guard_error)
        return guard_error
    command = guarded_command
    args = {**args, "command": command}

    guard_msg = _guard_acp_dangerous_command(command)
    if guard_msg:
        logger.warning(f"[ACP] {guard_msg} streaming cmd={command[:80]}")
        return guard_msg

    if policy is None:
        policy = resolve_terminal_policy(command)

    session_id = getattr(handler, "session_id", "")
    conn_id = getattr(handler, "conn_id", "?")

    # CR1: 从 ACP SDK 解嵌套 exitStatus.exitCode (兼容旧版直接 exitCode)
    def _get_exit_code(resp: dict) -> int | None:
        es = resp.get("exitStatus") if isinstance(resp, dict) else None
        if isinstance(es, dict) and es.get("exitCode") is not None:
            return int(es["exitCode"])
        code = resp.get("exitCode") if isinstance(resp, dict) else None
        return int(code) if code is not None else None

    # P2-2: autonomy 检查 (ACP 会话 notify=False 跳过外部通知)
    _lazy_import_agent_tools()
    agent_id = getattr(handler, "agent_id", None)
    user_id = getattr(handler, "user_id", None)
    if agent_id is not None and user_id is not None:
        _check_fn = _INLINE_IMPORTED.get("check_tool_autonomy")
        if _check_fn is not None:
            block = await _check_fn("execute_command", args, agent_id, user_id, notify=False)
            if block is not None:
                logger.warning(
                    f"[ACP-PERF] terminal-streaming autonomy blocked conn={conn_id} "
                    f"cmd={command[:80]} reason={block[:60]}"
                )
                return block

    logger.info(
        f"[ACP-PERF] terminal-streaming START conn={conn_id} "
        f"session={session_id} cmd={command[:80]}"
    )

    # Step 1: 创建 terminal 进程
    try:
        create_resp = await handler.send_request(
            "terminal/create",
            {"sessionId": session_id, "command": command},
            timeout=float(os.getenv("ACP_TERMINAL_CREATE_TIMEOUT", "30")),
        )
    except Exception as e:
        logger.error(
            f"[ACP-PERF] terminal-streaming CREATE-FAIL conn={conn_id} "
            f"session={session_id}: {e}"
        )
        return f"terminal 创建失败: {e}"

    if isinstance(create_resp, dict) and create_resp.get("error"):
        logger.error(
            f"[ACP-PERF] terminal-streaming CREATE-FAIL conn={conn_id}: {create_resp['error']}"
        )
        return f"terminal 创建失败: {create_resp['error']}"
    terminal_id = create_resp.get("terminalId", "") if isinstance(create_resp, dict) else ""
    if not terminal_id:
        logger.error(
            f"[ACP-PERF] terminal-streaming CREATE-FAIL conn={conn_id}: no terminalId"
        )
        return "terminal 创建失败: 未返回 terminalId"
    logger.info(
        f"[ACP-PERF] terminal-streaming CREATE-OK conn={conn_id} terminalId={terminal_id}"
    )

    # Phase 1: 500ms 静默等待 — 短命令直接返回
    await asyncio.sleep(0.5)

    try:
        check = await handler.send_request(
            "terminal/output",
            {"sessionId": session_id, "terminalId": terminal_id},
            timeout=float(os.getenv("ACP_TERMINAL_OUTPUT_TIMEOUT", "30")),
        )
    except Exception as e:
        logger.error(
            f"[ACP-PERF] terminal-streaming CHECK-FAIL conn={conn_id} "
            f"session={session_id}: {e}"
        )
        return f"terminal 检查失败: {e}"

    exit_code = _get_exit_code(check) if isinstance(check, dict) else None
    if exit_code is not None:
        out_text = check.get("output", "") if isinstance(check, dict) else ""
        logger.info(
            f"[ACP-PERF] terminal-streaming SHORT-CMD conn={conn_id} "
            f"session={session_id} exitCode={exit_code} outputLen={len(out_text)}"
        )
        if exit_code != 0:
            return f"命令失败 (exit={exit_code}):\n{out_text}"
        return out_text

    # Phase 2: 300ms 流式轮询
    logger.info(
        f"[ACP-PERF] terminal-streaming PHASE2 conn={conn_id} "
        f"session={session_id} reason=exceeded 500ms silence"
    )
    # H2: 独立 Event, 与 prompt cancel_event 解耦
    terminal_cancel = asyncio.Event()
    seq = 0
    # H1+R7: 增量跟踪, 从 Phase1 输出长度开始避免重复
    _last_output_len = len(check.get("output", "")) if isinstance(check, dict) else 0
    deadline = time.monotonic() + policy.timeout_seconds

    async def poll_loop():
        nonlocal seq, _last_output_len
        _key = f"terminal-stream-{terminal_id}"
        try:
            while True:
                # 取消检查 (terminal_cancel 或 handler cleanup)
                if terminal_cancel.is_set():
                    logger.warning(
                        f"[ACP-PERF] terminal-streaming CANCELLED conn={conn_id} "
                        f"session={session_id} terminalId={terminal_id}"
                    )
                    try:
                        await handler.send_request(
                            "terminal/kill",
                            {"sessionId": session_id, "terminalId": terminal_id},
                            timeout=5,
                        )
                        logger.warning(
                            f"[ACP-PERF] terminal-streaming KILLED conn={conn_id}"
                        )
                    except Exception as e:
                        logger.warning(
                            f"[ACP-PERF] terminal-streaming KILL-FAIL conn={conn_id}: {e}"
                        )
                    break

                # 超时保护 (600s, 对齐 hermes-agent)
                if time.monotonic() > deadline:
                    logger.error(
                        f"[ACP-PERF] terminal-streaming TIMEOUT conn={conn_id} "
                        f"session={session_id}"
                    )
                    try:
                        await handler.send_request(
                            "terminal/kill",
                            {"sessionId": session_id, "terminalId": terminal_id},
                            timeout=5,
                        )
                    except Exception as e:
                        logger.warning(
                            f"[ACP-PERF] terminal-streaming KILL-FAIL conn={conn_id}: {e}"
                        )
                    await handler._send_notification("session/update", {
                        "sessionId": session_id,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": f"\n\n[timeout: {int(policy.timeout_seconds)}s]"},
                        },
                    })
                    break

                # 获取 terminal 输出
                try:
                    output = await handler.send_request(
                        "terminal/output",
                        {"sessionId": session_id, "terminalId": terminal_id},
                        timeout=float(os.getenv("ACP_TERMINAL_OUTPUT_TIMEOUT", "30")),
                    )
                except Exception as e:
                    err_msg = str(e)
                    # R1: WS 断开时直接终止, 避免 2000 次空循环
                    if "WebSocket" in err_msg or "Connection" in err_msg:
                        logger.error(
                            f"[ACP-PERF] terminal-streaming WS-DISCONNECT conn={conn_id}: {e}"
                        )
                        break
                    logger.warning(
                        f"[ACP-PERF] terminal-streaming POLL-FAIL conn={conn_id} "
                        f"session={session_id}: {e}"
                    )
                    await asyncio.sleep(0.3)
                    continue

                if not isinstance(output, dict):
                    await asyncio.sleep(0.3)
                    continue

                # H1: 只发送增量 (避免全量重复)
                new_text = output.get("output", "")
                if len(new_text) > _last_output_len:
                    seq += 1
                    try:
                        await handler._send_notification("session/update", {
                            "sessionId": session_id,
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {
                                    "type": "text",
                                    "text": f"[TERM] {new_text[_last_output_len:]}",
                                },
                            },
                        })
                    except Exception:
                        # R4: WS 断开时 notification 失败, 终止轮询
                        break
                    _last_output_len = len(new_text)
                # 无新输出时继续 poll — 构建工具启动期可长时间零输出

                if isinstance(output, dict) and output.get("error"):
                    err_text = str(output.get("error", ""))
                    if "不存在" in err_text or "not found" in err_text.lower():
                        logger.warning(
                            "[ACP-PERF] terminal-streaming GONE conn={} session={} err={}",
                            conn_id,
                            session_id,
                            err_text[:80],
                        )
                        break

                # CR1: 解嵌套检查 exitCode
                exit_code = _get_exit_code(output)
                if exit_code is not None:
                    logger.info(
                        f"[ACP-PERF] terminal-streaming DONE conn={conn_id} "
                        f"session={session_id} exitCode={exit_code} total_chunks={seq}"
                    )
                    # M1: exit 用 agent_message_chunk (确保文本可见)
                    try:
                        await handler._send_notification("session/update", {
                            "sessionId": session_id,
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {
                                    "type": "text",
                                    "text": f"\n\n[exit: {exit_code}]",
                                },
                            },
                        })
                    except Exception:
                        pass
                    break

                await asyncio.sleep(0.3)

        except asyncio.CancelledError:
            # R3: CancelledError 路径也发送 kill (task.cancel() 不触发 event 检查)
            logger.warning(
                f"[ACP-PERF] terminal-streaming CANCELLED (task) conn={conn_id} "
                f"session={session_id}"
            )
            try:
                await handler.send_request(
                    "terminal/kill",
                    {"sessionId": session_id, "terminalId": terminal_id},
                    timeout=5,
                )
            except Exception:
                pass
        except Exception as e:
            logger.error(
                f"[ACP-PERF] terminal-streaming poll_loop error conn={conn_id} "
                f"session={session_id}: {e}"
            )
        finally:
            handler._active_tasks.pop(_key, None)
            getattr(handler, "_terminal_events", {}).pop(terminal_id, None)
            try:
                await handler.send_request(
                    "terminal/release",
                    {"sessionId": session_id, "terminalId": terminal_id},
                    timeout=10,
                )
            except Exception:
                pass

    # CR2: 注册到 handler._active_tasks, 确保 cleanup() 和 _handle_cancel 可取消
    _key = f"terminal-stream-{terminal_id}"
    handler._active_tasks[_key] = asyncio.create_task(poll_loop())
    # H2+R5: 注册 terminal_cancel 到 handler, 供 _handle_cancel 和 timeout 联动
    if not hasattr(handler, '_terminal_events'):
        handler._terminal_events = {}
    handler._terminal_events[terminal_id] = terminal_cancel
    return None  # 流式模式: 调用者看到 None 应停止等待, poll_loop 后台运行

async def _list_files_local(path: str, depth: int = 3, limit: int = 200) -> str:
    """本地 list_files 回落 — Docker 容器内 / IDE 不可用时使用。

    使用 pathlib 遍历本地目录，排序后截断返回。作为 ACP 远程 list_files
    的降级方案，确保 IDE 或 Docker 不可用时 agent 仍能枚举文件。
    """
    # 使用 pathlib 规范化路径，防御路径穿越攻击
    p = pathlib.Path(path).resolve()
    if not p.exists():
        return f"❌ 路径不存在: {path}"
    if not p.is_dir():
        return f"❌ 不是目录: {path}"

    # 遍历目录条目，目录优先、按名称排序
    entries = list(p.iterdir())
    entries.sort(key=lambda x: (not x.is_dir(), x.name.lower()))

    # 格式化输出，限制条目数量防止输出过大
    lines: list[str] = []
    for e in entries:
        prefix = "📁 " if e.is_dir() else "📄 "
        lines.append(f"{prefix}{e.name}")
        if len(lines) >= limit:
            lines.append(f"... 及 {len(entries) - limit} 项更多")
            break

    return "\n".join(lines) if lines else "(空目录)"
