"""同轮 tool 调度策略 — 只读批并行、写/副作用串行。

单源 registry：SERIAL_ALWAYS / PARALLEL_READ / RATE_LIMITED。
未知工具默认 SERIAL；CAP 语义由 RATE_LIMITED + caller 内联 semaphore 实现。
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from typing import Any

from loguru import logger

SERIAL_ALWAYS: frozenset[str] = frozenset({
    "write_file", "edit_file", "delete_file", "safe_delete", "move_file",
    "refactor_rename", "reformat_code", "optimize_imports", "convert_java_to_kotlin",
    "apply_quickfix", "build_project", "sync_files", "ide_screenshot", "screenshot",
    "execute_command", "run_in_terminal", "execute_code", "execute_code_e2b",
    "git_stage", "git_commit",
    "upsert_focus_item", "complete_focus_item",
    "set_trigger", "update_trigger", "cancel_trigger",
    "add_tasks", "todo_write", "manage_tasks",
    "send_message_to_agent", "send_file_to_agent",
    "send_feishu_message", "send_channel_message", "send_platform_message", "send_channel_file",
    "import_mcp_server", "install_skill", "upload_image",
    "publish_page", "unpublish_page", "delete_published_page",
    "send_email", "reply_email",
    "convert_csv_to_xlsx", "convert_html_to_pdf", "convert_html_to_pptx",
    "convert_markdown_to_docx", "convert_markdown_to_pdf",
    "bitable_create_app", "bitable_create_record", "bitable_update_record", "bitable_delete_record",
    "feishu_doc_create", "feishu_doc_append",
    "feishu_calendar_create", "feishu_calendar_update", "feishu_calendar_delete",
    "feishu_drive_share", "feishu_drive_delete", "feishu_approval_create",
    "plaza_create_post", "plaza_add_comment",
    "update_kr_content", "update_kr_progress", "update_any_kr_progress",
    "create_objective", "create_key_result", "update_objective",
    "collect_okr_progress", "generate_okr_report", "generate_monthly_okr_report",
    "upsert_member_daily_report",
    "vercel_deploy", "vercel_set_env", "vercel_manage_domain", "neon_create_database",
    "retrieve_context", "discover_resources",
})

AGENTBAY_SERIAL_PREFIXES: tuple[str, ...] = ("agentbay_",)

WORKSPACE_WRITE_TOOLS: frozenset[str] = frozenset({
    "write_file", "edit_file", "delete_file", "safe_delete", "move_file",
    "refactor_rename", "reformat_code", "optimize_imports", "convert_java_to_kotlin",
    "apply_quickfix", "git_stage", "git_commit",
})

PARALLEL_READ: frozenset[str] = frozenset({
    "read_file", "list_files", "find_file", "search_text",
    "find_class", "find_symbol", "index_status",
    "find_references", "find_definition", "find_implementations", "find_super_methods",
    "call_hierarchy", "type_hierarchy", "diagnostics",
    "file_structure", "get_documentation", "active_file",
    "git_status", "git_diff",
    "grep", "search_file", "search_files", "find_files", "read_document",
    "list_focus_items",
    "list_triggers", "list_published_pages",
})

CONCURRENT_READ = PARALLEL_READ

RATE_LIMITED: frozenset[str] = frozenset({
    "web_search", "jina_search", "jina_read", "exa_search",
    "duckduckgo_search", "tavily_search", "google_search", "bing_search",
    "search_clawhub", "read_webpage",
    "feishu_doc_search", "feishu_user_search",
    "feishu_wiki_list", "feishu_doc_read", "feishu_calendar_list",
    "feishu_approval_query", "feishu_approval_get",
    "bitable_list_tables", "bitable_list_fields", "bitable_query_records",
    "read_emails", "plaza_get_new_posts",
    "get_okr", "get_my_okr", "get_okr_settings",
    "vercel_list_deployments", "vercel_get_deploy_logs",
    "open_file",
})

_search_sems: dict[str, asyncio.Semaphore] = {}


@lru_cache
def _rate_limit_permits() -> int:
    try:
        return min(3, max(2, int(os.getenv("PARALLEL_SEARCH_PERMITS", "2"))))
    except ValueError:
        return 2


def rate_limit_sem(tool_name: str) -> asyncio.Semaphore | None:
    """RATE_LIMITED 工具返回 per-tool semaphore。"""
    if tool_name not in RATE_LIMITED:
        return None
    sem = _search_sems.get(tool_name)
    if sem is None:
        sem = asyncio.Semaphore(_rate_limit_permits())
        _search_sems[tool_name] = sem
    return sem


class ToolExecutionMode(str, Enum):
    SERIAL = "serial"
    PARALLEL = "parallel"


@dataclass(frozen=True)
class ToolBatch:
    mode: ToolExecutionMode
    calls: tuple[dict, ...]


def _tool_name(tc: dict) -> str:
    return (tc.get("function") or {}).get("name") or ""


def _parse_args(tc: dict) -> dict[str, Any]:
    raw = (tc.get("function") or {}).get("arguments") or "{}"
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}


def classify_tool_call(tc: dict) -> ToolExecutionMode:
    name = _tool_name(tc)
    args = _parse_args(tc)
    if name in SERIAL_ALWAYS or name.startswith(AGENTBAY_SERIAL_PREFIXES):
        return ToolExecutionMode.SERIAL
    if name == "diagnostics" and args.get("includeBuildErrors"):
        return ToolExecutionMode.SERIAL
    if name in RATE_LIMITED or name in PARALLEL_READ or name.startswith("find_"):
        return ToolExecutionMode.PARALLEL
    if name:
        logger.info("[TOOL-POLICY] 未知工具 {} 降级 SERIAL", name)
    return ToolExecutionMode.SERIAL


def detect_path_conflict(a: dict, b: dict) -> bool:
    """保留供单测；partition 已不再按 path 拆批。"""
    for key in ("path", "file", "source_path", "target_path", "directory"):
        pa = _parse_args(a).get(key)
        pb = _parse_args(b).get(key)
        if isinstance(pa, str) and isinstance(pb, str) and pa.strip() and os.path.normpath(pa) == os.path.normpath(pb):
            return True
    return False


def partition_tool_calls(tool_calls: list[dict]) -> list[ToolBatch]:
    if not tool_calls:
        return []

    batches: list[ToolBatch] = []
    current_mode: ToolExecutionMode | None = None
    current_calls: list[dict] = []

    def flush() -> None:
        nonlocal current_mode, current_calls
        if current_calls:
            batches.append(ToolBatch(mode=current_mode or ToolExecutionMode.SERIAL, calls=tuple(current_calls)))
        current_mode = None
        current_calls = []

    for tc in tool_calls:
        mode = classify_tool_call(tc)
        if mode == ToolExecutionMode.SERIAL:
            flush()
            batches.append(ToolBatch(mode=ToolExecutionMode.SERIAL, calls=(tc,)))
            continue
        if current_mode != ToolExecutionMode.PARALLEL:
            flush()
            current_mode = ToolExecutionMode.PARALLEL
            current_calls = [tc]
            continue
        current_calls.append(tc)

    flush()
    return batches


def max_parallel_concurrency() -> int:
    try:
        return max(1, int(os.getenv("TOOL_PARALLEL_MAX_CONCURRENCY", "6")))
    except ValueError:
        return 6


_workspace_write_locks: dict[str, asyncio.Lock] = {}


def workspace_write_lock(session_id: str) -> asyncio.Lock:
    """同 session/workspace 写工具串行锁（S4）。"""
    key = session_id or "__default__"
    lock = _workspace_write_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _workspace_write_locks[key] = lock
    return lock
