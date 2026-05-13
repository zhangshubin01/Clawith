"""JSON-RPC 2.0 路由器 — LSP4J 协议处理核心。

处理通义灵码 IDE 通过 LSP4J 发送的 JSON-RPC 请求/响应/通知，
桥接到 Clawith 的 call_llm 智能体调用链。

协议字段严格匹配通义灵码插件源码定义（基于 /Users/shubinzhang/Downloads/demo-new 验证）：
- ChatAnswerParams: requestId, sessionId, text, overwrite, isFiltered, timestamp, extra
- ChatThinkingParams: requestId, sessionId, text, step("start"/"done"), timestamp, extra
- ChatFinishParams: requestId, sessionId, reason, statusCode, fullAnswer, extra
- ToolInvokeRequest: requestId, toolCallId, name, parameters, async
- ToolInvokeResponse: toolCallId, name, success, errorMessage, result, startTime, timeConsuming

⚠️ 绝对不能使用 ACP 的字段名（content, thinking, tool, arguments），
   否则插件无法解析消息，静默丢弃。

关键设计决策：
- 内存工具调用上下文优先于 DB：会话上下文始终以 _tool_call_history_by_session 内存记录为准，
  防止 DB 持久化窗口内的最新工具调用丢失
- search_replace 语义正确性：先通过 read_file 获取文件内容，执行 str.replace()，
  再将完整结果通过 replace_text_by_path 发送，避免全文替换的语义降级
- 差异化的工具超时：_TOOL_TIMEOUTS 中 run_in_terminal 使用三级超时（编译 600s / 只读 30s / 其他 180s）
"""

from __future__ import annotations

import asyncio
import fnmatch as _fnmatch
import json
import os
import re
import subprocess
import time
from pathlib import Path
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone as tz_utc
from typing import Any

from loguru import logger
from sqlalchemy import delete, select

from app.config import get_settings
from app.database import async_session
from app.models.agent import Agent as AgentModel
from app.models.llm import LLMModel
from app.models.chat_session import ChatSession
from app.models.audit import ChatMessage
from app.models.task import Task as TaskModel
from app.services.llm.caller import call_llm_with_failover
from app.services.agent_context import _is_ide_session
from app.services.task_executor import execute_task as _execute_task
import app.api.websocket as ws_module

from .context import (
    current_lsp4j_message_history,
    current_lsp4j_session_id,
    get_active_router,
    list_active_routers,
)
from .lsp_protocol import LSPBaseProtocolParser, ParseError
from .answer_sync import plan_answer_sync_before_finish
from .stream_buffer import StreamBufferManager
from .tool_constants import (
    LSP4J_IDE_TOOL_NAMES,
    TOOL_DISPLAY_NAME_MAP,
    TOOL_NAME_MAP,
)
from .workspace_file_service import WorkspaceFileService
from .search_input_utils import (
    android_module_tier,
    collect_android_values_xml_hits,
    extract_android_resource_name as _extract_android_resource_name,
    filename_keyword_for_search_file,
    infer_implicit_file_pattern_from_description,
    is_android_resource_path as _is_android_resource_path,
    is_android_resource_query as _is_android_resource_query,
    is_unusable_natural_language_file_query,
    sanitize_search_input as _sanitize_search_input,
)

# ──────────────────────────────────────────────
# LSP4J IDE 工具名称（直接导入自 tool_constants，与 tool_hooks 保持一致）
# ──────────────────────────────────────────────

# LSP4J 文件编辑工具（需要 filePath 转换 + fileId 注入）
# 这些工具在 invoke_tool_on_ide 中统一处理参数转换和 results 注入
_LSP4J_FILE_EDIT_TOOLS = frozenset(
    {
        "replace_text_by_path",
        "search_replace",
        "create_file_with_text",
        "delete_file_by_path",
        "apply_patch",
        "save_file",
    }
)

# 高频搜索类工具：在同一请求内会被连续调用很多次。
# 为避免插件 UI（EDT）被中间态事件风暴淹没，对其 PENDING/RUNNING 做降噪处理。
_UI_HEAVY_SEARCH_TOOLS = frozenset(
    {
        "search_codebase",
        "search_file",
        "list_dir",
        "grep_code",
        "search_symbol",
    }
)

# 高频工具步骤回调节流窗口（秒）。
# 同一 request 内重复的 (step,status,description) 在窗口内仅发送一次，
# 避免 process_step_callback 事件风暴压垮插件 EDT。
_PROCESS_STEP_THROTTLE_WINDOW_SEC = 0.35

# ──────────────────────────────────────────────
# 本地搜索工具配置（Android 项目优化）
# ──────────────────────────────────────────────

# 可搜索的源文件扩展名（覆盖 Java/Kotlin/Android 项目）
_SEARCHABLE_EXTENSIONS = frozenset(
    {
        # 源代码
        ".kt",
        ".java",
        ".kts",
        ".py",
        ".go",
        ".rs",
        ".swift",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".c",
        ".cpp",
        ".h",
        ".hpp",
        ".rb",
        ".php",
        ".scala",
        ".dart",
        # Android 资源与配置
        ".xml",
        ".json",
        ".yaml",
        ".yml",
        ".properties",
        ".pro",
        # Gradle
        ".gradle",
        # 其他
        ".sql",
        ".sh",
        ".bash",
        ".zsh",
        ".html",
        ".css",
        ".scss",
        ".md",
    }
)

# 排除的目录（构建产物 + IDE 配置）
_EXCLUDED_DIRS = frozenset(
    {
        "build",
        ".gradle",
        ".idea",
        ".git",
        ".comate",
        "__pycache__",
        "node_modules",
        ".claude",
        "logs",
        "generated",
        ".kotlin",
        "intermediates",
        "ksp",
        "kapt",
    }
)

# 最大扫描文件数（防止大项目超时）
_MAX_FILES_TO_SCAN = 500
# 最大结果数
_MAX_RESULTS = 30

# Android 场景下优先目录（召回排序 + 分层扫描）
_ANDROID_PRIORITY_SEGMENTS = (
    "/app/src/main/",
    "/src/main/java/",
    "/src/main/kotlin/",
    "/feature/",
)

# ──────────────────────────────────────────────
# 数据类定义（基于灵码插件 ChatAskParam.java 17 字段）
# ──────────────────────────────────────────────


@dataclass
class ChatAskParam:
    """灵码插件 chat/ask 请求参数。

    所有字段均设默认值，兼容旧版插件缺少字段的情况。
    字段名严格匹配 ChatAskParam.java 的 camelCase 命名。
    """

    requestId: str = ""
    chatTask: str = ""
    chatContext: Any = None
    sessionId: str = ""
    codeLanguage: str = ""
    isReply: bool = False
    source: int = 1
    questionText: str = ""
    stream: bool = True
    taskDefinitionType: str = ""
    extra: Any = None
    sessionType: str = ""
    targetAgent: str = ""
    pluginPayloadConfig: Any = None
    mode: str = ""
    shellType: str = ""
    customModel: Any = None


# ──────────────────────────────────────────────
# 模块级变量
# ──────────────────────────────────────────────

# 后台持久化任务集合（防止 GC 回收未完成的 fire-and-forget 任务）
_lsp4j_background_tasks: set[asyncio.Task] = set()
# 严格模式：tool/invokeResult 缺失 toolCallId 时直接失败，禁止多路并发下的降级误匹配。
_LSP4J_STRICT_TOOLCALL_ID = os.getenv("LSP4J_STRICT_TOOLCALL_ID", "1").strip() != "0"

# LSP4J 工具调用结果缓存（模块级，所有连接共享）
_lsp4j_tool_cache: dict[str, tuple[float, str]] = {}
_LSP4J_TOOL_CACHE_TTL = 300.0  # 与 IDE 插件端缓存 TTL 对齐
_CACHEABLE_TOOLS = frozenset({"read_file", "search_file", "search_codebase", "search_symbol", "list_dir", "grep_code"})

# 终端只读命令前缀：匹配的命令插件端可走 ProcessBuilder 快径，免去 invokeAndWait + Ctrl+C 开销
_TERMINAL_READONLY_PREFIXES = (
    "git show", "git diff", "git log", "git status",
    "ls ", "cat ", "head ", "tail ", "grep ", "find ",
    "wc ", "pwd", "which ", "whoami", "echo ", "date",
    "uname ", "hostname",
)

# 搜索负缓存: 记录返回 0 结果的查询，避免 LLM 重复无效搜索浪费轮次
_search_zero_result_cache: dict[str, tuple[float, str]] = {}
_SEARCH_ZERO_CACHE_TTL = 600.0

# 会话消息内存缓存：同 session 内多次 chat/ask 无需重复查 DB
_SESSION_MESSAGE_CACHE: dict[str, tuple[float, list[dict]]] = {}
_SESSION_MESSAGE_CACHE_TTL = 1800.0  # 30 分钟，覆盖大部分会话生命周期

# 基础工具名映射直接使用导入的常量（tool_constants.TOOL_NAME_MAP / TOOL_DISPLAY_NAME_MAP）

# ──────────────────────────────────────────────
# 本地工具执行（不在 IDE 端，由后端直接执行）
# ──────────────────────────────────────────────

# Agent 内部目录前缀 — 解析到后端工作空间，不解析到 IDE 项目路径
# ⚠️ "workspace/" 不在此列表：LLM 常用 workspace/xxx 路径操作项目文件，
# Agent 内部文件（soul.md 等）由 _AGENT_WS_BASE_NAMES 单独保护。
_AGENT_WS_PREFIXES = ("memory/", "skills/", "enterprise_info/")
_AGENT_WS_BASE_NAMES = frozenset({"soul.md", "focus.md", "tasks.json", "memory.md"})


def _resolve_search_path(rel_path: str, project_path: str | None = None) -> Path:
    """解析搜索路径：Agent 内部路径 → 后端 CWD，其余 → IDE project_path。

    ★ LLM 常用 workspace/xxx 作为虚拟路径。当 project_path 已知时，
    自动剥离 workspace/ 前缀，使搜索落在用户项目目录中。
    """
    from pathlib import Path

    # 绝对路径直接使用
    p = Path(rel_path)
    if p.is_absolute():
        return p

    # Agent 内部文件 → 后端工作空间（由文件名保护，不依赖路径前缀）
    basename = rel_path.split("/")[-1]
    if basename in _AGENT_WS_BASE_NAMES:
        return Path.cwd() / rel_path
    for prefix in _AGENT_WS_PREFIXES:
        if rel_path.startswith(prefix):
            return Path.cwd() / rel_path

    # ★ 剥离 LLM 常用的 workspace/ 前缀
    _clean_path = rel_path
    if _clean_path.startswith("workspace/"):
        _clean_path = _clean_path[len("workspace/") :]

    # 其余相对路径 → IDE 项目路径
    if project_path:
        return Path(project_path) / _clean_path
    return Path.cwd() / _clean_path


def _dynamic_scan_budget(project_root: Path, query: str, tool_name: str) -> int:
    """按项目规模和 query 类型动态分配扫描预算，避免固定 500 导致 Android 多模块截断。"""
    budget = _MAX_FILES_TO_SCAN
    root_text = str(project_root).lower()
    is_android_like = any(seg in root_text for seg in ("/app", "/android", "/feature"))
    if is_android_like:
        budget = max(budget, 1600)
    if tool_name in {"search_symbol", "search_file"} and len(query) >= 8:
        budget = max(budget, 2400)
    if tool_name in {"grep_code", "search_codebase"}:
        budget = max(budget, 1800)
    return min(budget, 5000)


def _is_android_priority_path(path_text: str) -> bool:
    normalized = path_text.replace("\\", "/")
    return any(seg in normalized for seg in _ANDROID_PRIORITY_SEGMENTS)


def _rg_search(project_root: str, pattern: str, max_results: int = 50) -> list[dict] | None:
    """使用 ripgrep 搜索代码，比 Python rglob + read_text 快 10-100x。

    返回 [{"fileName": ..., "path": ..., "startLine": ..., "endLine": ..., "matchLine": ...}]
    或 None 表示 rg 不可用。
    """
    import shutil
    _rg = shutil.which("rg") or shutil.which("rg", path="/opt/homebrew/bin:/usr/local/bin:/usr/bin")
    if not _rg:
        return None
    try:
        result = subprocess.run(
            [_rg, "--no-heading", "--with-filename", "--line-number",
             "--ignore-case", "--no-ignore-vcs", "--max-count=3",
             "--glob=!.git", "--glob=!.gradle", "--glob=!.idea",
             "--glob=!build", "--glob=!node_modules",
             "-e", pattern, project_root],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode not in (0, 1):
            return None  # rg error (returncode 1 = no matches, which is valid)
        items = []
        for line in result.stdout.strip().split("\n"):
            if not line or len(items) >= max_results:
                break
            # rg output: path:lineno:content
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            file_path, lineno_str, match_text = parts
            try:
                lineno = int(lineno_str)
            except ValueError:
                continue
            items.append({
                "fileName": os.path.basename(file_path),
                "path": os.path.join(project_root, file_path),
                "startLine": lineno,
                "endLine": lineno,
                "matchLine": match_text.strip()[:200],
            })
        logger.info("[RG-SEARCH] grep_code pattern={} results={}", pattern[:80], len(items))
        return items
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.info("[RG-SEARCH] unavailable: {}", e)
        return None


def _get_code_map_for_session(project_path: str) -> str:
    """获取代码结构地图（带缓存），用于注入新会话 context。"""
    try:
        from .file_index import get_or_build_code_map
        return get_or_build_code_map(project_path)
    except Exception:
        logger.exception("[CODE-MAP] get failed")
        return ""


async def _build_project_file_index(project_path: str) -> None:
    """后台构建项目文件索引（fire-and-forget），供搜索操作使用。"""
    try:
        from .file_index import get_or_build_index
        get_or_build_index(project_path, force_rebuild=True)
    except Exception:
        logger.exception("[FILE-INDEX] build failed")


def _execute_local_tool(tool_name: str, arguments: dict, project_path: str = "") -> tuple[str, list[dict]]:
    """本地执行搜索工具，返回 (result_json_str, results_list).

    results_list 格式匹配插件期望：
    - list_dir → DirItem[] (fileName, fileCount, fileSize, type, path)
    - search_file → FileItem[] (fileName, path)
    - grep_code → [{fileName, path, startLine, endLine}]
    - search_codebase → [{fileName, path, startLine, endLine}]
    - search_symbol → [{fileName, path}]

    Args:
        project_path: IDE 项目根路径。传入时非 Agent 内部的相对路径以此为基准，
                      否则回退到 Path.cwd()（兼容 Web UI 路径）。
    """
    from pathlib import Path
    import re as _re

    cwd = Path(project_path) if project_path else Path.cwd()
    search_query = _sanitize_search_input(arguments.get("query", ""))

    # ── 搜索负缓存: 相同查询之前返回 0 结果，直接返回不浪费扫描 ──
    if tool_name in ("search_file", "search_codebase", "grep_code", "search_symbol"):
        _neg_key_parts = [tool_name, search_query, arguments.get("file_pattern", ""),
                          arguments.get("regex", ""), arguments.get("pattern", "*")]
        _neg_key = "|".join(p for p in _neg_key_parts if p)
        if _neg_key in _search_zero_result_cache:
            _ts, _reason = _search_zero_result_cache[_neg_key]
            if time.monotonic() - _ts < _SEARCH_ZERO_CACHE_TTL:
                logger.info("[LSP4J-CACHE] negative hit: {} reason={}", _neg_key[:120], _reason)
                if tool_name == "grep_code":
                    return json.dumps([]), []
                return json.dumps([]), []
        # 清理过期负缓存
        _now = time.monotonic()
        _expired_neg = [k for k, (ts, _) in _search_zero_result_cache.items()
                        if _now - ts > _SEARCH_ZERO_CACHE_TTL]
        for k in _expired_neg:
            del _search_zero_result_cache[k]

    scan_budget = _dynamic_scan_budget(cwd, search_query, tool_name)

    def _is_searchable(file_path: Path) -> bool:
        parts = set(file_path.parts)
        if parts & _EXCLUDED_DIRS:
            return False
        if file_path.suffix not in _SEARCHABLE_EXTENSIONS:
            return False
        return True

    def _iter_project_files():
        """按 Android 优先目录分两轮流式扫描，避免一次性加载全量文件列表。"""
        count = 0
        seen: set[str] = set()
        for prefer_priority in (True, False):
            for p in cwd.rglob("*"):
                if count >= scan_budget:
                    return
                if not p.is_file() or not _is_searchable(p):
                    continue
                is_priority = _is_android_priority_path(str(p))
                if prefer_priority != is_priority:
                    continue
                p_text = str(p)
                if p_text in seen:
                    continue
                seen.add(p_text)
                count += 1
                yield p

    if tool_name == "list_dir":
        rel_path = arguments.get("relative_workspace_path") or arguments.get("path") or "."
        ws_path = _resolve_search_path(rel_path, project_path or None)
        items: list[dict] = []

        # ★ 快路径: 使用文件索引
        from .file_index import get_or_build_index
        _file_idx = get_or_build_index(str(cwd))
        if _file_idx is not None and _file_idx.file_count > 0:
            items = _file_idx.list_dir(rel_path)
            if items:
                logger.info("[FILE-INDEX] list_dir hit: path={} entries={}", rel_path, len(items))
                result_str = json.dumps(items, ensure_ascii=False, default=str)
                return result_str, items
        try:
            if ws_path.exists() and ws_path.is_dir():
                for entry in sorted(ws_path.iterdir()):
                    try:
                        is_dir = entry.is_dir()
                        stat = entry.stat()
                        file_size = "" if is_dir else str(stat.st_size)
                    except OSError:
                        is_dir = False
                        file_size = ""
                    # ★ #5 修复：过滤排除目录（构建产物、IDE 配置等）
                    if not is_dir and not _is_searchable(entry):
                        continue
                    items.append(
                        {
                            "fileName": entry.name,
                            "fileCount": "",
                            "fileSize": file_size,
                            "type": "directory" if is_dir else "file",
                            "path": str(entry.absolute()),
                        }
                    )
        except PermissionError:
            logger.warning("[LSP4J-TOOL] list_dir 权限不足: path={}", ws_path)
        result_str = json.dumps(items, ensure_ascii=False, default=str)
        return result_str, items

    elif tool_name == "search_file":
        # ★ #1 + #5 修复：file_pattern 做 glob 初筛 + query 做文件名二次过滤 + _is_searchable 过滤排除目录
        pattern = _sanitize_search_input(arguments.get("file_pattern", "") or "*")
        query = _sanitize_search_input(arguments.get("query", ""))
        pattern = infer_implicit_file_pattern_from_description(query, pattern)
        filename_kw = filename_keyword_for_search_file(query, pattern)
        search_path = _sanitize_search_input(arguments.get("path", "."))
        is_resource_query = _is_android_resource_query(query)
        resource_name = _extract_android_resource_name(query)
        search_dir = _resolve_search_path(search_path, project_path or None)
        items: list[dict] = []

        # ★ 快路径: 使用文件索引（O(1) 查询），回退到 rglob 扫描
        from .file_index import get_or_build_index
        _file_idx = get_or_build_index(str(cwd))
        if _file_idx is not None and _file_idx.file_count > 0:
            items = _file_idx.search_file(query, pattern)
            if items:
                logger.info(
                    "[FILE-INDEX] search_file hit: query={} pattern={} results={}",
                    query, pattern, len(items),
                )
                result_str = json.dumps(items, ensure_ascii=False, default=str)
                return result_str, items
            else:
                # 索引未命中，记录负缓存
                _neg_key_parts = ["search_file", search_query,
                                  arguments.get("file_pattern", ""), pattern]
                _neg_key = "|".join(p for p in _neg_key_parts if p)
                _search_zero_result_cache[_neg_key] = (time.monotonic(), "index_miss")
                logger.info("[FILE-INDEX] search_file miss: query={} pattern={}", query, pattern)
                # 提示换用内容搜索（项目使用混淆文件名，按名称搜不到）
                _hint_query = query or pattern.replace("*", "").replace(".", "")
                result_str = json.dumps(
                    [{"hint": "按文件名搜索无结果（项目使用混淆名称）。"
                              f"请改用 grep_code(regex) 搜索文件内容: "
                              f"grep_code(regex=\"{_hint_query[:60]}\")"}],
                    ensure_ascii=False, default=str)
                return result_str, []

        scanned_count = 0
        path_filtered = 0
        pattern_filtered = 0
        query_filtered = 0
        skip_scan = is_unusable_natural_language_file_query(query, pattern, is_resource_query)
        try:
            if skip_scan:
                pass
            elif search_dir.exists() and search_dir.is_dir():
                for p in search_dir.rglob("*"):
                    if scanned_count >= scan_budget:
                        break
                    if not p.is_file():
                        continue
                    scanned_count += 1
                    # 过滤排除目录和不可搜索扩展名
                    if not _is_searchable(p):
                        path_filtered += 1
                        continue
                    # glob 初筛（仅当 pattern 不是 "*" 时）
                    if pattern != "*":
                        try:
                            if not p.match(pattern) and not _fnmatch.fnmatch(p.name, pattern):
                                pattern_filtered += 1
                                continue
                        except (ValueError, TypeError):
                            pattern_filtered += 1
                            continue
                    # 二次过滤：文件名包含关键词（混中文描述时已提炼为拉丁词干）；
                    # Android 资源语义 query 允许通过资源名命中 xml。
                    if filename_kw:
                        file_name_lower = p.name.lower()
                        query_lower = filename_kw.lower()
                        base_name_lower = p.stem.lower()
                        resource_hit = (
                            is_resource_query
                            and resource_name
                            and _is_android_resource_path(str(p))
                            and base_name_lower == resource_name.lower()
                        )
                        if query_lower not in file_name_lower and not resource_hit:
                            query_filtered += 1
                            continue
                    items.append(
                        {
                            "fileName": p.name,
                            "path": str(p.absolute()),
                        }
                    )
                    if len(items) >= 50:
                        break
        except PermissionError:
            logger.warning("[LSP4J-TOOL] search_file 权限不足: path={}", search_dir)
        if is_resource_query and resource_name:
            seen_paths = {str(x.get("path")) for x in items}
            for hit in collect_android_values_xml_hits(cwd, resource_name):
                if len(items) >= 50:
                    break
                pth = hit.get("path", "")
                if pth and pth not in seen_paths:
                    seen_paths.add(pth)
                    items.append({"fileName": hit["fileName"], "path": pth})
        zero_result_reason = "none"
        if not items:
            if skip_scan:
                zero_result_reason = "natural_language_filename_query"
            elif scanned_count >= scan_budget:
                zero_result_reason = "scope_truncated"
            elif pattern_filtered > 0 and query_filtered == 0:
                zero_result_reason = "pattern_filtered_all"
            elif query_filtered > 0:
                zero_result_reason = "no_text_match"
            elif path_filtered > 0:
                zero_result_reason = "path_filtered_all"
            else:
                zero_result_reason = "index_empty"
        logger.info(
            "[LSP4J-TOOL] local_search_file strategy=strict budget={} scanned={} results={} zero_result_reason={} "
            "path_filtered={} pattern_filtered={} query_filtered={} query={} pattern={} path={}",
            scan_budget,
            scanned_count,
            len(items),
            zero_result_reason,
            path_filtered,
            pattern_filtered,
            query_filtered,
            query,
            pattern,
            search_path,
        )
        if items:
            # Android 资源联动排序：模块层级（app/feature/src-main）→ 资源匹配 → 文件名命中。
            def _rank(item: dict) -> tuple[int, int, str]:
                file_name = str(item.get("fileName", ""))
                path = str(item.get("path", ""))
                tier = android_module_tier(path)
                base_name = file_name.rsplit(".", 1)[0].lower() if "." in file_name else file_name.lower()
                if (
                    is_resource_query
                    and resource_name
                    and _is_android_resource_path(path)
                    and base_name == resource_name.lower()
                ):
                    group = 0
                elif (filename_kw and filename_kw.lower() in file_name.lower()) or (
                    query and query.lower() in file_name.lower()
                ):
                    group = 1
                elif is_resource_query and _is_android_resource_path(path):
                    group = 2
                else:
                    group = 3
                return (tier, group, path)

            items.sort(key=_rank)
        # 负缓存写入: 0 结果搜索
        if not items and _neg_key:
            _search_zero_result_cache[_neg_key] = (time.monotonic(), zero_result_reason)
        result_str = json.dumps(items, ensure_ascii=False, default=str)
        return result_str, items

    elif tool_name == "grep_code":
        regex = arguments.get("regex", "")
        items = _rg_search(str(cwd), regex, max_results=_MAX_RESULTS)
        if items is not None:
            result_str = json.dumps(items, ensure_ascii=False, default=str)
            return result_str, items
        # rg 不可用时的 Python 回退
        try:
            pattern = _re.compile(regex, _re.IGNORECASE)
        except _re.error:
            return json.dumps({"error": f"无效的正则表达式: {regex}"}), []
        items = []
        from .file_index import get_or_build_index
        _file_idx = get_or_build_index(str(cwd))
        if _file_idx is not None and _file_idx.all_files:
            _iter_source = (cwd / rel for rel in _file_idx.all_files if (cwd / rel).is_file())
        else:
            _iter_source = _iter_project_files()
        for p in _iter_source:
            if len(items) >= _MAX_RESULTS:
                break
            try:
                text = p.read_text(errors="ignore")
                matches = list(pattern.finditer(text))
                for m in matches[:5]:
                    if len(items) >= _MAX_RESULTS:
                        break
                    start_line = text[: m.start()].count("\n") + 1
                    end_line = text[: m.end()].count("\n") + 1
                    items.append(
                        {
                            "fileName": p.name,
                            "path": str(p.absolute()),
                            "startLine": start_line,
                            "endLine": end_line,
                        }
                    )
            except (OSError, UnicodeDecodeError):
                pass
        # 负缓存: 0 结果搜索
        if not items and _neg_key:
            _search_zero_result_cache[_neg_key] = (time.monotonic(), "no_match")
        result_str = json.dumps(items, ensure_ascii=False, default=str)
        return result_str, items

    elif tool_name == "search_codebase":
        query = arguments.get("query", "")
        # ★ 快路径: 用 ripgrep 搜索（快 10-100x）
        items = _rg_search(str(cwd), _re.escape(query), max_results=_MAX_RESULTS)
        if items is not None:
            result_str = json.dumps(items, ensure_ascii=False, default=str)
            return result_str, items
        # Python 回退
        query_lower = query.lower()
        items = []
        from .file_index import get_or_build_index
        _file_idx = get_or_build_index(str(cwd))
        if _file_idx is not None and _file_idx.all_files:
            _iter_source = (cwd / rel for rel in _file_idx.all_files if (cwd / rel).is_file())
        else:
            _iter_source = _iter_project_files()
        for p in _iter_source:
            if len(items) >= _MAX_RESULTS:
                break
            try:
                text = p.read_text(errors="ignore").lower()
                idx = text.find(query_lower)
                if idx >= 0:
                    start_line = text[:idx].count("\n") + 1
                    items.append(
                        {
                            "fileName": p.name,
                            "path": str(p.absolute()),
                            "startLine": start_line,
                            "endLine": start_line,
                        }
                    )
            except (OSError, UnicodeDecodeError):
                pass
        # 负缓存: 0 结果搜索
        if not items and _neg_key:
            _search_zero_result_cache[_neg_key] = (time.monotonic(), "no_match")
        result_str = json.dumps(items, ensure_ascii=False, default=str)
        return result_str, items

    elif tool_name == "search_symbol":
        query = _sanitize_search_input(arguments.get("query", ""))
        query_lower = query.lower()
        items = []
        is_resource_query = _is_android_resource_query(query)
        resource_name = _extract_android_resource_name(query)
        declaration_pattern = (
            _re.compile(rf"(?i)\b(object|class|interface|typealias)\s+{_re.escape(query)}\b") if query else None
        )
        resource_ref_pattern = (
            _re.compile(
                rf"(?i)\bR\.(layout|string|id|drawable|color|menu|anim|mipmap)\s*\.\s*{_re.escape(resource_name)}\b"
            )
            if resource_name
            else None
        )
        declaration_hits = 0
        resource_ref_hits = 0
        # ★ 快路径: 使用文件索引
        from .file_index import get_or_build_index
        _file_idx = get_or_build_index(str(cwd))
        if _file_idx is not None and _file_idx.all_files:
            _iter_source = (cwd / rel for rel in _file_idx.all_files if (cwd / rel).is_file())
        else:
            _iter_source = _iter_project_files()
        for p in _iter_source:
            if len(items) >= _MAX_RESULTS:
                break
            if query_lower in p.name.lower():
                items.append(
                    {
                        "fileName": p.name,
                        "path": str(p.absolute()),
                    }
                )
                continue
            if declaration_pattern is None:
                if not (is_resource_query and resource_ref_pattern):
                    continue
            try:
                text = p.read_text(errors="ignore")
            except (OSError, UnicodeDecodeError):
                continue
            if declaration_pattern and declaration_pattern.search(text):
                declaration_hits += 1
                items.append(
                    {
                        "fileName": p.name,
                        "path": str(p.absolute()),
                    }
                )
                continue
            if (
                is_resource_query
                and resource_name
                and _is_android_resource_path(str(p))
                and p.stem.lower() == resource_name.lower()
            ):
                items.append(
                    {
                        "fileName": p.name,
                        "path": str(p.absolute()),
                    }
                )
                continue
            if resource_ref_pattern and resource_ref_pattern.search(text):
                resource_ref_hits += 1
                items.append(
                    {
                        "fileName": p.name,
                        "path": str(p.absolute()),
                    }
                )
        seen_paths_sym = {str(x.get("path")) for x in items}
        if is_resource_query and resource_name and len(items) < _MAX_RESULTS:
            for hit in collect_android_values_xml_hits(cwd, resource_name):
                if len(items) >= _MAX_RESULTS:
                    break
                pth = hit.get("path", "")
                if pth and pth not in seen_paths_sym:
                    seen_paths_sym.add(pth)
                    items.append({"fileName": hit["fileName"], "path": pth})
        if not items and resource_name:
            probe = resource_name.lower()
            # ★ 快路径: 使用文件索引
            _idx_source = (cwd / rel for rel in _file_idx.all_files if (cwd / rel).is_file()) if _file_idx and _file_idx.all_files else _iter_project_files()
            for p in _idx_source:
                if len(items) >= _MAX_RESULTS:
                    break
                npath = str(p).replace("\\", "/").lower()
                if any(
                    seg in npath
                    for seg in ("/build/", "/.gradle/", "/generated/", "/intermediates/", "/ksp/", "/kapt/")
                ):
                    continue
                if p.suffix.lower() not in {".kt", ".java", ".xml"}:
                    continue
                try:
                    txt = p.read_text(errors="ignore").lower()
                except OSError:
                    continue
                if probe in txt:
                    items.append(
                        {
                            "fileName": p.name,
                            "path": str(p.absolute()),
                        }
                    )

        # Android 噪声符号下沉排序（不丢弃，保证召回完整）
        def _android_noise(name: str) -> bool:
            base = name.rsplit(".", 1)[0]
            return (
                base == "R"
                or base.startswith("R$")
                or base == "BuildConfig"
                or base.endswith("Binding")
                or base.endswith("Directions")
            )

        items.sort(
            key=lambda item: (
                android_module_tier(str(item.get("path", ""))),
                0
                if is_resource_query
                and resource_name
                and _is_android_resource_path(str(item.get("path", "")))
                and str(item.get("fileName", "")).rsplit(".", 1)[0].lower() == resource_name.lower()
                else 1,
                1 if _android_noise(str(item.get("fileName", ""))) else 0,
                str(item.get("path", "")),
            )
        )
        zero_result_reason = "none"
        if not items:
            zero_result_reason = "no_symbol_match"
        elif declaration_hits > 0:
            zero_result_reason = "symbol_exact_miss_fallback_hit"
        logger.info(
            "[LSP4J-TOOL] local_search_symbol budget={} results={} declaration_hits={} resource_ref_hits={} "
            "zero_result_reason={} query={}",
            scan_budget,
            len(items),
            declaration_hits,
            resource_ref_hits,
            zero_result_reason,
            query,
        )
        # 负缓存: 0 结果搜索
        if not items and _neg_key:
            _search_zero_result_cache[_neg_key] = (time.monotonic(), zero_result_reason)
        result_str = json.dumps(items, ensure_ascii=False, default=str)
        return result_str, items

    else:
        raise ValueError(f"未知的本地工具: {tool_name}")


# ──────────────────────────────────────────────
# IDE 上下文提示构建（基于 ChatTaskEnum.java 21 个枚举值）
# ──────────────────────────────────────────────

_CHAT_TASK_HINTS = {
    "EXPLAIN_CODE": "用户正在请求解释代码，请详细说明代码逻辑和意图。",
    "CODE_GENERATE_COMMENT": "用户请求为代码生成注释，请生成清晰的中文注释。",
    "OPTIMIZE_CODE": "用户请求优化代码，请分析性能问题并给出改进建议。",
    "GENERATE_TESTCASE": "用户请求生成测试用例，请生成符合项目风格的单元测试。",
    "TERMINAL_COMMAND_GENERATION": "用户请求生成终端命令，请给出准确、安全的命令。",
    "DESCRIPTION_GENERATE_CODE": "用户请求根据描述生成代码，请生成符合规范的完整代码。",
    "CODE_PROBLEM_SOLVE": "用户遇到代码问题，请分析问题原因并给出解决方案。",
    "DOCUMENT_TRANSLATE": "用户请求翻译文档，请保持格式并准确翻译。",
    "SEARCH_TITLE_ASK": "用户进行搜索标题提问，请给出简洁准确的回答。",
    "ERROR_INFO_ASK": "用户询问错误信息，请分析错误原因并提供解决方案。",
    "FREE_INPUT": "用户自由提问，请综合上下文给出有帮助的回答。",
    "INLINE_CHAT": "用户进行行内问答，请简短直接地回答。",
    "INLINE_EDIT": "用户进行行内编辑，请生成精确的代码修改。",
}


def _sanitize_lang(lang: str) -> str:
    """清洗编程语言标识符，防止 Markdown 代码围栏注入。

    只允许字母、数字、+、-、.，并限制长度。
    """
    return re.sub(r"[^a-zA-Z0-9+\-.]", "", lang)[:20]


def _build_lsp4j_ide_prompt(params: ChatAskParam) -> str:
    """基于 ChatAskParam 字段构建 IDE 环境提示，注入 role_description。

    将 chatTask、codeLanguage、mode、chatContext、extra.context、shellType 等
    未被 Clawith 核心逻辑消费的字段，以结构化提示的方式传递给 LLM。
    包含 2000 字符 token 预算控制（P2-1）。

    ⚠️ 字段名与灵码插件源码一致：
    - BaseChatTaskDto.activeFilePath（不是 activeFile）
    - ChatContext.sourceCode / filePath / fileLanguage（不是 selectedCode）
    - ExtraContext.selectedItem.extra.contextType（"file"/"selectedCode"/"openFiles"）
    """
    parts: list[str] = []

    # chatTask 任务类型提示
    hint = _CHAT_TASK_HINTS.get(params.chatTask, "")
    if hint:
        parts.append(hint)

    # codeLanguage 编程语言
    if params.codeLanguage:
        parts.append(f"当前编程语言: {params.codeLanguage}")

    # mode 模式（inline_chat / edit 等）
    if params.mode:
        parts.append(f"编辑模式: {params.mode}")

    # ── chatContext 结构化解析（字段名与插件源码一致） ──
    try:
        chat_context = params.chatContext
        if isinstance(chat_context, dict):
            active_file = chat_context.get("activeFilePath", "")
            if active_file:
                parts.append(f"当前活动文件: {active_file}")
            selected_code = chat_context.get("sourceCode", "")
            if selected_code:
                lang = _sanitize_lang(chat_context.get("fileLanguage", "") or params.codeLanguage or "")
                parts.append(f"[用户选中的代码]\n```{lang}\n{selected_code[:4000]}\n```")
            file_path = chat_context.get("filePath", "")
            if file_path:
                parts.append(f"相关文件路径: {file_path}")
            # imageUrls 处理（图片 URL 列表）
            image_urls = chat_context.get("imageUrls", [])
            if image_urls and isinstance(image_urls, list):
                parts.append(f"用户附带了 {len(image_urls)} 张图片")
    except Exception as e:
        logger.warning("[LSP4J] chatContext 解析异常，跳过: {}", e)

    # ── extra.context 结构化解析 ──
    try:
        if isinstance(params.extra, dict):
            # 旧格式：extra.context[].type/content
            for ctx in params.extra.get("context", []):
                ctx_type = ctx.get("type", "")
                ctx_content = ctx.get("content", "")
                if ctx_type == "code" and ctx_content:
                    lang = _sanitize_lang(ctx.get("language") or params.codeLanguage or "")
                    parts.append(f"[用户选中的代码（仅供参考）]\n```{lang}\n{ctx_content[:4000]}\n```")
                elif ctx_type == "file" and ctx_content:
                    parts.append(f"相关文件: {ctx_content}")

            # 新格式：extra.context[].selectedItem.extra.contextType（灵码插件自动填充）
            for ctx in params.extra.get("context", []):
                if isinstance(ctx, dict):
                    selected_item = ctx.get("selectedItem", {})
                    if isinstance(selected_item, dict):
                        ctx_extra = selected_item.get("extra", {})
                        if isinstance(ctx_extra, dict):
                            context_type = ctx_extra.get("contextType", "")
                            if context_type == "selectedCode":
                                code = ctx_extra.get("content", "") or selected_item.get("content", "")
                                if code:
                                    lang = _sanitize_lang(ctx_extra.get("language", "") or params.codeLanguage or "")
                                    parts.append(f"[用户选中的代码]\n```{lang}\n{code[:4000]}\n```")
                            elif context_type == "file":
                                fpath = ctx_extra.get("filePath", "") or selected_item.get("path", "")
                                if fpath:
                                    parts.append(f"用户添加的上下文文件: {fpath}")
                            elif context_type == "openFiles":
                                open_files = ctx_extra.get("filePaths", [])
                                if open_files and isinstance(open_files, list):
                                    parts.append(f"用户打开的文件: {', '.join(str(f) for f in open_files[:20])}")
                            elif context_type == "recent_tool_result":
                                summary = ctx_extra.get("content", "")
                                if summary:
                                    parts.append(f"[最近工具执行结果（来自缓存，避免重复搜索）]\n{summary[:2000]}")

            if params.extra.get("fullFileEdit"):
                parts.append("整文件编辑模式：请输出完整的文件内容。")
    except Exception as e:
        logger.warning("[LSP4J] extra.context 解析异常，跳过: {}", e)

    # shellType（P2-2）
    if params.shellType:
        parts.append(f"项目终端 Shell: {params.shellType}")

    # ★ CODE_EDIT_BLOCK 格式引导（关键：让 LLM 输出可 Apply 的代码块）
    # 灵码插件 MarkdownStreamPanel.java:39-42 正则表达式：
    #   ```([\\w#+.-]*\n*)?(.*?)`{2,3}
    #   group(9) = 语言标识（如 python）
    #   group(10) = 围栏内容（包含语言标识 + |CODE_EDIT_BLOCK| + 路径 + 代码）
    # 插件 line 302 解析：group(10).split("|") → [language, "CODE_EDIT_BLOCK", path+code]
    # 正确格式示例：
    #   ```python|CODE_EDIT_BLOCK|/path/to/file.java
    #   <code content>
    #   ```
    # 插件识别后会：
    # 1. 渲染代码块时显示 "Apply" 按钮（CodeMarkdownHighlightComponent.java:358-461）
    # 2. 用户点击 Apply 后调用 chat/codeChange/apply
    # 3. 插件渲染 InEditorDiffRenderer 显示 diff（CodeMarkdownHighlightComponent.java:527-530）
    if params.chatTask in (
        "CODE_GENERATE_COMMENT",
        "OPTIMIZE_CODE",
        "INLINE_EDIT",
        "DESCRIPTION_GENERATE_CODE",
        "CODE_PROBLEM_SOLVE",
        "FREE_INPUT",
        "PRE_CONTEXT",
        "CODE_REVIEW",
        "UNIT_TEST",
    ):
        parts.append(
            "[代码输出格式要求] 如需生成代码，请使用以下格式让代码可交互编辑：\n"
            "```python|CODE_EDIT_BLOCK|/absolute/path/to/file.py\n"
            "<完整代码内容>\n"
            "```\n"
            "注意：语言标识（如 python）和 |CODE_EDIT_BLOCK| 必须在同一行，路径后必须换行再写代码。\n"
            "这样用户可以直接点击 'Apply' 按钮应用代码变更，并查看 diff。"
        )

    # pluginPayloadConfig（P2-2，仅记录日志）
    if params.pluginPayloadConfig:
        logger.debug("LSP4J: pluginPayloadConfig 存在但暂不处理: {}", type(params.pluginPayloadConfig).__name__)

    if not parts:
        return ""

    ide_prompt = "\n\n[IDE 环境提示]\n" + "\n".join(f"- {p}" for p in parts)

    # Token 预算控制（2000 字符上限，P2-1）
    if len(ide_prompt) > 2000:
        logger.warning("LSP4J: ide_prompt 超长 ({} 字符)，截断到 2000", len(ide_prompt))
        ide_prompt = ide_prompt[:1997] + "..."

    return ide_prompt


async def _resolve_model_by_key(model_key: str) -> LLMModel | None:
    """根据模型 key（UUID 或 model 名称）查找 LLMModel。

    用于 LSP4J extra.modelConfig.key 模型切换场景。
    优先按 UUID 查 id 字段，再按 model 名称查。

    Args:
        model_key: 模型标识，可以是 UUID 字符串或 model 名称（如 "gpt-4o"）

    Returns:
        匹配的 LLMModel 实例，未找到则返回 None
    """
    # key=auto 表示使用默认模型，直接返回 None
    if model_key == "auto":
        return None

    async with async_session() as db:
        # 尝试按 UUID 查找 id 字段
        try:
            mid = uuid.UUID(model_key)
            mr = await db.execute(select(LLMModel).where(LLMModel.id == mid))
            model = mr.scalar_one_or_none()
            if model:
                return model
        except ValueError:
            pass

        # 按 model 名称查找
        mr = await db.execute(select(LLMModel).where(LLMModel.model == model_key))
        model = mr.scalar_one_or_none()
        if model:
            logger.debug("[LSP4J] 模型查找: key={} → model={} provider={}", model_key, model.model, model.provider)
        else:
            logger.warning("[LSP4J] 模型查找失败: key={}", model_key)
        return model


class JSONRPCRouter:
    """LSP4J JSON-RPC 2.0 路由器。

    每个 WebSocket 连接创建一个实例，负责：
    - LSP 生命周期管理（initialize/initialized/shutdown/exit）
    - 聊天请求路由（chat/ask → call_llm → 流式回调推送）
    - 工具调用编排（tool/invoke → IDE 执行 → 结果回传）
    - 对话持久化（ChatSession + ChatMessage）
    """

    def __init__(
        self,
        websocket: Any,
        user_id: uuid.UUID,
        agent_obj: AgentModel,
        model_obj: LLMModel,
    ) -> None:
        self._ws = websocket
        self._user_id = user_id
        self._agent_obj = agent_obj
        self._model_obj = model_obj
        self._agent_id = agent_obj.id
        self._session_id: str | None = None

        # JSON-RPC 请求 ID 计数器（用于发送 server→client 请求如 tool/invoke）
        self._request_id_counter: int = 0

        # LSP 协议解析器
        self._parser = LSPBaseProtocolParser()

        # pending tool Futures: toolCallId → asyncio.Future
        # 从 ContextVar 读取（由 router.py 的 WebSocket 端点设置）
        self._pending_tools: dict[str, asyncio.Future] = {}
        # pending tool 元数据：toolCallId → {request_id, tool_name, created_at}
        # 用于 tool/invokeResult 缺少 toolCallId 时的兜底匹配。
        self._pending_tool_meta: dict[str, dict[str, Any]] = {}

        # pending JSON-RPC 响应 Futures: request_id → asyncio.Future
        self._pending_responses: dict[int, asyncio.Future] = {}

        # cancel 事件（chat/stop 使用）
        self._cancel_event: asyncio.Event | None = None

        # chat 并发锁，防止多个 chat/ask 同时执行
        self._chat_lock = asyncio.Lock()

        # 当前请求 ID（用于 chat/answer 等消息中携带的 requestId）
        self._current_request_id: str | None = None

        # 项目根路径（从 initialize 的 rootUri 提取，用于 tool/call/sync 通知）
        self._project_path: str = ""

        # ★ toolCallId 队列：按序存储 (original_name, mapped_name, tool_call_id)，
        # original_name 为 LLM 侧名称（如 edit_file），mapped_name 为插件原生名称（如 replace_text_by_path）。
        # 支持 LLM 连续调用多个工具时各工具独立匹配 toolCallId。
        # 替代旧的单字段 _current_tool_call_id（无法处理多工具并发）。
        self._tool_call_id_queue: list[tuple[str, str, str]] = []

        # ★ 工具参数暂存：tool_call_id → params，
        # 用于 on_tool_call done 时发送 FINISHED sync 携带原始参数（如 file_path），
        # 确保插件端点击工具卡片可获取文件路径并打开文件。
        self._tool_params: dict[str, dict] = {}

        # ★ call_id → tool_call_id 映射：LLM 的 call_id → 后端生成的工具调用 ID，
        # 用于 on_tool_call done 时获取与 PENDING/RUNNING sync 一致的 toolCallId。
        self._call_id_to_tool_id: dict[str, str] = {}

        # 连接关闭标记（防止断开后继续发送消息）
        self._closed: bool = False
        # WebSocket 连接状态标志（主动标记，cleanup 时立即设为 False，比 _closed 更早阻止发送）
        self._ws_connected: bool = True

        # 图片上传缓存: request_id → (image_url, base64_data, timestamp)
        self._image_cache: dict[str, tuple[str, str, float]] = {}
        self._image_cache_max_size: int = 10
        self._image_cache_ttl: float = 600.0  # 10 分钟
        self._image_cleanup_task: asyncio.Task | None = None

        # tool/call/results 历史缓存（按 sessionId 分桶，Clawith-only 内存实现）。
        self._tool_call_history_by_session: dict[str, list[dict[str, Any]]] = {}
        self._tool_call_history_limit: int = 200
        self._MAX_TOOL_CALL_HISTORY_SESSIONS: int = 100

        # 已取消请求的 RPC ID 集合（防止超时后迟达响应干扰，OrderedDict 保证 FIFO）
        self._cancelled_requests: dict[int, None] = {}
        self._MAX_CANCELLED_REQUESTS_SIZE: int = 100

        # 工作区文件服务（diff 卡片支持）
        self._ws_file_service = WorkspaceFileService()

        # process_step_callback 去重节流缓存：
        # request_id -> {"sig": (step, status, description), "ts": monotonic_ts}
        self._process_step_last_emit: dict[str, dict[str, Any]] = {}

        # ★ 性能计时: 当前请求的基准时间戳（在 _handle_chat_ask 中设置）
        self._perf_start: float = 0.0

    # ──────────────────────────────────────────
    # 性能计时工具
    # ──────────────────────────────────────────

    def _log_perf(self, label: str, start: float, last_t: float) -> float:
        """记录阶段性耗时，返回当前时间供下次调用。

        Args:
            label: 阶段名称标签
            start: 请求起始时间戳（monotonic）
            last_t: 上一个阶段结束的时间戳

        Returns:
            当前时间戳（monotonic），用于下次调用时传入
        """
        now = time.monotonic()
        elapsed = now - start
        delta = now - last_t
        self._perf_start = start
        logger.info("[LSP4J-PERF] {} delta={:.3f}s total={:.3f}s", label, delta, elapsed)
        return now

    async def route(self, raw_data: str) -> None:
        """路由一条 WebSocket 消息。

        解析 LSP Base Protocol → JSON-RPC 消息 → 按类型分发。

        Args:
            raw_data: WebSocket 收到的原始文本帧
        """
        messages = self._parser.read_message(raw_data)
        for msg in messages:
            # 检测 JSON 解析失败，返回 JSON-RPC -32700 Parse error
            if isinstance(msg, ParseError):
                await self._send_error_response(None, -32700, msg.message)
                continue
            await self._dispatch(msg)

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        """分发单条 JSON-RPC 消息。"""
        # 1. 判断是否为 JSON-RPC 响应（client 响应 server 的请求如 tool/invoke）
        if "id" in msg and "method" not in msg and ("result" in msg or "error" in msg):
            error_detail = msg.get("error", {}).get("message", "") if "error" in msg else ""
            logger.info(
                "[LSP4J ←] response: id={} has_result={} has_error={} error_detail={}",
                msg.get("id"),
                "result" in msg,
                "error" in msg,
                error_detail,
            )
            await self._handle_response(msg)
            return

        method = msg.get("method", "")
        params = msg.get("params", {})
        msg_id = msg.get("id")

        # 协议追踪日志：记录收到的每条请求/通知
        # 心跳/探活消息降级为 DEBUG，避免污染 INFO 日志（#2 修复）
        if method == "ping":
            logger.debug(
                "[LSP4J ←] method={} id={} params_keys={}",
                method,
                msg_id,
                list(params.keys()) if isinstance(params, dict) else type(params).__name__,
            )
        else:
            logger.info(
                "[LSP4J ←] method={} id={} params_keys={}",
                method,
                msg_id,
                list(params.keys()) if isinstance(params, dict) else type(params).__name__,
            )

        # 2. 核心方法路由
        handler = self._METHOD_MAP.get(method)
        if handler:
            await handler(self, params, msg_id)
            return

        # 3. 非核心方法通用处理（插件 JsonNotification / LSP 标准，无业务实现，静默忽略以免 -32601）
        if method in (
            "initialized",  # 生命周期通知，无需响应
            "textDocument/didOpen",  # 文档同步（通义灵码自动发送，忽略）
            "textDocument/didChange",
            "textDocument/didClose",
            "textDocument/didSave",
            "textDocument/willChange",  # 编辑器变更前触发，无业务需求
            "textDocument/willSave",  # 保存前触发
            "textDocument/willSaveWaitUntil",  # 保存前等待
            # LanguageServer.java — 见 docs/plugin-analysis/15-complete-method-by-method-gap-analysis.md P0
            "window/workDoneProgress/cancel",
            "settings/change",
            "statistics/compute",
            "statistics/general",
            "config/updateGlobal",
            "config/updateGlobalMcpAutoRun",
            "config/appendCommandAllowList",
            "config/removeCommandAllowList",
            "config/updateGlobalWebToolsAutoExecute",
            "config/updateGlobalTerminalRunMode",
        ):
            # 通知类型，无需响应
            return

        # 4. 未知方法：如果有 id 则返回 method-not-found 错误（标准 JSON-RPC 2.0 错误码）
        # 这涵盖了 LSP 标准方法中尚未显式适配的约 30 个 TextDocumentService 方法
        # （completionItem/resolve, hover, definition, references, codeAction, etc.）
        if msg_id is not None:
            logger.info("LSP4J: 未适配方法, 返回 -32601 method={} id={}", method, msg_id)
            await self._send_error_response(msg_id, -32601, f"Method not found: {method}")
        else:
            logger.debug("LSP4J: 忽略未知通知 method={}", method)

    # ──────────────────────────────────────────
    # LSP 生命周期
    # ──────────────────────────────────────────

    async def _handle_initialize(self, params: dict, msg_id: Any) -> None:
        """处理 initialize 请求。

        通义灵码连接后第一个请求，返回服务器能力声明。
        同时从 params.rootUri 提取项目根路径。
        """
        # LSP 初始化
        logger.info("[LSP4J-LIFE] initialize: params_keys={}", list(params.keys()))

        # 提取项目根路径：优先 rootUri，其次 workspaceFolders[0].uri
        # 通义灵码插件不传 rootUri，只传 workspaceFolders（LSP 标准格式）
        import urllib.parse
        import os

        def _extract_path_from_uri(uri: str) -> str | None:
            """从 file:// URI 提取文件路径，含安全校验。"""
            if not uri:
                return None
            try:
                parsed = urllib.parse.urlparse(uri)
                path = parsed.path
                if path:
                    norm_path = os.path.normpath(path)
                    if ".." in norm_path.split(os.sep):
                        logger.warning("[LSP4J-LIFE] URI 含路径穿越，忽略: {}", uri)
                        return None
                    return norm_path
            except Exception as e:
                logger.warning("[LSP4J-LIFE] URI 解析失败: {} error={}", uri, e)
            return None

        # 1. 尝试 rootUri
        root_uri = params.get("rootUri", "")
        self._project_path = _extract_path_from_uri(root_uri) or ""

        # 2. rootUri 为空时尝试 workspaceFolders
        if not self._project_path:
            workspace_folders = params.get("workspaceFolders", [])
            if workspace_folders:
                first_folder = workspace_folders[0] if isinstance(workspace_folders[0], dict) else {}
                folder_uri = first_folder.get("uri", "")
                self._project_path = _extract_path_from_uri(folder_uri) or ""

        if self._project_path:
            logger.info("[LSP4J-LIFE] projectPath 提取: {}", self._project_path)
            # 异步构建文件索引（fire-and-forget，不阻塞 initialize 响应）
            _t_idx = asyncio.create_task(_build_project_file_index(self._project_path))
            _lsp4j_background_tasks.add(_t_idx)
            _t_idx.add_done_callback(_lsp4j_background_tasks.discard)
        else:
            logger.warning(
                "[LSP4J-LIFE] projectPath 未能提取: rootUri={} workspaceFolders={}",
                root_uri[:80] if root_uri else "(empty)",
                [f.get("uri", "")[:80] for f in (params.get("workspaceFolders") or [])],
            )

        await self._send_response(
            msg_id,
            {
                "capabilities": {
                    "textDocumentSync": {"openClose": True, "change": 1},
                    "completionProvider": {"resolveProvider": False, "triggerCharacters": ["."]},
                },
                "serverInfo": {"name": "Clawith LSP4J", "version": "0.1.0"},
            },
        )

    async def _ensure_project_path_ready(self, msg_id: Any, method: str) -> bool:
        """确保 projectPath 已就绪，防止空 projectPath 导致跨事件路由异常。"""
        if self._project_path:
            return True
        message = "projectPath is empty; initialize with rootUri/workspaceFolders before chat."
        logger.warning("[LSP4J-LIFE] {} rejected: {}", method, message)
        if msg_id is not None:
            await self._send_error_response(msg_id, -32002, message)
        return False

    async def _handle_shutdown(self, params: dict, msg_id: Any) -> None:
        """处理 shutdown 请求。"""
        logger.info("[LSP4J-LIFE] shutdown")
        await self._send_response(msg_id, None)
        self._closed = True

    async def _handle_exit(self, params: dict, msg_id: Any) -> None:
        """处理 exit 通知。连接即将关闭。"""
        logger.info("[LSP4J-LIFE] exit")
        self._closed = True
        # exit 是通知，无 id，不响应
        pass

    # ──────────────────────────────────────────
    # Chat 核心
    # ──────────────────────────────────────────

    async def _handle_chat_ask(self, params: dict, msg_id: Any) -> None:
        """处理 chat/ask 请求 — 核心聊天流程。

        流程：
        1. 发送 chat/think(step="start") 通知 IDE 进入思考状态
        2. 从数据库回填历史消息
        3. 调用 call_llm，通过 on_chunk/on_tool_call/on_thinking 回调推送流式内容
        4. 后台持久化 ChatSession + ChatMessage
        5. 发送 chat/finish 通知 IDE 完成

        参数来自 ChatAskParam（17 个字段），我们关注：
        - requestId: 请求 ID（必需，推送消息中必须携带）
        - sessionId: 会话 ID（UUID 格式）
        - questionText: 用户消息
        - stream: 是否流式
        - chatContext: 附加上下文
        - sessionType: 会话类型（写入 extra 字段）
        """
        # 连接已关闭，拒绝新请求
        # ★ 须尽力回 JSON-RPC error：否则 LSP4J 对带 id 的 @JsonRequest 会一直等到超时，
        #    IDE 表现为「发消息后没有任何回复」（_send_message 在 _closed 时会静默丢弃）。
        if self._closed:
            logger.warning("[LSP4J] chat/ask rejected: connection closed requestId={}", msg_id)
            await self._send_jsonrpc_error_best_effort(
                msg_id,
                -32603,
                "LSP connection closed; please reconnect the IDE plugin.",
            )
            return

        # 解析参数（兼容旧版插件缺少字段）
        ask = ChatAskParam(**{k: v for k, v in params.items() if k in ChatAskParam.__dataclass_fields__})

        request_id = ask.requestId or str(uuid.uuid4())
        session_id = ask.sessionId
        question_text = (ask.questionText or "").strip() or (str(ask.chatContext or "")).strip()
        chat_context = str(ask.chatContext or "")

        # 保存流式模式和会话类型（供后续回调使用）
        self._stream_mode = ask.stream
        self._current_session_type = ask.sessionType or ""

        if not question_text:
            # 空消息拒绝
            logger.warning("[LSP4J] chat/ask rejected: empty questionText, requestId={}", request_id)
            await self._send_error_response(msg_id, -32602, "Missing questionText")
            return
        if not await self._ensure_project_path_ready(msg_id, "chat/ask"):
            return

        logger.info(
            "[LSP4J] chat/ask 开始处理: requestId={} sessionId={} stream={} chatTask={} mode={}",
            request_id,
            session_id,
            ask.stream,
            ask.chatTask,
            ask.mode,
        )

        # 清理过期的工具调用缓存
        _now = time.monotonic()
        _expired = [
            k for k, (ts, _) in _lsp4j_tool_cache.items()
            if _now - ts > _LSP4J_TOOL_CACHE_TTL
        ]
        for k in _expired:
            del _lsp4j_tool_cache[k]
        if _expired:
            logger.info("[LSP4J-CACHE] cleaned {} expired entries", len(_expired))

        # 并发保护：同一连接只允许一个 chat/ask 同时执行
        if self._chat_lock.locked():
            # 并发请求拒绝
            logger.warning(
                "[LSP4J] chat/ask rejected: concurrent request, requestId={} current={}",
                request_id,
                self._current_request_id,
            )
            await self._send_error_response(msg_id, -32602, "Another chat is in progress")
            return

        async with self._chat_lock:
            # 记录当前请求 ID（后续推送消息需要携带）
            self._current_request_id = request_id
            _ask_start = time.monotonic()  # 计时起点

            # ★ 性能计时: 请求进入锁
            _t0 = _ask_start

            # 设置 session_id
            if session_id:
                self._session_id = session_id
                current_lsp4j_session_id.set(session_id)

                # 自动生成会话标题（取用户消息前 40 字符，替换换行为空格）
                auto_title = question_text[:40].replace("\n", " ").strip()
                if auto_title:
                    await self._send_session_title_update(session_id, auto_title)

            # ★ 性能计时: 会话初始化
            _t0 = self._log_perf("SESSION_INIT", _ask_start, _t0)

            # 创建 cancel 事件
            self._cancel_event = asyncio.Event()

            # ★ 重置 toolCallId 队列（新请求开始时清空）
            self._tool_call_id_queue = []

            # ★ 创建 snapshot 并发送 snapshot/syncAll（diff 卡片支持）
            await self._ws_file_service.get_or_create_snapshot(session_id, request_id)
            try:
                await self._send_snapshot_sync_all(session_id)
            except Exception as e:
                logger.warning("[WS-FILE] snapshot/syncAll 发送失败（非致命）: {}", e)

            # ★ 性能计时: snapshot 操作
            _t0 = self._log_perf("SNAPSHOT", _ask_start, _t0)

            # ★ 立即返回 JSON-RPC 响应，避免 IDE 端 LSP4J 框架请求超时
            # chat/ask 是 @JsonRequest，LSP4J 框架会等待响应；但 call_llm 可能运行数分钟，
            # 所以必须先返回响应确认收到请求，后续内容通过通知推送。
            # 这与 commitMsg/generate 的模式一致。
            await self._send_response(
                msg_id,
                {
                    "isSuccess": True,
                    "requestId": request_id,
                    "status": "processing",
                },
            )

            # ★ 性能计时: JSON-RPC 响应发送
            _t0 = self._log_perf("JSONRPC_RESPONSE", _ask_start, _t0)

            # 1. 发送思考状态（ChatThinkingParams 格式）
            await self._send_chat_think(session_id, "思考中...", "start", request_id)

            # ★ 性能计时: 发送 think start
            _t0 = self._log_perf("THINK_START", _ask_start, _t0)

            # 2. 从数据库回填历史消息（优先内存缓存）
            message_history: list[dict] = current_lsp4j_message_history.get() or []
            if session_id and not message_history:
                # 清理过期缓存
                _now = time.monotonic()
                _stale_keys = [k for k, (ts, _) in _SESSION_MESSAGE_CACHE.items() if _now - ts > _SESSION_MESSAGE_CACHE_TTL]
                for _k in _stale_keys:
                    _SESSION_MESSAGE_CACHE.pop(_k, None)
                # 检查内存缓存
                _cache_key = f"{self._user_id}:{session_id}"
                _cached = _SESSION_MESSAGE_CACHE.get(_cache_key)
                if _cached:
                    _cache_ts, _cached_msgs = _cached
                    if _now - _cache_ts < _SESSION_MESSAGE_CACHE_TTL:
                        message_history = _cached_msgs
                        current_lsp4j_message_history.set(message_history)
                        logger.info(
                            "[LSP4J-CTX] 复用内存消息缓存: session={} msgs={} cache_age={:.0f}s",
                            session_id,
                            len(_cached_msgs),
                            _now - _cache_ts,
                        )
                    else:
                        _SESSION_MESSAGE_CACHE.pop(_cache_key, None)
                # 内存未命中则查 DB
                if not message_history:
                    logger.info(
                        "[LSP4J-CTX] chat/ask: message_history empty, loading from DB session={}",
                        session_id,
                    )
                    _t_hist_start = time.monotonic()
                    loaded = await _load_lsp4j_history_from_db(session_id, self._agent_id, self._user_id)
                    _t_hist_elapsed = time.monotonic() - _t_hist_start
                    if loaded:
                        message_history = loaded
                        current_lsp4j_message_history.set(message_history)
                        _SESSION_MESSAGE_CACHE[_cache_key] = (_now, loaded)
                        logger.info(
                            "[LSP4J-PERF] DB history loaded: session={} elapsed={:.3f}s rows={}",
                            session_id,
                            _t_hist_elapsed,
                            len(loaded),
                        )
                    else:
                        logger.info(
                            "[LSP4J-PERF] DB history loaded (empty): session={} elapsed={:.3f}s",
                            session_id,
                            _t_hist_elapsed,
                        )
            else:
                logger.debug(
                    "[LSP4J-CTX] chat/ask: message_history already populated ({}) or no session_id ({})",
                    len(message_history),
                    session_id,
                )
            # 从内存注入工具调用上下文（始终以内存为准，覆盖 DB 可能不完整的上下文）
            # 内存中的记录包含最新的工具调用（含 DB 尚未完成持久化的窗口），比 DB 更完整
            if session_id and session_id in self._tool_call_history_by_session:
                inmem_records = self._tool_call_history_by_session[session_id]
                logger.info(
                    "[LSP4J-CTX] chat/ask: found {} in-memory tool_call records for session={}",
                    len(inmem_records),
                    session_id,
                )
                # 移除 DB 注入的旧上下文（如有），以内存最新记录替换，避免丢失未持久化的工具调用
                message_history = [
                    m
                    for m in message_history
                    if not (m.get("role") == "system" and "[会话上下文]" in m.get("content", ""))
                ]
                if inmem_records:
                    _t_ctx_start = time.monotonic()
                    ctx_msg = _format_tool_context_message(inmem_records)
                    _t_ctx_elapsed = time.monotonic() - _t_ctx_start
                    if ctx_msg:
                        message_history.insert(0, ctx_msg)
                        logger.info(
                            "[LSP4J] tool_call context injected from memory: session_id={} tool_count={}",
                            session_id,
                            len(inmem_records),
                        )
                        logger.info(
                            "[LSP4J-PERF] Memory context format: session={} elapsed={:.3f}s ctx_len={}",
                            session_id,
                            _t_ctx_elapsed,
                            len(ctx_msg.get("content", "")),
                        )
                    else:
                        logger.info(
                            "[LSP4J-CTX] chat/ask: memory context NOT injected (no useful records after formatting), "
                            "session={} tool_count={}",
                            session_id,
                            len(inmem_records),
                        )
            else:
                logger.debug(
                    "[LSP4J-CTX] chat/ask: no in-memory tool_call records for session={}",
                    session_id,
                )
            # 历史消息加载完成
            history_msgs = message_history or []
            history_roles = {}
            for m in history_msgs:
                r = m.get("role", "unknown")
                history_roles[r] = history_roles.get(r, 0) + 1
            _history_total_chars = sum(len(m.get("content", "")) for m in history_msgs)
            logger.info(
                "[LSP4J-PERF] History summary: session={} messages={} roles={} total_chars={}",
                session_id,
                len(history_msgs),
                history_roles,
                _history_total_chars,
            )

            # 拼接用户消息
            full_text = question_text
            if chat_context and chat_context != question_text:
                full_text = f"{question_text}\n\n[附加上文]\n{chat_context}"

            # ★ 注入代码结构地图（Aider/Cursor 策略：LLM 直接知道项目结构，不需要搜索探索）
            if self._project_path:
                from .file_index import get_or_build_code_map as _get_cmap
                _code_map = _get_cmap(self._project_path)
                if _code_map:
                    message_history = [
                        m for m in message_history
                        if not (m.get("role") == "system" and "项目代码结构地图" in m.get("content", ""))
                    ]
                    message_history.insert(0, {"role": "system", "content": _code_map})
                    logger.info("[CODE-MAP] injected: project={} map_len={}", self._project_path, len(_code_map))
                else:
                    logger.warning("[CODE-MAP] empty: project={}", self._project_path)

            message_history.append({"role": "user", "content": full_text})

            # 3. 定义流式回调
            reply_parts: list[str] = []
            thinking_chunks: list[str] = []
            _thinking_started: bool = False

            # ★ 流式输出缓冲：按"完整行"发送；Markdown 表格按整块发送，避免被拆成半截列。
            buffer = StreamBufferManager(
                send_fn=lambda payload: self._send_chat_answer(session_id, payload, request_id)
            )

            async def on_chunk(text: str) -> None:
                """流式文本回调 — 推送 chat/answer（ChatAnswerParams 格式）

                使用缓冲区累积小 chunk，按行或阈值发送，避免 markdown 表格被拆分成单个字符，
                导致 MarkdownStreamPanel 无法正确解析。
                """
                nonlocal _thinking_started
                # 思考结束标记：收到首个正文 chunk 即表示思考阶段结束
                if _thinking_started:
                    _thinking_started = False
                    await self._send_chat_think(session_id, "", "done", request_id)

                # 检查取消事件
                if self._cancel_event and self._cancel_event.is_set():
                    logger.info("[LSP4J] on_chunk: cancelled by chat/stop, chunks_sent={}", len(reply_parts))
                    raise asyncio.CancelledError("chat/stop requested")

                reply_parts.append(text)

                # ★ 缓冲逻辑：累积 chunk，按行或阈值发送
                if not getattr(self, "_stream_mode", True):
                    # 非流式模式：不发送，在 finish 中一次性返回
                    return

                if buffer.feed(text):
                    await buffer.flush()

            async def on_thinking(text: str) -> None:
                """推理过程回调 — 逐步推送 DeepSeek reasoning_content 到 IDE。

                插件 ChatThinkingProcessor 监听 chat/think 通知并渲染到思考面板。
                避免前端在 LLM 推理期间（28s-41s）完全静止。
                """
                nonlocal _thinking_started
                thinking_chunks.append(text)
                if not _thinking_started:
                    _thinking_started = True
                    await self._send_chat_think(session_id, "思考中...", "start", request_id)
                # ★ 逐步推送推理文本：每 15 chunk 或遇换行时发送一次
                # 避免每个 token 都发一条通知（过多 RPC 开销），同时保证前端持续有内容更新
                if len(thinking_chunks) % 15 == 0 or "\n" in text:
                    await self._send_chat_think(session_id, "".join(thinking_chunks), "start", request_id)

            async def on_tool_call(data: dict) -> None:
                """工具调用回调 — 推送状态通知给 IDE + step callback + toolCall markdown"""
                status = data.get("status", "running")
                tool_name = data.get("name", "unknown")
                if status == "running":
                    # ★ 应用工具名映射（与 tool_hooks.TOOL_NAME_MAP 保持一致）
                    # LLM 可能调用基础工具名（如 edit_file），需映射为插件原生名称（如 replace_text_by_path）
                    # 映射后队列中的名称与 invoke_tool_on_ide 收到的一致，保证匹配
                    original_name = tool_name
                    tool_name = TOOL_NAME_MAP.get(tool_name, tool_name)
                    if tool_name != original_name:
                        logger.debug("[LSP4J] on_tool_call 工具名映射: {} → {}", original_name, tool_name)

                    # ★ 显示名映射：插件 ToolTypeEnum.getByToolName() 不识别插件原生名称，
                    # 只识别 LLM 侧名称（如 edit_file）。markdown 块必须用 LLM 侧名称。
                    # 当 LLM 直接调用插件原生名（如 replace_text_by_path）时，反向映射为显示名。
                    display_name = TOOL_DISPLAY_NAME_MAP.get(original_name, original_name)

                    # ★ 只对实际会路由到 IDE 执行的工具生成 toolCallId（供后续 invoke_tool_on_ide 按序匹配）
                    # 相对路径（如 soul.md, workspace/xxx）回退到本地执行，不创建 IDE 工具卡片
                    is_lsp4j_tool = tool_name in LSP4J_IDE_TOOL_NAMES
                    tool_call_id = ""
                    _should_create_ide_card = False
                    if is_lsp4j_tool:
                        from .tool_hooks import _should_route_to_ide as _route_check

                        raw_args = data.get("args", {})
                        _should_create_ide_card = _route_check(tool_name, raw_args)
                        if not _should_create_ide_card:
                            logger.info(
                                "[LSP4J-TOOL] 跳过 IDE 工具卡片（相对路径回退本地）: tool={} args={}",
                                tool_name,
                                {
                                    k: v
                                    for k, v in raw_args.items()
                                    if k in ("path", "file_path", "filePath", "relative_workspace_path")
                                },
                            )
                    if _should_create_ide_card:
                        tool_call_id = str(uuid.uuid4())
                        # ★ 队列存储 3 元组：(显示名, 插件原生名称, UUID)
                        # display_name 供 markdown 块使用（ToolPanel 需要 LLM 侧名称识别文件工具）
                        # tool_name 供 invoke_tool_on_ide 按插件原生名称匹配
                        self._tool_call_id_queue.append((display_name, tool_name, tool_call_id))
                        raw_args = data.get("args", {})
                        # ★ 保留原始 snake_case 参数（不做 camelCase 转换）
                        # sync 通知的 parameters 需用 snake_case（ToolPanel.constructFileItem() 读取 file_path 键），
                        # tool/invoke 的 camelCase 转换统一在 invoke_tool_on_ide 中集中处理。
                        # 此前分散在此处的转换逻辑已全部移除。
                        params = dict(raw_args)
                        # read_file 的链接渲染依赖 parameters["file_path"]，
                        # 但模型常传 "path"；这里统一补齐给 ToolPanel 用。
                        if tool_name == "read_file":
                            fp = params.get("file_path") or params.get("path") or params.get("filePath")
                            if fp and "file_path" not in params:
                                params["file_path"] = fp
                        self._tool_params[tool_call_id] = params
                        # run_in_terminal: 确保 run_mode 有默认值，避免客户端 PENDING sync 时收到 null
                        if tool_name == "run_in_terminal" and "run_mode" not in params:
                            params["run_mode"] = "autoRun"
                        llm_call_id = data.get("call_id", "")
                        if llm_call_id:
                            self._call_id_to_tool_id[llm_call_id] = tool_call_id
                        logger.debug(
                            "[LSP4J] toolCallId 入队: name={} callId={} queue_len={}",
                            tool_name,
                            tool_call_id[:8],
                            len(self._tool_call_id_queue),
                        )
                        logger.info(
                            "[LSP4J-TOOL] toolCall 入队: display={} mapped={} callId={} queue_len={}",
                            display_name,
                            tool_name,
                            tool_call_id[:8],
                            len(self._tool_call_id_queue),
                        )
                        logger.info(
                            "[LSP4J-TRACE] 入队 trace=req={} call={} logical={} invoke={}",
                            request_id[:8],
                            tool_call_id[:8],
                            display_name,
                            tool_name,
                        )

                    if _should_create_ide_card:
                        # ★ 发送 toolCall markdown 块（双通道之 markdown 通道）
                        # 插件 MarkdownStreamPanel 解析此格式创建工具卡片 UI
                        # 插件正则：```([\w#+.-]+)::([^\n]+)::([^\n]+)\n+(.*?)```
                        # group(1)=toolCall, group(2)=name, group(3)=toolCallId
                        # ⚠️ 关键：markdown 块仅用于创建工具卡片，不在这里发送 INIT/FINISHED。
                        # 状态流转统一走 tool/call/sync，避免 UI 出现 "list_files FINISHED" 刷屏。
                        # ★ 使用显示名（display_name），而非插件原生名称（tool_name）
                        # 插件 MarkdownStreamPanel 解析此块 → 构造 ToolInfo → ToolPanel 构造时调用
                        # ToolTypeEnum.getByToolName(toolName) 识别工具类型。
                        # 若使用插件原生名称（如 replace_text_by_path），getByToolName 返回 UNKNOWN，
                        # 文件工具分支永远不进入，导致无 AIDevFilePanel（diff 卡片）。
                        # 插件 MarkdownStreamPanel.parseBlock 依赖四段结构：
                        # ```toolCall::toolName::toolCallId::INIT
                        # ```
                        # 若缺少末尾状态段，插件会在 split("::")[1] 处越界。
                        markdown_block = f"```toolCall::{display_name}::{tool_call_id}::INIT\n```"

                        logger.info(
                            "[LSP4J-TOOL] 准备发送 toolCall markdown 块: mapped={} callId={}",
                            tool_name,
                            tool_call_id[:8],
                        )
                        logger.info(
                            "[LSP4J-TOOL] markdown 块使用显示名: name={} callId={}", display_name, tool_call_id[:8]
                        )
                        if getattr(self, "_stream_mode", True):
                            await self._send_chat_answer(session_id, markdown_block, request_id)
                            logger.info("[LSP4J-TOOL] toolCall markdown 块已发送: callId={}", tool_call_id[:8])
                        else:
                            logger.info(
                                "[LSP4J-TOOL] 非流式模式，跳过 toolCall markdown 块发送: callId={}", tool_call_id[:8]
                            )

                        # ★ 插件 ChatToolEventProcessor 采用 buffer+replay 机制：
                        # 事件先于 panel 注册到达时自动缓冲，registerPanel 时 replay。
                        # 后端无需等待，yield 控制权即可。
                        await asyncio.sleep(0)

                        # 发送 PENDING sync（双通道之事件通道）
                        # 插件 ChatToolEventProcessor 收到后更新卡片参数（scopeLabel 依赖 parameters）
                        # ★ 使用 LLM 侧名称 + snake_case 参数
                        # ToolPanel 存储 sync 中的 toolName 用于后续 FINISHED 时判断是否为文件工具，
                        # 同时 parameters["file_path"] 用于 constructFileItem() 渲染文件链接。
                        if tool_name in _UI_HEAVY_SEARCH_TOOLS:
                            logger.info(
                                "[LSP4J-TRACE] 跳过 PENDING（高频搜索降噪）: req={} call={} tool={}",
                                request_id[:8],
                                tool_call_id[:8],
                                tool_name,
                            )
                        else:
                            await self._send_tool_call_sync(
                                session_id,
                                request_id,
                                tool_call_id,
                                "PENDING",
                                tool_name=original_name,
                                parameters=params,
                            )

                    await self._send_process_step_callback(
                        session_id,
                        request_id,
                        step=f"tool_{tool_name}",
                        description=f"正在执行: {tool_name}",
                        status="doing",
                    )
                    await self._send_chat_think(
                        session_id,
                        f"正在调用工具: {tool_name}",
                        "start",
                        request_id,
                    )
                elif status == "done":
                    # ★ 应用工具名映射（与 running 分支一致）
                    original_name = tool_name
                    tool_name = TOOL_NAME_MAP.get(tool_name, tool_name)
                    if tool_name != original_name:
                        logger.debug("[LSP4J] on_tool_call done 工具名映射: {} → {}", original_name, tool_name)

                    # ★ IDE 工具：FINISHED sync 已由 invoke_tool_on_ide 发送（携带实际执行结果和 fileId），
                    # 跳过此处的重复 FINISHED，避免：1) 空结果覆盖 invoke_tool_on_ide 的真实结果；
                    # 2) invoke_tool_on_ide 已从队列 pop 后此处匹配失败导致新建兜底 UUID（两个不同 callId）。
                    is_lsp4j_tool = tool_name in LSP4J_IDE_TOOL_NAMES
                    if is_lsp4j_tool:
                        logger.debug(
                            "[LSP4J] on_tool_call done: 跳过 IDE 工具的 FINISHED sync, "
                            "已由 invoke_tool_on_ide 发送, original={} mapped={}",
                            original_name,
                            tool_name,
                        )
                    else:
                        # ★ 非 IDE 工具（纯后端执行，不走 invoke_tool_on_ide）：正常发送 FINISHED sync
                        llm_call_id = data.get("call_id", "")
                        finished_call_id = self._call_id_to_tool_id.pop(llm_call_id, "") if llm_call_id else ""

                        if not finished_call_id:
                            # 队列匹配兜底（适配 3 元组）
                            for i, (orig_name, mapped_name, stored_id) in enumerate(self._tool_call_id_queue):
                                if mapped_name == tool_name:
                                    finished_call_id = stored_id
                                    self._tool_call_id_queue.pop(i)
                                    logger.debug(
                                        "[LSP4J] toolCallId 队列匹配 (done): name={} callId={}",
                                        tool_name,
                                        finished_call_id[:8],
                                    )
                                    break

                        done_params = self._tool_params.pop(finished_call_id, {}) if finished_call_id else {}

                        if not finished_call_id:
                            # ★ 没有匹配的 toolCallId → 没有对应的 ToolPanel
                            # 不能发送 FINISHED sync：若使用随机 UUID，ChatToolEventProcessor
                            # 会为每个幽灵事件等待 10s（timeoutWaitPanel），堵塞所有后续工具事件。
                            # 非 IDE 工具（如 execute_code）走纯后端执行，无 toolCallId 是正常行为
                            logger.debug("[LSP4J] toolCallId 未匹配 (done)，跳过 FINISHED sync: name={}", tool_name)
                        else:
                            await self._send_tool_call_sync(
                                session_id,
                                request_id,
                                finished_call_id,
                                "FINISHED",
                                tool_name=original_name,
                                parameters=done_params,
                            )

                    await self._send_process_step_callback(
                        session_id,
                        request_id,
                        step=f"tool_{tool_name}",
                        description=f"已完成: {tool_name}",
                        status="done",
                    )
                    await self._send_chat_think(
                        session_id,
                        f"工具 {tool_name} 执行完成",
                        "done",
                        request_id,
                    )
                    # 持久化 tool_call 消息（与 Web 通道 JSON 字段一致）
                    try:
                        async with async_session() as _tc_db:
                            tc_msg = ChatMessage(
                                conversation_id=session_id or "",
                                role="tool_call",
                                content=json.dumps(
                                    {
                                        "name": tool_name,
                                        "args": data.get("args"),
                                        "status": "done",
                                        "result": (data.get("result") or "")[:500],
                                        "reasoning_content": data.get("reasoning_content"),
                                    },
                                    ensure_ascii=False,
                                ),
                                agent_id=self._agent_id,
                                user_id=self._user_id,
                            )
                            _tc_db.add(tc_msg)
                            await _tc_db.commit()
                            logger.debug("[LSP4J] tool_call 持久化成功: tool={} sessionId={}", tool_name, session_id)
                    except Exception as _tc_e:
                        logger.warning(
                            "[LSP4J] tool_call 持久化失败: tool={} sessionId={} error={}", tool_name, session_id, _tc_e
                        )

            # 4. 调用 call_llm
            # 构建 IDE 环境提示（chatTask、codeLanguage 等注入 role_description）
            ide_prompt = _build_lsp4j_ide_prompt(ask)
            role_desc = self._agent_obj.role_description or ""
            if ide_prompt:
                role_desc = role_desc + ide_prompt
            # 注入工具可用性提示和项目路径
            tool_hint = "\n[工具可用性] 已连接本地 IDE 环境，可直接使用 read_file、replace_text_by_path、run_in_terminal、get_terminal_output、create_file_with_text、delete_file_by_path、get_problems 等工具访问项目文件。"
            if self._project_path:
                tool_hint += f"\n[项目根路径] {self._project_path}"

            # ★ 优先工具链路：文件改动应通过 IDE 工具调用触发 ToolPanel / diff 卡片。
            # CODE_EDIT_BLOCK 仅作为兜底（当工具调用明确不可用时才允许）。
            tool_hint += (
                "\n[执行策略] 对于读写文件、创建文件、删除文件、搜索、终端命令，必须优先使用工具调用。"
                "\n优先级：先按意图选择工具（符号类优先 search_symbol，文件类优先 search_file，文本类优先 search_codebase/grep_code），"
                "若 0 结果则按回退链路自动放宽。"
                "\n先给出'读取清单'（3-8 个关键文件）再批量读取，不要边搜边读反复循环。"
                "\n同一轮禁止重复调用相同工具+相同参数；已有结果优先复用。"
                "\n优先使用 read_file / replace_text_by_path / create_file_with_text / delete_file_by_path / list_dir / search_file / run_in_terminal。"
                "\n不要先输出大段'开始重构/步骤说明'代码块再改文件，优先直接执行工具并回传结果。"
                "\n目标展示应为工具卡片链路（toolCall + tool/call/sync + workingSpaceFile/sync），而不是纯 markdown 代码块。"
                "\n仅当工具调用不可用或明确失败时，才允许输出 CODE_EDIT_BLOCK 作为兜底。"
                "\n[路径规则] 修改代码时必须使用绝对路径或项目相对路径（如 app/src/main/Main.kt），禁止使用 workspace/ 前缀。"
                "\nlist_dir/search_file 返回的路径可直接用于后续 read_file/replace_text_by_path 调用。"
                "\n相对路径仅用于 Agent 自身文件（soul.md, memory.md, focus.md, skills/）。"
            )

            role_desc = role_desc + tool_hint

            # ── 模型选择（优先级：customModel > extra.modelConfig.key > 默认） ──
            model_obj = self._model_obj
            supports_vision = getattr(model_obj, "supports_vision", False)

            # 5.1: customModel 处理（BYOK 模型，用户自带密钥）
            # customModel 字段基于灵码 CustomModelParam.kt：
            #   provider, model, isVl, isReasoning, maxInputTokens, parameters(Map<String,String>)
            if ask.customModel and isinstance(ask.customModel, dict):
                cm = ask.customModel
                _provider = cm.get("provider", "")
                _model_name = cm.get("model", "")
                if _provider and _model_name:
                    _params = cm.get("parameters", {})
                    transient_model = LLMModel(
                        id=uuid.uuid4(),
                        provider=_provider,
                        model=_model_name,
                        label=f"BYOK {_model_name}",
                        base_url=_params.get("base_url", ""),
                        api_key_encrypted="",  # 占位，实际密钥通过运行时属性注入
                    )
                    # ⚠️ 密钥仅当次请求有效，不入库、不写日志
                    # TODO: 改用 LLMModel 正式字段传递 API key，避免依赖私有属性 _runtime_api_key
                    # 当前依赖 call_llm_with_failover 内部检查此私有属性的约定，若 LLMModel 重构可能静默失效
                    transient_model._runtime_api_key = _params.get("api_key", "")
                    model_obj = transient_model
                    supports_vision = bool(cm.get("isVl"))
                    logger.info("LSP4J: 使用 BYOK 模型 {}:{}", _provider, _model_name)

            # 5.4: extra.modelConfig.key 处理（仅当 customModel 未覆盖时生效）
            if model_obj is self._model_obj and ask.extra and isinstance(ask.extra, dict):
                model_config = ask.extra.get("modelConfig", {})
                model_key = model_config.get("key", "") if isinstance(model_config, dict) else ""
                if model_key:
                    override = await _resolve_model_by_key(model_key)
                    if override:
                        model_obj = override
                        supports_vision = getattr(override, "supports_vision", False)
                        logger.info("LSP4J: 使用模型配置 key={}", model_key)

            # 发送步骤开始通知（chat/process_step_callback）
            await self._send_process_step_callback(
                session_id,
                request_id,
                step="step_start",
                description="开始处理",
                status="doing",
            )

            # 加载 fallback 模型
            fallback_model_obj = None
            if self._agent_obj.fallback_model_id:
                async with async_session() as _fb_db:
                    _fb_r = await _fb_db.execute(
                        select(LLMModel).where(LLMModel.id == self._agent_obj.fallback_model_id)
                    )
                    fallback_model_obj = _fb_r.scalar_one_or_none()

            cancelled = False
            error_status_code = 200  # 默认成功
            # ★ 设置 IDE 会话标记，使 build_agent_context 注入项目优先指令
            _ide_token = _is_ide_session.set(True)

            # ★ 性能计时: 准备进入 call_llm
            _t_call_llm_start = time.monotonic()
            _context_chars = sum(
                len(m.get("content", "")) for m in message_history if isinstance(m.get("content"), str)
            )
            logger.info(
                "[LSP4J-PERF] PRE_CALL_LLM session={} model={} messages={} context_chars={}",
                session_id,
                model_obj.model if hasattr(model_obj, "model") else "unknown",
                len(message_history),
                _context_chars,
            )
            try:
                reply = await call_llm_with_failover(
                    primary_model=model_obj,
                    fallback_model=fallback_model_obj,
                    messages=message_history,
                    agent_name=self._agent_obj.name,
                    role_description=role_desc,
                    agent_id=self._agent_id,
                    user_id=self._user_id,
                    session_id=session_id or "",
                    on_chunk=on_chunk,
                    on_tool_call=on_tool_call,
                    on_thinking=on_thinking,
                    supports_vision=supports_vision,
                    cancel_event=self._cancel_event,
                    parallel_tools_extra_readonly={
                        "search_file", "search_codebase", "search_symbol",
                        "list_dir", "read_file", "grep_code",
                    },
                    tool_warning_mode="lsp4j",
                )
            except asyncio.CancelledError:
                cancelled = True
                reply = ""
                # 用户取消不是服务端错误，statusCode 仍为 200
            except Exception as e:
                logger.exception("LSP4J call_llm error")
                error_status_code = 500
                reply = f"[错误] {type(e).__name__}: {str(e)[:200]}"
            finally:
                _is_ide_session.reset(_ide_token)

            # 检测 LLM 调用返回的错误字符串（call_llm 内部 catch LLMError 后返回
            # 字符串 "[LLM Error] HTTP <code>: ..." 而非抛出异常，导致 error_status_code
            # 保持在 200。这里解析字符串以恢复真实状态码，用于区分 429 配额耗尽 vs
            # 500 内部错误，使 chat/finish 的 finish_reason 能正确反映错误类型。
            if isinstance(reply, str) and reply.startswith("[LLM Error]"):
                _m = re.search(r'HTTP (\d{3})', reply)
                error_status_code = int(_m.group(1)) if _m else 500
                logger.info(
                    "[LSP4J] LLM error string detected: statusCode={} preview={}...",
                    error_status_code,
                    reply[:120],
                )

            # ★ 性能计时: call_llm 完成
            _t_call_llm_elapsed = time.monotonic() - _t_call_llm_start
            logger.info(
                "[LSP4J-PERF] CALL_LLM_DONE session={} elapsed={:.1f}s cancelled={} reply_len={}",
                session_id,
                _t_call_llm_elapsed,
                cancelled,
                len(reply),
            )

            # ★ 性能计时: 思考完成
            _t0 = self._log_perf("THINK_DONE", _ask_start, _t0)

            # ★ 发送思考完成状态
            # 若 on_chunk 已被调用（_thinking_started=False），已在 on_chunk 中发送过 done，
            # 此处仅兜底处理纯思考无正文的场景（_thinking_started=True）。
            if _thinking_started:
                _thinking_started = False
                await self._send_chat_think(session_id, "", "done", request_id)

            # 发送步骤结束通知（chat/process_step_callback）
            await self._send_process_step_callback(
                session_id,
                request_id,
                step="step_end",
                description="处理完成",
                status="done",
            )

            # ★ 性能计时: 后置操作完成
            _t0 = self._log_perf("POST_CALLBACKS", _ask_start, _t0)

            logger.info(
                "[LSP4J] chat/ask 处理完成: requestId={} cancelled={} reply_len={} elapsed={:.1f}s",
                request_id,
                cancelled,
                len(reply),
                time.monotonic() - _ask_start,
            )

            # 检测任务创建意图
            if not cancelled and full_text and reply:
                _t_task_start = time.monotonic()
                task_match = re.search(
                    r"(?:创建|新建|添加|建一个|帮我建|create|add)(?:一个|a )?(?:任务|待办|todo|task)[，,：：:\\s]*(.+)",
                    full_text,
                    re.IGNORECASE,
                )
                if task_match:
                    task_title = task_match.group(1).strip()
                    if task_title:
                        try:
                            async with async_session() as _tsk_db:
                                task = TaskModel(
                                    agent_id=self._agent_id,
                                    title=task_title,
                                    created_by=self._user_id,
                                    status="pending",
                                    priority="medium",
                                )
                                _tsk_db.add(task)
                                await _tsk_db.commit()
                                await _tsk_db.refresh(task)
                                _task_id = task.id
                            asyncio.create_task(_execute_task(_task_id, str(self._agent_id)))
                            logger.info("[LSP4J] Task created: id={} title={}", _task_id, task_title)
                        except Exception as _te:
                            logger.warning("[LSP4J] Task creation failed: {}", _te)
                _t_task_elapsed = time.monotonic() - _t_task_start
                if _t_task_elapsed > 0.05:  # 只记录超过50ms的操作
                    logger.info(
                        "[LSP4J-PERF] TASK_DETECTION session={} elapsed={:.3f}s matched={}",
                        session_id,
                        _t_task_elapsed,
                        bool(task_match),
                    )

            # 5. 后台持久化
            if session_id and reply:
                _t_persist_start = time.monotonic()
                _t = asyncio.create_task(
                    _persist_lsp4j_chat_turn(
                        agent_id=self._agent_id,
                        session_id=session_id,
                        user_text=full_text,
                        reply_text=reply,
                        user_id=self._user_id,
                        thinking_text="".join(thinking_chunks) if thinking_chunks else None,
                    )
                )
                _t_persist_elapsed = time.monotonic() - _t_persist_start
                logger.info(
                    "[LSP4J-PERF] PERSIST_TASK_CREATED session={} create_elapsed={:.3f}s",
                    session_id,
                    _t_persist_elapsed,
                )
                _lsp4j_background_tasks.add(_t)
                _t.add_done_callback(_lsp4j_background_tasks.discard)

            # 更新消息历史（同时刷新模块级缓存，确保跨连接场景也能读到最新消息）
            message_history.append({"role": "assistant", "content": reply})
            current_lsp4j_message_history.set(message_history)
            _cache_key = f"{self._user_id}:{session_id}"
            _SESSION_MESSAGE_CACHE[_cache_key] = (time.monotonic(), list(message_history))

            # ★ 刷新缓冲区，确保所有累积的文本都已发送
            _t_flush_start = time.monotonic()
            await buffer.flush(force=True)
            _t_flush_elapsed = time.monotonic() - _t_flush_start

            # ★ 灵码 MarkdownStreamPanel 主要消费 chat/answer。`finish` 工具返回的正文不经
            # client.stream 的 on_chunk，reply_parts 为空时会出现 fullAnswer 非空但气泡空白。
            # 在 chat/finish 前把与最终 reply 一致的文本补发为 chat/answer（见 answer_sync.plan_*）。
            _streamed_plain = "".join(reply_parts)
            _stream_mode_on = getattr(self, "_stream_mode", True)
            _sync_plan = plan_answer_sync_before_finish(
                cancelled=cancelled,
                reply=reply or "",
                streamed_plain=_streamed_plain,
                stream_mode_on=_stream_mode_on,
            )
            logger.info(
                "[LSP4J-UI] answer_sync_plan requestId={} branch={} reply_len={} streamed_len={} "
                "stream_mode={} cancelled={}",
                request_id,
                _sync_plan.branch,
                len(reply or ""),
                len(_streamed_plain),
                _stream_mode_on,
                cancelled,
            )
            if _sync_plan.text is not None:
                await self._send_chat_answer(
                    session_id,
                    _sync_plan.text,
                    request_id,
                    overwrite=_sync_plan.overwrite,
                )
                logger.info(
                    "[LSP4J-UI] answer_sync_sent requestId={} branch={} send_len={} overwrite={}",
                    request_id,
                    _sync_plan.branch,
                    len(_sync_plan.text),
                    _sync_plan.overwrite,
                )
                if _sync_plan.branch == "overwrite_mismatch":
                    logger.warning(
                        "[LSP4J-UI] answer_sync overwrite_mismatch detail requestId={} streamed_len={} reply_len={}",
                        request_id,
                        len(_streamed_plain),
                        len(reply or ""),
                    )

            # 6. 发送完成信号（ChatFinishParams 格式）
            # statusCode 映射：200=成功, 429=配额耗尽, 408=超时, 500=异常
            # ★ 微延迟：插件端 ChatFinishProcessor 和 ChatAnswerProcessor 并发执行，
            # ChatFinishProcessor 会移除 REQUEST_TO_PROJECT 映射，导致 ChatAnswerProcessor
            # 找不到请求。给 200ms 窗口让 ChatAnswerProcessor 先完成查找。
            await asyncio.sleep(0.2)
            _t_finish_start = time.monotonic()
            if cancelled:
                finish_reason = "cancelled"
            elif error_status_code == 200:
                finish_reason = "success"
            elif error_status_code == 429:
                finish_reason = "error"
                # 配额耗尽时在回复末尾追加提示，帮助用户理解中断原因
                reply = (reply or "") + "\n\n> ⚠️ 模型配额已耗尽（HTTP 429），请稍后重试或联系管理员。"
            else:
                finish_reason = "error"
            await self._send_chat_finish(session_id, finish_reason, reply, request_id, status_code=error_status_code)

            # 发送 REQUEST_FINISHED 清理通知（tool/call/sync）
            await self._send_tool_call_sync(
                session_id,
                request_id,
                "",
                "REQUEST_FINISHED",
            )
            _t_finish_elapsed = time.monotonic() - _t_finish_start
            logger.info("[LSP4J-PERF] FINISH_NOTIFICATIONS session={} elapsed={:.3f}s", session_id, _t_finish_elapsed)

            # ★ 最终性能汇总
            _total_elapsed = time.monotonic() - _ask_start
            logger.info(
                "[LSP4J-PERF] TOTAL session={} elapsed={:.1f}s reply_len={} cancelled={} model={}",
                session_id,
                _total_elapsed,
                len(reply),
                cancelled,
                model_obj.model if hasattr(model_obj, "model") else "unknown",
            )

            # 7. JSON-RPC 响应已在 call_llm 之前发送（避免 IDE 超时）
            # 完成状态通过 chat/finish 通知传递

            self._current_request_id = None

    async def _handle_chat_stop(self, params: dict, msg_id: Any) -> None:
        """处理 chat/stop 请求 — 取消当前正在进行的 chat/ask。

        ChatService.java 中 stop 方法定义为 @JsonRequest，必须返回响应，
        否则 LSP4J 框架会超时等待。
        """
        # 用户停止生成
        logger.info(
            "[LSP4J] chat/stop: requestId={} cancel_set={}",
            params.get("requestId", ""),
            self._cancel_event.is_set() if self._cancel_event else False,
        )
        if self._cancel_event:
            self._cancel_event.set()
        await self._send_response(msg_id, {})

    # ──────────────────────────────────────────
    # 工具调用处理
    # ──────────────────────────────────────────

    async def _handle_pre_completion(self, params: dict, msg_id: Any) -> None:
        """处理 textDocument/preCompletion — IDE 补全预请求。

        插件 TextDocumentService.java:314 定义为 CompletableFuture<Void>，
        属于 fire-and-forget 模式，返回 null 即可。
        不实现实际补全逻辑，仅消除 -32601 Method not found 错误。
        """
        logger.debug(
            "[LSP4J] preCompletion: requestId={} triggerMode={}",
            params.get("requestId", ""),
            params.get("triggerMode", ""),
        )
        await self._send_response(msg_id, None)

    async def _handle_completion(self, params: dict, msg_id: Any) -> None:
        """处理 textDocument/completion — LSP 标准代码补全。

        Clawith 模式不支持实时代码补全（需要专用模型），返回空列表避免
        IDE 侧每按键都收到 -32601 Method not found 错误。
        """
        await self._send_response(msg_id, {"isIncomplete": False, "items": []})

    async def _handle_tool_invoke(self, params: dict, msg_id: Any) -> None:
        """处理 tool/invoke — 工具调用入口。

        灵码插件通过此方法调用工具（如 add_tasks, todo_write, search_replace）。
        对于纯 UI 工具（add_tasks/todo_write），直接返回成功响应。
        对于 search_replace，降级为 replace_text_by_path 处理。

        ToolInvokeRequest 格式：
        - toolName: 工具名称
        - parameters: 工具参数（dict）
        - requestId: 请求 ID
        - sessionId: 会话 ID
        """
        tool_name = params.get("toolName", "")
        parameters = params.get("parameters", {})
        request_id = params.get("requestId", "")
        session_id = params.get("sessionId", "")

        logger.info("[LSP4J] tool/invoke: tool={} requestId={} sessionId={}", tool_name, request_id, session_id)

        # 特殊工具处理（纯 UI 工具）
        if tool_name in ("add_tasks", "todo_write"):
            # 直接返回成功响应，插件 AddTasksToolDetailPanel 会自动渲染任务树
            result = {
                "success": True,
                "message": f"{tool_name} 工具调用成功",
                "tool_name": tool_name,
                "parameters": parameters,
            }
            await self._send_response(
                msg_id,
                {
                    "requestId": request_id,
                    "errorCode": None,  # 成功时必须为 null，不能是 ""
                    "errorMessage": None,
                    "result": result,
                },
            )
            logger.info("[LSP4J] tool/invoke: {} 纯 UI 工具，返回成功响应", tool_name)
            return

        if tool_name == "update_tasks":
            # 更新任务状态/标题到数据库
            task_id = parameters.get("taskId", "")
            new_status = parameters.get("status")
            new_title = parameters.get("title")
            try:
                from app.models.task import Task as TaskModel

                async with async_session() as _ut_db:
                    _ut_r = await _ut_db.execute(select(TaskModel).where(TaskModel.id == task_id))
                    task = _ut_r.scalar_one_or_none()
                    if task:
                        if new_status:
                            task.status = new_status
                        if new_title:
                            task.title = new_title
                        await _ut_db.commit()
                        result = {"success": True, "taskId": task_id, "status": task.status}
                        logger.info("[LSP4J] tool/invoke: update_tasks success id={} status={}", task_id, task.status)
                    else:
                        result = {"success": False, "error": f"Task not found: {task_id}"}
                        logger.warning("[LSP4J] tool/invoke: update_tasks not found id={}", task_id)
            except Exception as _ute:
                logger.exception("[LSP4J] tool/invoke: update_tasks error")
                result = {"success": False, "error": str(_ute)[:200]}
            await self._send_response(
                msg_id,
                {
                    "requestId": request_id,
                    "errorCode": None,
                    "errorMessage": None,
                    "result": result,
                },
            )
            return

        if tool_name == "search_replace":
            file_path = parameters.get("filePath", "")
            search_text = parameters.get("searchText", "")
            replace_text = parameters.get("replaceText", "")
            logger.info(
                "[LSP4J] tool/invoke: search_replace path={} search_len={} replace_len={}",
                file_path, len(search_text), len(replace_text),
            )
            new_content = await self._fetch_and_replace_file(file_path, search_text, replace_text)
            parameters = {"filePath": file_path, "text": new_content if new_content else replace_text}
            tool_name = "replace_text_by_path"

        # 正常工具调用：通过 invoke_tool_on_ide 发送到 IDE
        try:
            result = await self.invoke_tool_on_ide(tool_name, parameters)
            await self._send_response(
                msg_id,
                {
                    "requestId": request_id,
                    "errorCode": None,  # 成功时必须为 null，不能是 ""
                    "errorMessage": None,
                    "result": result,
                },
            )
            logger.info("[LSP4J] tool/invoke: {} 调用成功", tool_name)
        except Exception as e:
            logger.exception("[LSP4J] tool/invoke: {} 调用失败", tool_name)
            await self._send_response(
                msg_id,
                {
                    "requestId": request_id,
                    "errorCode": "TOOL_INVOKE_FAILED",  # 错误时保留错误码
                    "errorMessage": str(e),
                    "result": None,
                },
            )

    async def _handle_tool_call_approve(self, params: dict, msg_id: Any) -> None:
        """处理 tool/call/approve 请求 — 工具调用审批。

        插件 ToolCallService.java:16 定义为 @JsonRequest("approve")，
        参数 ToolCallApproveRequest：sessionId, requestId, toolCallId, approval(boolean)。
        approval=true 表示用户同意，approval=false 表示用户拒绝。
        无独立 reject 方法 — 拒绝 = approve(approval=false)。
        """
        tool_call_id = params.get("toolCallId", "")
        approved = params.get("approval", True)  # 缺失时默认 true 保持向后兼容

        if not approved:
            # 用户拒绝工具调用 — 取消 pending Future
            logger.info(
                "[LSP4J-TOOL] 工具审批拒绝: toolCallId={} name={}",
                tool_call_id[:8] if tool_call_id else "",
                params.get("name"),
            )
            if tool_call_id:
                future = self._pending_tools.get(str(tool_call_id))
                if future and not future.done():
                    future.set_result("[用户拒绝] 工具调用已被用户拒绝")
                    logger.info("[LSP4J-TOOL] 已取消 pending Future: toolCallId={}", tool_call_id[:8])
        else:
            logger.info(
                "[LSP4J-TOOL] 工具审批通过: toolCallId={} name={}",
                tool_call_id[:8] if tool_call_id else "",
                params.get("name"),
            )

        await self._send_response(msg_id, {})

    async def _handle_tool_invoke_result(self, params: dict, msg_id: Any) -> None:
        """处理 tool/invokeResult 请求 — 异步工具执行结果回传。

        插件通过 ToolService 的 @JsonRequest("invokeResult") 发回结果。
        参数类型为 ToolInvokeResponse：
        - toolCallId: 工具调用 ID
        - name: 工具名称
        - success: 是否成功
        - errorMessage: 错误信息
        - result: 执行结果
        """
        # ★ 入口日志：记录收到的完整 invokeResult 关键信息
        tool_call_id = params.get("toolCallId")
        logger.info(
            "[LSP4J-RESULT] ← 收到 invokeResult: toolCallId={} name={} success={} requestId={} result_type={}",
            str(tool_call_id)[:8] if tool_call_id else "(缺失)",
            params.get("name"),
            params.get("success"),
            str(params.get("requestId", ""))[:8],
            type(params.get("result")).__name__,
        )
        if not tool_call_id:
            if _LSP4J_STRICT_TOOLCALL_ID:
                logger.warning(
                    "[LSP4J-TOOL] invokeResult 缺失 toolCallId（strict 模式拒绝）: requestId={} name={}",
                    str(params.get("requestId", ""))[:8],
                    str(params.get("name", "")),
                )
                await self._send_response(msg_id, {"status": "error", "message": "Missing toolCallId in strict mode"})
                return
            request_id = str(params.get("requestId") or "")
            tool_name = str(params.get("name") or "")
            now = time.time()
            candidates: list[str] = []
            for call_id, future in self._pending_tools.items():
                if future.done():
                    continue
                meta = self._pending_tool_meta.get(call_id, {})
                if request_id and meta.get("request_id") != request_id:
                    continue
                if tool_name and meta.get("tool_name") != tool_name:
                    continue
                created_at = float(meta.get("created_at") or 0.0)
                if created_at and now - created_at > 180:
                    continue
                candidates.append(call_id)

            if len(candidates) == 1:
                tool_call_id = candidates[0]
                logger.warning(
                    "[LSP4J-TOOL] invokeResult toolCallId 缺失，补偿命中: callId={} requestId={} name={}",
                    tool_call_id[:8],
                    request_id[:8],
                    tool_name,
                )
            elif len(candidates) > 1:
                logger.warning(
                    "[LSP4J-TOOL] invokeResult toolCallId 缺失且候选冲突: requestId={} name={} candidates={}",
                    request_id[:8],
                    tool_name,
                    [c[:8] for c in candidates],
                )
                await self._send_response(
                    msg_id, {"status": "error", "message": "Missing toolCallId and ambiguous fallback match"}
                )
                return
            else:
                logger.warning(
                    "[LSP4J-TOOL] invokeResult toolCallId 缺失且无候选: requestId={} name={}",
                    request_id[:8],
                    tool_name,
                )
                await self._send_response(msg_id, {"status": "error", "message": "Missing toolCallId"})
                return

        # 查找 pending Future
        future = self._pending_tools.get(str(tool_call_id))
        if future and not future.done():
            # ★ 正常匹配成功日志
            logger.info(
                "[LSP4J-RESULT] ✅ 正常匹配: toolCallId={} name={} success={} pending_count={}",
                str(tool_call_id)[:8],
                params.get("name"),
                params.get("success", True),
                len(self._pending_tools),
            )
            if params.get("success", True):
                result = params.get("result", {})
                tool_name = params.get("name", "")
                if isinstance(result, dict):
                    # ★ #4 修复：read_file 的 IDE 返回结果是 {"content": "文件内容"}，
                    # 直接提取纯文本，避免 json.dumps 后形成 '{"content": "..."}' 双重嵌套
                    if tool_name == "read_file" and "content" in result:
                        result_str = result["content"]
                    else:
                        result_str = json.dumps(result, ensure_ascii=False)
                else:
                    result_str = str(result)
                future.set_result(result_str)
                logger.info(
                    "[LSP4J-RESULT] ✅ Future 已设置结果: toolCallId={} result_len={}",
                    str(tool_call_id)[:8],
                    len(result_str),
                )
            else:
                error_msg = params.get("errorMessage", "Tool execution failed")
                tool_name = params.get("name", "unknown")
                request_id = str(params.get("requestId", ""))[:8]
                logger.warning(
                    "[LSP4J-RESULT] ❌ 工具失败: toolCallId={} tool={} requestId={} error={}",
                    str(tool_call_id)[:8],
                    tool_name,
                    request_id,
                    error_msg,
                )
                future.set_result(f"[工具错误] {error_msg}")
        elif not future or future.done():
            # ★ 无匹配 Future 详细日志
            logger.warning(
                "[LSP4J-RESULT] ❌ 无匹配 Future: toolCallId={} future_exists={} future_done={} 尝试补偿匹配",
                str(tool_call_id)[:8] if tool_call_id else "",
                future is not None,
                future.done() if future else "N/A",
            )
            # 无匹配 Future（可能已超时，或插件 SearchResultCache 缓存了旧 toolCallId）
            logger.warning(
                "[LSP4J-TOOL] invokeResult: toolCallId={} 无匹配 Future（可能已超时或缓存旧 ID），尝试补偿匹配",
                tool_call_id[:8] if tool_call_id else "",
            )
            request_id = str(params.get("requestId") or "")
            tool_name = str(params.get("name") or "")
            now = time.time()
            candidates: list[str] = []
            for call_id, f in self._pending_tools.items():
                if f.done():
                    continue
                meta = self._pending_tool_meta.get(call_id, {})
                if request_id and meta.get("request_id") != request_id:
                    continue
                if tool_name and meta.get("tool_name") != tool_name:
                    continue
                created_at = float(meta.get("created_at") or 0.0)
                if created_at and now - created_at > 180:
                    continue
                candidates.append(call_id)
            if len(candidates) == 1:
                fallback_id = candidates[0]
                logger.info(
                    "[LSP4J-RESULT] ✅ 补偿命中: staleId={} → realId={} name={} pending_count={}",
                    str(tool_call_id)[:8] if tool_call_id else "",
                    fallback_id[:8],
                    tool_name,
                    len(self._pending_tools),
                )
                f = self._pending_tools[fallback_id]
                if params.get("success", True):
                    result = params.get("result", {})
                    if isinstance(result, dict):
                        # ★ #4 修复：read_file 补偿分支同样需要提取纯文本
                        if tool_name == "read_file" and "content" in result:
                            result_str = result["content"]
                        else:
                            result_str = json.dumps(result, ensure_ascii=False)
                    else:
                        result_str = str(result)
                    f.set_result(result_str)
                    logger.info(
                        "[LSP4J-RESULT] ✅ 补偿 Future 已设置结果: realId={} result_len={}",
                        fallback_id[:8],
                        len(result_str),
                    )
                else:
                    error_msg = params.get("errorMessage", "Tool execution failed")
                    f.set_result(f"[工具错误] {error_msg}")
            elif len(candidates) > 1:
                logger.warning(
                    "[LSP4J-RESULT] ❌ 补偿匹配候选冲突: staleId={} candidates={}",
                    str(tool_call_id)[:8] if tool_call_id else "",
                    [c[:8] for c in candidates],
                )
            else:
                logger.warning(
                    "[LSP4J-RESULT] ❌ 补偿匹配无候选: staleId={} requestId={} name={}",
                    str(tool_call_id)[:8] if tool_call_id else "",
                    request_id[:8],
                    tool_name,
                )

        # tool/invokeResult 是 @JsonRequest，需要返回 OperateCommonResult 格式
        # 插件 ToolService.invokeResult() 期望返回 OperateCommonResult {errorCode, errorMessage}
        # ⚠️ 关键：成功时 errorCode 必须为 null（不是空字符串），否则插件会认为是错误响应
        # 插件源码：if (result.getErrorCode() != null) { log.warn("error response"); }
        await self._send_response(
            msg_id,
            {
                "errorCode": None,  # 成功时必须为 null，不能是 ""
                "errorMessage": None,
            },
        )
        logger.info(
            "[LSP4J-RESULT] ✅ 已返回 invokeResult 响应: toolCallId={} errorCode=null",
            str(tool_call_id)[:8] if tool_call_id else "",
        )

    async def _handle_response(self, msg: dict) -> None:
        """处理 JSON-RPC 响应 — 同步工具执行结果回传。

        当 tool/invoke 的 async=false 时（旧路径，已弃用），工具结果通过 JSON-RPC 响应返回。
        """
        msg_id = msg.get("id")
        if msg_id is None:
            return

        # 检查是否为已超时取消的请求（迟达响应）
        if msg_id in self._cancelled_requests:
            del self._cancelled_requests[msg_id]
            logger.debug("[LSP4J-TOOL] 忽略已超时请求的迟达响应: id={}", msg_id)
            return

        future = self._pending_responses.pop(msg_id, None)
        if future and not future.done():
            if "error" in msg:
                error = msg["error"]
                # 工具响应错误
                logger.warning(
                    "[LSP4J-TOOL] 工具响应错误: id={} code={} msg={}", msg_id, error.get("code"), error.get("message")
                )
                future.set_result(f"[工具错误] {error.get('message', 'Unknown error')}")
            else:
                result = msg.get("result", {})
                # 解析 ToolInvokeResponse 格式
                if isinstance(result, dict):
                    success = result.get("success", True)
                    if success:
                        # 工具执行成功
                        logger.info("[LSP4J-TOOL] 工具执行成功: id={} name={}", msg_id, result.get("name", "unknown"))
                        tool_result = result.get("result", {})
                        if isinstance(tool_result, dict):
                            future.set_result(json.dumps(tool_result, ensure_ascii=False))
                        else:
                            future.set_result(str(tool_result))
                    else:
                        # 工具执行失败
                        logger.warning(
                            "[LSP4J-TOOL] 工具执行失败: id={} name={} error={}",
                            msg_id,
                            result.get("name"),
                            result.get("errorMessage", ""),
                        )
                        future.set_result(f"[工具错误] {result.get('errorMessage', 'Execution failed')}")
                else:
                    future.set_result(str(result))
        else:
            if future is None:
                # 未匹配的响应（可能是 tool/invoke 的 ack 响应，正常情况）
                logger.debug(
                    "[LSP4J-TOOL] 收到未匹配的响应: id={} pending_responses={} pending_tools={}",
                    msg_id,
                    list(self._pending_responses.keys()),
                    list(self._pending_tools.keys())[:3],
                )
            elif future.done():
                # Future 已完成，忽略响应
                logger.debug("[LSP4J-TOOL] Future 已完成/不存在，忽略响应: id={}", msg_id)

    # ──────────────────────────────────────────
    # 工具调用编排（server → client）
    # ──────────────────────────────────────────

    @staticmethod
    def _wrap_results(results: list[dict] | str | None) -> list[dict]:
        """将 results 转换为 LSP 契约要求的 List<Map<String, Object>> 格式。

        LSP 模型 ToolCallSyncResult.results 定义为 List<Map<String, Object>>，
        即数组中每个元素必须是 JSON 对象，不能是裸字符串。
        """
        if not results:
            return []
        if isinstance(results, list):
            # 过滤掉非 dict 元素，确保每个元素都是 Map<String, Object>
            return [r for r in results if isinstance(r, dict)]
        # 字符串结果包装为标准对象
        return [{"content": results[:500]}]

    async def _send_tool_call_sync(
        self,
        session_id: str | None,
        request_id: str,
        tool_call_id: str,
        tool_call_status: str,
        tool_name: str = "",
        parameters: dict | None = None,
        results: list[dict] | str | None = None,
        error_code: str = "",
        error_msg: str = "",
    ) -> None:
        """发送 tool/call/sync 通知（ToolCallSyncResult 格式）。

        插件收到后会在聊天面板渲染工具卡片。
        状态取值参考 ToolCallStatusEnum：
        INIT, PENDING, RUNNING, FINISHED, ERROR, CANCELLED, REQUEST_FINISHED

        注意：results 必须为 List<Map<String, Object>> 格式，
        由 _wrap_results 自动将字符串结果包装为 [{"content": "..."}]。
        """
        wrapped_results = self._wrap_results(results)
        payload = {
            "sessionId": session_id or "",
            "requestId": request_id,
            "projectPath": self._project_path,
            "toolCallId": tool_call_id,
            "toolCallStatus": tool_call_status,
            "name": tool_name,
            "parameters": parameters or {},
            "results": wrapped_results,
            "errorCode": error_code,
            "errorMsg": error_msg,
        }
        await self._send_client_request("tool/call/sync", payload)

        # 记录历史，供 tool/call/results 拉取。
        session_key = session_id or ""
        if session_key:
            bucket = self._tool_call_history_by_session.setdefault(session_key, [])
            bucket.append(payload)
            if len(bucket) > self._tool_call_history_limit:
                del bucket[: len(bucket) - self._tool_call_history_limit]
            # 防止 session 条目无限累积：超过上限时删除最旧的 session
            if len(self._tool_call_history_by_session) > self._MAX_TOOL_CALL_HISTORY_SESSIONS:
                oldest = next(iter(self._tool_call_history_by_session))
                self._tool_call_history_by_session.pop(oldest, None)

            # 持久化终态工具调用到 DB（供后续请求恢复上下文，避免 LLM 重复搜索）
            if tool_call_id and tool_call_status in ("FINISHED", "ERROR", "CANCELLED"):
                _t = asyncio.create_task(
                    _persist_lsp4j_tool_call(
                        agent_id=self._agent_id,
                        user_id=self._user_id,
                        session_id=session_key,
                        tool_name=tool_name,
                        parameters=parameters,
                        results=results,
                        tool_call_id=tool_call_id,
                        request_id=request_id,
                        status=tool_call_status,
                    )
                )
                _lsp4j_background_tasks.add(_t)
                _t.add_done_callback(_lsp4j_background_tasks.discard)

    async def _send_workspace_file_sync(self, ws_file: Any, sync_type: str = "MODIFIED") -> None:
        """发送 workingSpaceFile/sync 通知（WorkspaceFileSyncResult 格式）。

        插件 LanguageClientImpl.syncWorkspaceFile 收到后：
        - type=ADD → WorkspaceFileAddedNotifier（新增文件卡片）
        - type=MODIFIED → WorkspaceFileModifiedNotifier（更新状态/diff/showNewDiff）
        - type=FRUSH → VFS 刷新
        """
        payload = await self._ws_file_service.build_sync_result(ws_file, self._project_path, sync_type)
        logger.info(
            "[WS-FILE] 发送 workingSpaceFile/sync: type={} fileId={} status={} +{} -{}",
            sync_type,
            ws_file.file_id,
            ws_file.status,
            ws_file.diff_info.add,
            ws_file.diff_info.delete,
        )
        await self._send_client_request("workingSpaceFile/sync", payload)

    async def _send_snapshot_sync_all(self, session_id: str, sync_type: str = "ADD") -> None:
        """发送 snapshot/syncAll 通知（SnapshotSyncAllResult 格式）。"""
        payload = await self._ws_file_service.build_snapshot_sync_all(session_id, self._project_path, sync_type)
        logger.info(
            "[WS-FILE] 发送 snapshot/syncAll: type={} snapshot_count={} file_count={}",
            sync_type,
            len(payload["snapshots"]),
            len(payload["workingSpaceFiles"]),
        )
        await self._send_client_request("snapshot/syncAll", payload)

    async def _handle_file_edit_finished(
        self,
        tool_name: str,
        file_path: str,
        arguments: dict,
        tool_call_id: str,
        request_id: str,
    ) -> Any:
        """文件编辑工具完成后：创建工作区文件、计算 diff、设置状态。

        返回 WorkspaceFile 对象供 toolCallSync FINISHED 注入 fileId。
        """
        session_id = self._session_id or ""

        # 获取或创建 snapshot
        snap = await self._ws_file_service.get_or_create_snapshot(session_id, request_id)

        # 确定变更类型
        if tool_name == "create_file_with_text":
            mode = "ADD"
        elif tool_name == "delete_file_by_path":
            mode = "DELETE"
        else:
            mode = "MODIFIED"

        # 创建或更新工作区文件
        ws_file = await self._ws_file_service.create_or_update_file(session_id, snap.id, file_path, mode, tool_call_id)

        # 获取编辑前后内容
        if tool_name == "create_file_with_text":
            last_stable = ""
            full_content = arguments.get("text") or arguments.get("content") or ""
        elif tool_name == "delete_file_by_path":
            last_stable = await self._ws_file_service.get_cached_content(file_path) or ""
            full_content = ""
        else:
            # replace_text_by_path: 全文替换，新内容就是 text 参数
            last_stable = await self._ws_file_service.get_cached_content(file_path) or ""
            full_content = arguments.get("text") or ""

        # 计算 DiffInfo 并存储（大文件 difflib 可能较慢，避免阻塞事件循环）
        await self._ws_file_service.set_content(file_path, last_stable, full_content)

        # 设置状态为 APPLIED
        await self._ws_file_service.update_status(file_path, "APPLIED")

        logger.info(
            "[WS-FILE] 文件编辑完成: tool={} path={} mode={} wsId={} +{} -{}",
            tool_name,
            file_path,
            mode,
            ws_file.id[:8],
            ws_file.diff_info.add,
            ws_file.diff_info.delete,
        )
        return ws_file

    async def invoke_tool_on_ide(self, tool_name: str, arguments: dict, timeout: float = 120.0) -> str:
        """通过 LSP4J 协议调用 IDE 端工具（异步模式）。

        发送 tool/invoke 请求（ToolInvokeRequest 格式）：
        - requestId: 请求 ID
        - toolCallId: 工具调用 ID（用于匹配结果）
        - name: 工具名称（**必须使用插件原生名称**，如 read_file，不是 ide_read_file）
        - parameters: 工具参数（**不是 arguments**）
        - async: true（异步执行，结果通过 tool/invokeResult 回传）

        插件执行完成后通过 tool/invokeResult (@JsonRequest) 回传结果，
        由 _handle_tool_invoke_result 解析并 resolve Future。

        关键设计：必须以请求（带 id）发送，因为插件的 @JsonRequest("invoke")
        只处理带 id 的 JSON-RPC 请求，不处理通知。但结果不通过 JSON-RPC 响应
        返回，而是通过独立的 tool/invokeResult 异步回传。因此：
        - 不将 rpc_id 注册到 _pending_responses（避免 ack 响应误 resolve Future）
        - 只注册 toolCallId 到 _pending_tools（等待 invokeResult 回传真实结果）
        - 插件的 ack 响应会被 _handle_response 静默忽略（无匹配 Future）
        """
        # ★ 从 toolCallId 队列中按序匹配消费（3 元组格式）
        # tool_name 已是插件原生名称（由 tool_hooks 映射），与队列中的 mapped_name 匹配。
        # 匹配成功后提取 original_name（LLM 侧名称），供后续 sync 通知使用。
        tool_call_id = None
        original_name = tool_name  # 默认值（队列未匹配时使用）
        for i, (orig_name, mapped_name, stored_id) in enumerate(self._tool_call_id_queue):
            if mapped_name == tool_name:
                original_name = orig_name
                tool_call_id = stored_id
                self._tool_call_id_queue.pop(i)
                logger.info(
                    "[LSP4J-TOOL] toolCallId 队列匹配: original={} mapped={} callId={} queue_remaining={}",
                    original_name,
                    tool_name,
                    tool_call_id[:8],
                    len(self._tool_call_id_queue),
                )
                break
        queue_matched = bool(tool_call_id)
        if not tool_call_id:
            tool_call_id = str(uuid.uuid4())
            logger.info("[LSP4J-TOOL] toolCallId 队列未匹配，新建兜底: name={} callId={}", tool_name, tool_call_id[:8])
        request_id = self._current_request_id or str(uuid.uuid4())

        logger.info(
            "[LSP4J-TOOL] invoke_tool_on_ide: tool={} callId={} requestId={} timeout={} queue_matched={}",
            tool_name,
            tool_call_id[:8],
            request_id[:8],
            timeout,
            queue_matched,
        )
        _tool_invoke_start = time.monotonic()

        # ★ 本地工具（list_dir, search_file, add_tasks, todo_write）：后端本地执行，不发送到 IDE
        if tool_name in ("list_dir", "search_file", "add_tasks", "todo_write"):
            try:
                if tool_name in ("add_tasks", "todo_write"):
                    # ★ 纯 UI 工具：任务数据在 arguments.tasks 中，需注入 results
                    # 否则 ToolPanel.syncToolCall() 检查 results 为空 → 跳过任务列表渲染
                    raw_tasks = arguments.get("tasks", [])
                    if isinstance(raw_tasks, str):
                        try:
                            raw_tasks = json.loads(raw_tasks)
                        except (json.JSONDecodeError, TypeError):
                            raw_tasks = []
                    if isinstance(raw_tasks, list):
                        results_list = [{"tasks": raw_tasks}]
                    else:
                        results_list = [{"tasks": []}]
                    result_str = json.dumps(
                        {"success": True, "tool_name": tool_name, "tasks": raw_tasks}, ensure_ascii=False
                    )
                else:
                    result_str, results_list = _execute_local_tool(
                        tool_name, arguments, project_path=self._project_path or ""
                    )
                if not queue_matched:
                    # 队列未匹配 → on_tool_call 未被调用 → 没有 markdown_block → 没有 ToolPanel
                    # 发送 FINISHED sync 只会导致消费线程阻塞等待不存在的 panel
                    logger.warning(
                        "[LSP4J-TOOL] 跳过本地工具 FINISHED sync（无匹配 panel）: tool={} callId={}",
                        tool_name,
                        tool_call_id[:8],
                    )
                else:
                    # 插件采用 buffer+replay，后端无需等待 panel 注册
                    await asyncio.sleep(0)
                    await self._send_tool_call_sync(
                        self._session_id,
                        request_id,
                        tool_call_id,
                        "FINISHED",
                        tool_name=original_name,
                        parameters=arguments,
                        results=results_list,
                    )
                logger.info("[LSP4J-TOOL] 本地工具执行完成: tool={} results_count={}", tool_name, len(results_list))
                return result_str
            except Exception as e:
                logger.exception("[LSP4J-TOOL] 本地工具执行失败: tool={}", tool_name)
                if queue_matched:
                    await self._send_tool_call_sync(
                        self._session_id,
                        request_id,
                        tool_call_id,
                        "ERROR",
                        tool_name=original_name,
                        parameters=arguments,
                        error_msg=str(e),
                    )
                return f"[错误] 工具 {tool_name} 执行失败: {e}"

        # ★ 参数名统一转换：LLM 使用 snake_case，插件 ToolHandler 期望 camelCase
        # 按插件 ToolInvokeProcessor 中每个 handler 的取参方法分类：
        #   - replace_text_by_path / create_file_with_text / delete_file_by_path
        #     → handler 调用 getRequestFilePath()，取 filePath (camelCase)
        #   - read_file
        #     → handler 调用 getRequestFilePathWithUnderLine()，取 file_path (snake_case)
        # 因此前 3 个需转换 file_path→filePath，read_file 保留 file_path 不变。
        # 注意：read_file 的 LLM 参数名是 "path"（非 "file_path"），仅它特殊。
        params = dict(arguments)
        # update_tasks：LLM 常输出 task_id 或数字 taskId；插件 UpdateTasksToolHandler 只认 taskId 且曾强制 String 转型。
        # 在此统一写入 arguments（FINISHED sync 用）与 params（tool/invoke 用），避免 invoke 失败导致侧栏也不更新。
        if tool_name == "update_tasks":
            _raw_tid = params.get("taskId")
            if _raw_tid is None:
                _raw_tid = params.get("task_id")
            if _raw_tid is not None:
                _tid_str = str(_raw_tid).strip()
                if _tid_str:
                    params["taskId"] = _tid_str
                    arguments["taskId"] = _tid_str
                    logger.info("[LSP4J-TOOL] update_tasks 参数规范化 taskId={}", _tid_str[:32])
        invoke_tool_name = tool_name
        if tool_name == "search_replace":
            invoke_tool_name = "replace_text_by_path"
            file_path = params.get("filePath") or params.get("file_path") or ""
            search_text = params.get("searchText") or params.get("search_text") or ""
            replace_text = params.get("replaceText") or params.get("replace_text") or ""
            new_content = await self._fetch_and_replace_file(file_path, search_text, replace_text)
            if new_content is not None:
                params["text"] = new_content
            elif "text" not in params:
                params["text"] = replace_text
        if tool_name in _LSP4J_FILE_EDIT_TOOLS:
            if "file_path" in params and "filePath" not in params:
                params["filePath"] = params.pop("file_path")
        elif tool_name == "read_file":
            if "path" in params and "filePath" not in params:
                params["filePath"] = params.pop("path")
        elif tool_name == "run_in_terminal":
            # 插件执行处理器 RunTerminalToolHandlerV2 读取 isBackground（camelCase），
            # 而 UI 展示层使用 is_background（snake_case）。两者都补齐，避免 NPE 与展示不一致。
            if "isBackground" in params and "is_background" not in params:
                params["is_background"] = params["isBackground"]
            elif "is_background" in params and "isBackground" not in params:
                params["isBackground"] = params["is_background"]
            if "isBackground" not in params:
                params["isBackground"] = False
            if "is_background" not in params:
                params["is_background"] = params["isBackground"]
            # 检测只读命令：匹配前缀则标记 is_readonly=True，
            # 插件端可走 ProcessBuilder 快径，免去 invokeAndWait(~400ms) + Ctrl+C(~300ms) 开销
            if "command" in params:
                _cmd = str(params["command"]).strip()
                if any(_cmd.startswith(kw) for kw in _TERMINAL_READONLY_PREFIXES):
                    params["is_readonly"] = True

        # 相对路径解析：将非绝对路径拼接到 IDE 项目根路径
        # ★ 剥离 LLM 常用的 workspace/ 前缀，避免在项目根下创建多余的 workspace/ 目录
        if (tool_name in _LSP4J_FILE_EDIT_TOOLS or tool_name == "read_file") and self._project_path:
            fp = params.get("filePath") or params.get("path") or params.get("file_path")
            if fp and not fp.startswith("/"):
                _stripped = fp
                if _stripped.startswith("workspace/"):
                    _stripped = _stripped[len("workspace/") :]
                resolved = str(Path(self._project_path) / _stripped)
                params["filePath"] = resolved
                logger.info("[LSP4J-TOOL] 相对路径已解析: {} -> {}", fp, resolved)
        trace_key = f"req={request_id[:8]} call={tool_call_id[:8]} logical={original_name} invoke={invoke_tool_name}"
        logger.info("[LSP4J-TRACE] 队列状态 trace={} matched={}", trace_key, queue_matched)

        # ★ 使用 LLM 侧名称发送 RUNNING sync，参数保留原始 snake_case
        # ToolPanel 用 toolName 判断工具类型（如 edit_file 而非 replace_text_by_path），
        # 用 parameters["file_path"] 渲染文件链接。
        sync_parameters = dict(arguments)
        # ToolPanel 链接构建优先取 parameters["file_path"]，补齐避免"查看文件无链接"。
        if tool_name == "read_file":
            fp = sync_parameters.get("file_path") or sync_parameters.get("path") or sync_parameters.get("filePath")
            if fp and "file_path" not in sync_parameters:
                sync_parameters["file_path"] = fp
        elif tool_name == "run_in_terminal":
            bg = sync_parameters.get("is_background")
            if bg is None:
                bg = sync_parameters.get("isBackground")
            if bg is None:
                bg = False
            sync_parameters["is_background"] = bg
            # 插件 RunInTerminalToolContextProvider 从 parameters 读取 run_mode
            # 缺失时日志 warn "run_mode is null"，补齐为 autoRun（允许自动执行）
            if "run_mode" not in sync_parameters:
                sync_parameters["run_mode"] = "autoRun"
            if "run_mode" not in arguments:
                arguments["run_mode"] = "autoRun"

        if queue_matched:
            if tool_name in _UI_HEAVY_SEARCH_TOOLS:
                logger.info(
                    "[LSP4J-TRACE] 跳过 RUNNING（高频搜索降噪）: req={} call={} tool={}",
                    request_id[:8],
                    tool_call_id[:8],
                    tool_name,
                )
            else:
                await self._send_tool_call_sync(
                    self._session_id,
                    request_id,
                    tool_call_id,
                    "RUNNING",
                    tool_name=original_name,
                    parameters=sync_parameters,
                )
        else:
            # 未匹配到 markdown 注册的 ToolPanel 时，禁止把兜底 UUID 发给前端事件流，
            # 否则 ChatToolEventProcessor 会持续等待不存在的 panel 并阻塞后续事件。
            logger.warning(
                "[LSP4J-TOOL] 队列未匹配，跳过 RUNNING sync（避免幽灵事件）: tool={} callId={} requestId={}",
                tool_name,
                tool_call_id[:8],
                request_id[:8],
            )

        # 创建 Future 等待异步结果（通过 tool/invokeResult 回传）
        loop = asyncio.get_running_loop()
        tool_future: asyncio.Future = loop.create_future()
        self._pending_tools[tool_call_id] = tool_future
        self._pending_tool_meta[tool_call_id] = {
            "request_id": request_id,
            "tool_name": tool_name,
            "created_at": time.time(),
        }
        logger.info(
            "[LSP4J-TOOL] Future registered: toolCallId={} pending_count={} waiting for invokeResult",
            tool_call_id[:8],
            len(self._pending_tools),
        )
        logger.info("[LSP4J-TRACE] 调用工具 trace={}", trace_key)

        # 发送 tool/invoke 请求（带 id，触发插件 @JsonRequest("invoke") 处理器）
        # 但不注册到 _pending_responses —— 插件的 ack 响应不是工具结果，
        # 真实结果通过 tool/invokeResult 异步回传
        _t0 = time.monotonic()
        rpc_id = self._next_request_id()
        await self._send_message(
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "method": "tool/invoke",
                "params": {
                    "requestId": request_id,
                    "toolCallId": tool_call_id,
                    "name": invoke_tool_name,  # 实际调用名（search_replace 在此降级为 replace_text_by_path）
                    "parameters": params,  # 已转换参数名（如 path → filePath）
                    "async": True,  # 异步执行，结果通过 invokeResult 回传
                },
            }
        )
        _t1 = time.monotonic()
        logger.info(
            "[LSP4J-TOOL] → 已发送 tool/invoke 到 IDE: rpcId={} tool={} callId={} invokeName={}",
            rpc_id,
            tool_name,
            tool_call_id[:8],
            invoke_tool_name,
        )

        # 等待异步结果（同时监听取消事件，避免 cancel 在 wait_for 期间触发时无法及时响应）
        file_path_for_id = arguments.get("file_path") or arguments.get("filePath")
        try:
            cancel_future = asyncio.ensure_future(self._cancel_event.wait()) if self._cancel_event else None
            try:
                waitables = [tool_future]
                if cancel_future:
                    waitables.append(cancel_future)
                done, _ = await asyncio.wait(waitables, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                _t2 = time.monotonic()
                if cancel_future and cancel_future in done:
                    logger.info(
                        "[LSP4J-TOOL] 工具执行期间检测到取消信号: tool={} callId={}", tool_name, tool_call_id[:8]
                    )
                    if queue_matched:
                        await self._send_tool_call_sync(
                            self._session_id,
                            request_id,
                            tool_call_id,
                            "CANCELLED",
                            tool_name=original_name,
                            parameters=arguments,
                        )
                    self._cancelled_requests[rpc_id] = None
                    return "[已取消] 用户停止了聊天"
                if tool_future not in done:
                    _t2 = time.monotonic()
                    logger.info(
                        "[LSP4J-TIMING] tool={} send={:.0f}ms wait={:.0f}ms total={:.0f}ms trace={} TIMEOUT",
                        tool_name, (_t1 - _t0) * 1000, (_t2 - _t1) * 1000, (_t2 - _t0) * 1000,
                        trace_key,
                    )
                    raise asyncio.TimeoutError()
                result = tool_future.result()
                logger.info(
                    "[LSP4J-TIMING] tool={} send={:.0f}ms wait={:.0f}ms total={:.0f}ms trace={}",
                    tool_name, (_t1 - _t0) * 1000, (_t2 - _t1) * 1000, (_t2 - _t0) * 1000,
                    trace_key,
                )

                # ★ IDE 搜索工具（grep_code/search_codebase/search_symbol）：插件返回
                # {"results": [{fileName, path, ...}]} JSON 字符串，需解析提取内层列表
                # 否则 _wrap_results 会将整个 JSON 串包装为 [{"content": "..."}]
                # 导致插件端 CodeItem 反序列化时拿到 content 而非 fileName → truncateFirst null
                if tool_name in ("grep_code", "search_codebase", "search_symbol") and result:
                    try:
                        parsed = json.loads(result) if isinstance(result, str) else result
                        if isinstance(parsed, dict) and "results" in parsed:
                            results = parsed["results"]
                        else:
                            results = [{"raw": str(result)[:500]}]
                    except (json.JSONDecodeError, TypeError):
                        results = [{"raw": str(result)[:500]}]
                else:
                    results = result[:500] if result else None
                finished_backfilled = False
                if tool_name == "read_file":
                    path = arguments.get("path") or arguments.get("file_path") or arguments.get("filePath") or ""
                    content = result if result else ""
                    results = [{"path": path, "content": content[:500]}]
                    if path and "file_path" not in arguments:
                        arguments["file_path"] = path
                    # ★ 缓存 read_file 内容，供后续编辑工具的 diff 计算使用
                    if path and content:
                        await self._ws_file_service.cache_file_content(path, content)
                    logger.info("[LSP4J-TOOL] read_file FINISHED results 注入 path: path={}", path)
                elif tool_name in _LSP4J_FILE_EDIT_TOOLS and file_path_for_id:
                    # ★ 文件编辑工具：创建工作区文件 + 计算 diff + 发送 workingSpaceFile/sync
                    ws_file = await self._handle_file_edit_finished(
                        tool_name, file_path_for_id, arguments, tool_call_id, request_id
                    )

                    # ★ 注入工作区文件 UUID 作为 fileId（而非文件路径）
                    # ToolPanel.constructFileItem 用 results[0]["fileId"] 设置 FileItem.id，
                    # 后续 workingSpaceFile/sync 的 WorkingSpaceFileInfo.id 需与此一致，
                    # AIDevFilePanel.syncWorkspaceFile 才能通过 id 匹配更新。
                    ws_id = ws_file.id if ws_file else file_path_for_id
                    results = [{"path": file_path_for_id, "fileId": ws_id, "message": result[:500] if result else ""}]
                    if "file_path" not in arguments:
                        arguments["file_path"] = file_path_for_id
                    logger.info(
                        "[LSP4J-TOOL] FINISHED results 注入 path+fileId: path={} wsId={} tool={}",
                        file_path_for_id,
                        ws_id[:8],
                        tool_name,
                    )

                # P1 兜底：文件工具 FINISHED 结果为空/缺 fileId 时，按 file_path 回填一次，避免 ToolPanel 早退。
                if tool_name in _LSP4J_FILE_EDIT_TOOLS and file_path_for_id:
                    first_result = results[0] if isinstance(results, list) and results else {}
                    has_file_id = isinstance(first_result, dict) and bool(first_result.get("fileId"))
                    has_path = isinstance(first_result, dict) and bool(first_result.get("path"))
                    if not (has_file_id and has_path):
                        ws_file_fallback = await self._ws_file_service.get_file(file_path_for_id)
                        fallback_ws_id = ws_file_fallback.id if ws_file_fallback else file_path_for_id
                        results = [
                            {
                                "path": file_path_for_id,
                                "fileId": fallback_ws_id,
                                "message": result[:500] if isinstance(result, str) else "",
                            }
                        ]
                        if "file_path" not in arguments:
                            arguments["file_path"] = file_path_for_id
                        finished_backfilled = True
                        logger.warning(
                            "[LSP4J-TRACE] FINISHED 回填 fileId trace={} path={} wsId={} reason=missing_result_fields",
                            trace_key,
                            file_path_for_id,
                            str(fallback_ws_id)[:8],
                        )
                    else:
                        logger.info(
                            "[LSP4J-TRACE] FINISHED 结果完整 trace={} hasFileId=true hasPath=true",
                            trace_key,
                        )

                if queue_matched:
                    logger.info(
                        "[LSP4J-TOOL] ✅ 准备发送 FINISHED sync: tool={} callId={} results_count={}",
                        tool_name,
                        tool_call_id[:8],
                        len(results) if results else 0,
                    )
                    await self._send_tool_call_sync(
                        self._session_id,
                        request_id,
                        tool_call_id,
                        "FINISHED",
                        tool_name=original_name,
                        parameters=arguments,
                        results=results,
                    )
                    logger.info(
                        "[LSP4J-TRACE] FINISHED 已发送 trace={} backfilled={} results={}",
                        trace_key,
                        finished_backfilled,
                        json.dumps(results, ensure_ascii=False)[:200] if results else "None",
                    )
                else:
                    logger.warning(
                        "[LSP4J-TOOL] ❌ 队列未匹配，跳过 FINISHED sync（避免幽灵事件）: tool={} callId={} requestId={} queue_size={}",
                        tool_name,
                        tool_call_id[:8],
                        request_id[:8],
                        len(self._tool_call_id_queue),
                    )

                # ★ 文件编辑工具完成后发送 workingSpaceFile/sync 通知（APPLIED 状态）
                # 必须在 toolCallSync FINISHED 之后发送，确保 ToolPanel 已创建 AIDevFilePanel
                if tool_name in _LSP4J_FILE_EDIT_TOOLS and file_path_for_id:
                    logger.info(
                        "[WS-FILE] 🔍 检查文件编辑后 sync: tool={} file_path_for_id={}",
                        tool_name,
                        file_path_for_id,
                    )
                    ws_file = await self._ws_file_service.get_file(file_path_for_id)
                    if ws_file:
                        logger.info(
                            "[WS-FILE] ✅ ws_file 找到: path={} wsId={} mode={} status={} +{} -{}",
                            file_path_for_id,
                            ws_file.id[:8],
                            ws_file.mode,
                            ws_file.status,
                            ws_file.diff_info.add,
                            ws_file.diff_info.delete,
                        )
                        await asyncio.sleep(0.2)
                        # 插件双通道路由差异：
                        # 1) WorkingSpacePanel 只通过 ADD notifier 把文件加入"变更文件"列表；
                        # 2) ToolPanel 内 FileItemPanel 通过 MODIFIED notifier 更新状态（GENERATING→APPLIED）。
                        # 为保证"列表完整 + 卡片状态正确"，文件编辑工具统一双发 ADD + MODIFIED。
                        await self._send_workspace_file_sync(ws_file, "ADD")
                        await self._send_workspace_file_sync(ws_file, "MODIFIED")
                        logger.info("[LSP4J-TRACE] WS 双发已完成 trace={} sent=ADD,MODIFIED", trace_key)
                        if self._session_id:
                            try:
                                await self._send_snapshot_sync_all(self._session_id)
                            except Exception as e:
                                logger.warning("[WS-FILE] 工具编辑后 snapshot/syncAll 失败（非致命）: {}", e)
                    else:
                        logger.warning(
                            "[WS-FILE] ❌ ws_file 未找到，无法发送 sync: file_path_for_id={}",
                            file_path_for_id,
                        )

                _tool_invoke_elapsed = time.monotonic() - _tool_invoke_start
                logger.info(
                    "[LSP4J-PERF] TOOL_SUCCESS tool={} callId={} elapsed={:.1f}s result_len={}",
                    tool_name,
                    tool_call_id[:8],
                    _tool_invoke_elapsed,
                    len(result) if result else 0,
                )
                return result
            finally:
                if cancel_future:
                    cancel_future.cancel()
        except asyncio.TimeoutError:
            _tool_invoke_elapsed = time.monotonic() - _tool_invoke_start
            logger.warning(
                "[LSP4J-PERF] TOOL_TIMEOUT tool={} callId={} elapsed={:.1f}s timeout={}",
                tool_name,
                tool_call_id[:8],
                _tool_invoke_elapsed,
                timeout,
            )
            self._cancelled_requests[rpc_id] = None
            if len(self._cancelled_requests) > self._MAX_CANCELLED_REQUESTS_SIZE:
                self._cancelled_requests.pop(next(iter(self._cancelled_requests)))
            if queue_matched:
                await self._send_tool_call_sync(
                    self._session_id,
                    request_id,
                    tool_call_id,
                    "ERROR",
                    tool_name=original_name,
                    parameters=arguments,
                    error_msg=f"工具 {tool_name} 执行超时（{timeout}s）",
                )
                await self._send_client_request(
                    "chat/filterTimeout",
                    {
                        "requestId": request_id,
                        "sessionId": self._session_id or "",
                        "statusCode": 408,
                    },
                )
            else:
                logger.warning(
                    "[LSP4J-TOOL] 队列未匹配，跳过 ERROR sync（避免幽灵事件）: tool={} callId={} requestId={}",
                    tool_name,
                    tool_call_id[:8],
                    request_id[:8],
                )
            # 文件编辑超时也更新状态
            if tool_name in _LSP4J_FILE_EDIT_TOOLS and file_path_for_id:
                ws_file = await self._ws_file_service.update_status(
                    file_path_for_id, "APPLYING_FAILED", f"超时（{timeout}s）"
                )
                if ws_file:
                    await self._send_workspace_file_sync(ws_file, "MODIFIED")
            return f"[超时] 工具 {tool_name} 执行超时（{timeout}s）"
        finally:
            # ★ 不立即清理 Future，保留 60s 迟到窗口
            # 插件端可能因 SearchResultCache 或 terminal 上报链路延迟，
            # 在超时后几秒～几十秒才发回 invokeResult。
            # 保留期间 invokeResult 可通过补偿匹配命中，完成 UI 更新。
            existing_future = self._pending_tools.get(tool_call_id)
            if existing_future and existing_future.done():
                # wait_for 超时取消了 Future → 替换为新 Future 供迟到结果匹配
                loop = asyncio.get_running_loop()
                late_future: asyncio.Future = loop.create_future()
                self._pending_tools[tool_call_id] = late_future

                # 保留 meta 供补偿匹配使用（requestId + tool_name）
                # 60s 后清理
                async def _cleanup_late_window(cid: str):
                    await asyncio.sleep(60.0)
                    self._pending_tools.pop(cid, None)
                    self._pending_tool_meta.pop(cid, None)

                _t = asyncio.ensure_future(_cleanup_late_window(tool_call_id))
                _lsp4j_background_tasks.add(_t)
                _t.add_done_callback(_lsp4j_background_tasks.discard)
            else:
                self._pending_tools.pop(tool_call_id, None)
                self._pending_tool_meta.pop(tool_call_id, None)

    # ──────────────────────────────────────────
    # 消息发送方法（严格匹配插件协议字段）
    # ──────────────────────────────────────────

    @staticmethod
    def _convert_file_paths_to_links(text: str) -> str:
        """将文本中的文件路径转换为可点击的 Markdown 链接格式。

        修复问题：灵码插件的 detectFileUrl 方法只有在存在 @workspace 标签
        时才会将纯文本文件路径转换为可点击链接。我们在后端主动转换，
        确保文件路径在任何情况下都可点击跳转。

        格式：[`/path/to/file.java`](file:///path/to/file.java)

        支持的路径格式：
        - 绝对路径：/path/to/file.py, C:\\path\\to\\file.java
        - 带行号：file.py:123, file.py#L12, file.py#L12-L20
        - 注意：已在 Markdown 链接或反引号内的路径不重复转换
        """
        if not text:
            return text

        import re

        # ★ 安全策略：先保护所有已存在的 Markdown 结构，再处理纯文本
        # 1. 提取并保护 Markdown 代码块：```...``` → 占位符（防止 toolCall 等块被误处理）
        codeblock_placeholder_prefix = "__LSP4J_CODEBLOCK_PLACEHOLDER__"
        codeblock_placeholders: list[str] = []

        def protect_codeblock(m: re.Match) -> str:
            codeblock_placeholders.append(m.group(0))
            return f"{codeblock_placeholder_prefix}{len(codeblock_placeholders) - 1}__"

        # 保护代码块：匹配 ```language\ncontent``` 或 ```toolCall::name::id::status\n```
        # 使用非贪婪匹配，确保每个代码块独立匹配
        text = re.sub(r"```[^`]*```", protect_codeblock, text, flags=re.DOTALL)

        # 2. 提取并保护 Markdown 表格：防止表格单元格中的路径被转换
        table_placeholder_prefix = "__LSP4J_TABLE_PLACEHOLDER__"
        table_placeholders: list[str] = []

        def protect_table(m: re.Match) -> str:
            table_placeholders.append(m.group(0))
            return f"{table_placeholder_prefix}{len(table_placeholders) - 1}__"

        # 保护 Markdown 表格：匹配包含 | 的多行文本块（至少 2 行，其中一行包含 |---|）
        text = re.sub(
            r"((?:^|\n)(?:\|[^\n\|]+\|[^\n]*\n)+(?:\|\s*:?-+:?\s*\|[^\n]*\n)(?:\|[^\n\|]+\|[^\n]*\n)*)",
            protect_table,
            text,
        )

        # 3. 提取并保护 Markdown 链接：[text](url) → 占位符
        placeholder_prefix = "__LSP4J_LINK_PLACEHOLDER__"
        link_placeholders: list[str] = []

        def protect_link(m: re.Match) -> str:
            link_placeholders.append(m.group(0))
            return f"{placeholder_prefix}{len(link_placeholders) - 1}__"

        # 保护 Markdown 链接：[...](...)
        text = re.sub(r"\[[^\]]*\]\([^)]+\)", protect_link, text)

        # 4. 提取并保护反引号代码内容：`code` → 占位符
        inline_code_placeholders: list[str] = []

        def protect_inline_code(m: re.Match) -> str:
            inline_code_placeholders.append(m.group(0))
            return f"__LSP4J_INLINECODE_{len(inline_code_placeholders) - 1}__"

        text = re.sub(r"`[^`]+`", protect_inline_code, text)

        # 4. 现在可以安全地转换纯文本中的文件路径了
        path_pattern = re.compile(
            r"("
            r"/(?:[a-zA-Z0-9_\-./]+[a-zA-Z0-9_\-/]|bin|etc|usr|home|Users|tmp|var|opt)[a-zA-Z0-9_\-./]*"
            r"|[a-zA-Z]:[/\\\\][a-zA-Z0-9_\-./\\\\]+"
            r")"
            r"(?::(\d+)|#L(\d+)(?:-L(\d+))?)?"
        )

        def replace_path(match: re.Match) -> str:
            full_path = match.group(1)
            line_start = match.group(2) or match.group(3)
            line_end = match.group(4)

            file_url = f"file://{full_path}"
            display_text = full_path

            if line_start:
                if line_end:
                    file_url += f"#L{line_start}-L{line_end}"
                    display_text += f":{line_start}-{line_end}"
                else:
                    file_url += f"#L{line_start}"
                    display_text += f":{line_start}"

            return f"[`{display_text}`]({file_url})"

        try:
            text = path_pattern.sub(replace_path, text)
        except Exception as e:
            logger.debug("[LSP4J] 文件路径转换失败，使用原文: {}", e)
            return text

        # 5. 还原内联代码
        for i, code in enumerate(inline_code_placeholders):
            text = text.replace(f"__LSP4J_INLINECODE_{i}__", code)

        # 6. 还原表格（在链接还原之前，避免表格内的链接占位符被误处理）
        for i, table in enumerate(table_placeholders):
            text = text.replace(f"{table_placeholder_prefix}{i}__", table)

        # 7. 还原链接
        for i, link in enumerate(link_placeholders):
            text = text.replace(f"{placeholder_prefix}{i}__", link)

        # 8. 还原代码块（最后还原，确保代码块内的所有内容都不被修改）
        for i, codeblock in enumerate(codeblock_placeholders):
            text = text.replace(f"{codeblock_placeholder_prefix}{i}__", codeblock)

        return text

    @staticmethod
    def _fix_code_edit_block_format(text: str) -> str:
        """修复 CODE_EDIT_BLOCK 格式异常（流式断裂、换行问题）。

        修复场景（基于灵码插件 MarkdownStreamPanel.java:301-331 源码）：
        1. 语言标识和 |CODE_EDIT_BLOCK| 分成两行
           输入:  ```python\n|CODE_EDIT_BLOCK|/path\n...
           输出:  ```python|CODE_EDIT_BLOCK|/path\n...
        2. （未来扩展）流式输出中 |CODE_EDIT_BLOCK| 前后断裂

        为什么需要：
        - 插件正则要求 group(10).split("|") 长度 >= 3，且第二个元素是 CODE_EDIT_BLOCK
        - 流式输出时语言和 |CODE_EDIT_BLOCK| 可能分到不同 chunk，导致解析失败
        - 本地弱模型可能理解偏差，把 |CODE_EDIT_BLOCK| 放到第二行
        - 格式异常会导致 Apply 按钮消失，仅渲染普通代码块

        返回：修复后的文本
        """
        if not text:
            return text

        original = text

        # 场景 1: 语言和 |CODE_EDIT_BLOCK| 换行
        # 匹配: ```语言\n|CODE_EDIT_BLOCK|... → 替换为 ```语言|CODE_EDIT_BLOCK|...
        # 正则说明: 捕获 ``` 后的语言标识（[a-zA-Z0-9_-]+），然后是换行 + |CODE_EDIT_BLOCK|
        text = re.sub(r"```([a-zA-Z0-9_-]+)\n\|CODE_EDIT_BLOCK\|", r"```\1|CODE_EDIT_BLOCK|", text)

        # 日志：如果发生了修复，记录差异
        if text != original:
            logger.debug("[LSP4J] CODE_EDIT_BLOCK 格式已修复\n  修复前: {}\n  修复后: {}", original[:100], text[:100])

        return text

    async def _send_chat_answer(
        self,
        session_id: str | None,
        text: str,
        request_id: str,
        *,
        overwrite: bool = False,
    ) -> None:
        """发送 chat/answer（ChatAnswerParams 格式）。

        ⚠️ 字段名必须严格匹配：
        - text（不是 content）
        - requestId（必须携带）
        - overwrite: 流式追加为 False；与流式正文不一致时用 True 覆盖（见 finish-only 补发）
        - timestamp（毫秒时间戳）
        - extra: Map<String,String>（含 sessionType）
        """
        # ★ 临时调试：记录 toolCall markdown 块的详细内容
        if "toolCall::" in text:
            logger.info("[LSP4J-DEBUG] chat/answer TOOLCALL text={!r} len={}", text, len(text))

        # 构建 extra 字段（ChatAnswerParams.extra 类型为 Map<String, String>）
        extra: dict[str, str] = {}
        session_type = getattr(self, "_current_session_type", "")
        if session_type:
            extra["sessionType"] = session_type

        # chat/answer 发送追踪（INFO：排障时须在 backend.log 可见）
        logger.info(
            "[LSP4J-UI] chat/answer_build requestId={} raw_len={} overwrite={}", request_id, len(text), overwrite
        )

        # ★ 修复 1：CODE_EDIT_BLOCK 格式自动修复
        # 流式输出时语言标识和 |CODE_EDIT_BLOCK| 可能分到不同 chunk，导致 Apply 按钮消失
        fixed_text = self._fix_code_edit_block_format(text)

        # ★ 修复 2：将文件路径转换为可点击的 Markdown 链接
        # 注意：流式模式下每个 chunk 单独处理，_convert_file_paths_to_links 设计用于完整文本
        # 表格等多行结构在流式 chunk 中会被错误处理，因此仅在非流式或 finish 时转换
        # 流式输出的文件路径转换由 chat/finish 的 fullAnswer 统一处理（用于历史记录）
        is_streaming = getattr(self, "_stream_mode", True)
        if is_streaming:
            converted_text = fixed_text
        else:
            converted_text = self._convert_file_paths_to_links(fixed_text)

        await self._send_client_request(
            "chat/answer",
            {
                "requestId": request_id,
                "sessionId": session_id or "",
                "text": converted_text,
                "overwrite": overwrite,
                "isFiltered": False,
                "timestamp": int(time.time() * 1000),
                "extra": extra,
            },
        )

    async def _send_chat_think(self, session_id: str | None, text: str, step: str, request_id: str) -> None:
        """发送 chat/think（ChatThinkingParams 格式）。

        ⚠️ 字段名必须严格匹配：
        - text（不是 content）
        - step: "start" 或 "done"（不是 thinking: true）
        - requestId（必须携带）
        - timestamp（毫秒时间戳）
        - extra: Map<String,String>（含 sessionType）
        """
        extra: dict[str, str] = {}
        session_type = getattr(self, "_current_session_type", "")
        if session_type:
            extra["sessionType"] = session_type
        await self._send_client_request(
            "chat/think",
            {
                "requestId": request_id,
                "sessionId": session_id or "",
                "text": text,
                "step": step,  # "start" 或 "done"
                "timestamp": int(time.time() * 1000),
                "extra": extra,
            },
        )

    async def _send_chat_finish(
        self,
        session_id: str | None,
        reason: str,
        full_answer: str,
        request_id: str,
        status_code: int = 200,
    ) -> None:
        """发送 chat/finish（ChatFinishParams 格式）。

        ⚠️ 字段名必须严格匹配：
        - requestId（必须携带）
        - statusCode: 200（成功），408（超时），500（服务端错误）
          灵码插件 BaseChatPanel.stopGenerate() 检查 statusCode == 200 判断成功，
          其他值进入对应错误分支。绝对不能用 0 表示成功。
        - fullAnswer（完整回答文本）
        """
        logger.info("[LSP4J] chat/finish: requestId={} statusCode={} reason={}", request_id, status_code, reason)
        # ★ 修复 1：fullAnswer 也需要修复 CODE_EDIT_BLOCK 格式（历史记录显示时 Apply 按钮可用）
        fixed_full_answer = self._fix_code_edit_block_format(full_answer)
        # ★ 修复 2：fullAnswer 也需要转换文件路径（历史记录显示时可点击）
        converted_full_answer = self._convert_file_paths_to_links(fixed_full_answer)

        # ★ 修复 3：ChatFinishParams 有 extra 字段（Map<String, Object>）
        # 虽然插件不强制要求，但保持一致性更好
        extra = {}
        session_type = getattr(self, "_current_session_type", "")
        if session_type:
            extra["sessionType"] = session_type

        await self._send_client_request(
            "chat/finish",
            {
                "requestId": request_id,
                "sessionId": session_id or "",
                "reason": reason,
                "statusCode": status_code,
                "fullAnswer": converted_full_answer,
                "extra": extra if extra else None,  # 空 dict 发 null 避免无意义数据
            },
        )

    async def _send_session_title_update(self, session_id: str | None, title: str) -> None:
        """发送 session/title/update 通知（SessionTitleRequest 格式）。

        参数：sessionId, sessionTitle
        """
        await self._send_client_request(
            "session/title/update",
            {
                "sessionId": session_id or "",
                "sessionTitle": title,
            },
        )

    async def _send_process_step_callback(
        self,
        session_id: str | None,
        request_id: str,
        step: str,
        description: str,
        status: str,
        result: Any = None,
        message: str = "",
    ) -> None:
        """发送 chat/process_step_callback 通知（ChatProcessStepCallbackParams 格式）。

        插件收到后会在聊天面板渲染步骤进度列表。
        status 取值：doing / done / error / manual_confirm
        step 取值参考 ChatStepEnum：step_start, step_end, step_refine_query 等
        """
        # 高频步骤节流：同一 request 短时间内重复相同步骤签名时跳过
        now = time.monotonic()
        signature = (step, status, description)
        last_emit = self._process_step_last_emit.get(request_id)
        if last_emit:
            last_sig = last_emit.get("sig")
            last_ts = float(last_emit.get("ts") or 0.0)
            if last_sig == signature and (now - last_ts) < _PROCESS_STEP_THROTTLE_WINDOW_SEC:
                logger.info(
                    "[LSP4J-TRACE] process_step 节流跳过: req={} step={} status={} delta={:.3f}s",
                    request_id[:8],
                    step,
                    status,
                    now - last_ts,
                )
                return
        self._process_step_last_emit[request_id] = {"sig": signature, "ts": now}

        # 步骤推送日志
        logger.info(
            "[LSP4J] process_step_callback: requestId={} step={} status={} desc={}",
            request_id,
            step,
            status,
            description[:80],
        )
        await self._send_client_request(
            "chat/process_step_callback",
            {
                "requestId": request_id,
                "sessionId": session_id or "",
                "step": step,
                "description": description,
                "status": status,
                "result": result,
                "message": message,
            },
        )

    async def _send_message(self, message: dict[str, Any]) -> None:
        """发送 LSP Base Protocol 格式的消息到 WebSocket。"""
        # 连接已断开或不活跃，不再发送（_ws_connected 用于快速判断，_closed 用于兜底）
        if not self._ws_connected or self._closed:
            return
        try:
            # 协议追踪日志：记录发出的每条响应/通知/请求
            _method = message.get("method", "")
            if _method:
                _pkeys = list(message.get("params", {}).keys()) if isinstance(message.get("params"), dict) else []
                _params = message.get("params", {})
                # ★ 临时调试：记录 chat/answer 的详细内容
                if _method == "chat/answer" and isinstance(_params, dict):
                    _text = _params.get("text", "")
                    # 记录 toolCall markdown 块
                    if "toolCall::" in _text:
                        logger.info("[LSP4J →] method={} id={} TOOLCALL_BLOCK={!r}", _method, message.get("id"), _text)
                    # 记录包含工具名称的文本（可能是 LLM 回复中的纯文本）
                    elif any(tool in _text for tool in ["replace_text_by_path", "read_file", "list_files"]):
                        logger.info("[LSP4J →] method={} id={} TOOL_TEXT={!r}", _method, message.get("id"), _text[:200])
                    # 记录表格内容
                    elif _text and "|" in _text:
                        logger.info("[LSP4J →] method={} id={} text={!r}", _method, message.get("id"), _text)
                    else:
                        logger.info(
                            "[LSP4J →] method={} id={} text_len={} overwrite={} preview={!r}",
                            _method,
                            message.get("id"),
                            len(_text),
                            _params.get("overwrite", False),
                            (_text[:160].replace("\n", "↵") if _text else ""),
                        )
                else:
                    logger.info("[LSP4J →] method={} id={} params_keys={}", _method, message.get("id"), _pkeys)
            elif "result" in message:
                logger.debug(
                    "[LSP4J →] response id={} result_type={}", message.get("id"), type(message["result"]).__name__
                )
            elif "error" in message:
                logger.warning(
                    "[LSP4J →] error id={} code={} msg={}",
                    message.get("id"),
                    message.get("error", {}).get("code"),
                    message.get("error", {}).get("message"),
                )

            frame = LSPBaseProtocolParser.format_message(message)
            await self._ws.send_text(frame)
        except Exception as e:
            # WebSocket 发送失败意味着连接已不可用，标记关闭防止后续调用继续失败
            _method = message.get("method", "unknown")
            _id = message.get("id", "none")
            logger.warning("LSP4J: WebSocket 发送失败, method={} id={} error={}", _method, _id, e)
            self._closed = True
            self._ws_connected = False

    async def _send_jsonrpc_error_best_effort(self, msg_id: Any, code: int, message: str) -> None:
        """在 _closed 等状态下仍尝试发送 JSON-RPC error，避免客户端 RPC 永久挂起。

        普通 _send_message 在 self._closed 时直接 return，无法解开已结束的会话上
        新到的 chat/ask 等 @JsonRequest。此处仅检查 _ws_connected，尽力写出一帧。
        """
        if msg_id is None or not self._ws_connected:
            return
        try:
            frame = LSPBaseProtocolParser.format_message(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": code, "message": message},
                }
            )
            await self._ws.send_text(frame)
            logger.info("[LSP4J →] best-effort error id={} code={}", msg_id, code)
        except Exception as e:
            logger.warning("LSP4J: best-effort JSON-RPC error send failed id={} err={}", msg_id, e)
            self._closed = True
            self._ws_connected = False

    async def _send_response(self, msg_id: Any, result: Any) -> None:
        """发送 JSON-RPC 成功响应。"""
        await self._send_message(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": result,
            }
        )

    async def _send_error_response(self, msg_id: Any, code: int, message: str) -> None:
        """发送 JSON-RPC 错误响应。"""
        await self._send_message(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": code, "message": message},
            }
        )

    async def _send_request(self, method: str, params: dict, request_id: int) -> None:
        """发送 JSON-RPC 请求（server → client）。"""
        await self._send_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )

    async def _send_notification(self, method: str, params: dict) -> None:
        """发送 JSON-RPC 通知（无 id，不期望响应）。

        仅用于插件 LanguageClient.java 中标注为 @JsonNotification 的方法
        （如 image/uploadResultNotification）。
        对于 @JsonRequest 方法（chat/answer, chat/think, chat/finish 等），
        必须使用 _send_client_request 以确保 LSP4J 正确分发。
        """
        await self._send_message(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
            }
        )

    async def _send_client_request(self, method: str, params: dict) -> None:
        """发送 JSON-RPC 请求到客户端（带 id，但不等待响应）。

        用于插件 LanguageClient.java 中标注为 @JsonRequest 的方法：
        chat/answer, chat/think, chat/finish, chat/process_step_callback,
        session/title/update, tool/call/sync, commitMsg/answer, commitMsg/finish,
        chat/codeChange/apply/finish。

        LSP4J 框架要求：@JsonRequest 处理器只能被带 id 的 RequestMessage 触发，
        不带 id 的 NotificationMessage 会被静默忽略。
        因此必须发送带 id 的请求，但不需要注册 pending_response Future
        （插件的响应会被 _handle_response 静默处理）。
        """
        request_id = self._next_request_id()
        await self._send_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )

    # ──────────────────────────────────────────
    # 辅助方法
    # ──────────────────────────────────────────

    def _next_request_id(self) -> int:
        """生成下一个 JSON-RPC 请求 ID。"""
        self._request_id_counter += 1
        return self._request_id_counter

    async def cleanup(self) -> None:
        """连接断开时清理资源。

        必须包含以下步骤：
        1. 标记连接已关闭
        2. resolve 所有 pending tool Futures（防止协程挂起）
        3. resolve 所有 pending response Futures
        """
        self._closed = True
        self._ws_connected = False  # 主动标记连接已断开，阻止后续 _send_message 调用
        tool_count = len(self._pending_tools)
        resp_count = len(self._pending_responses)
        # 连接断开清理
        logger.info("[LSP4J-LIFE] 连接断开清理: pending_tools={} pending_responses={}", tool_count, resp_count)
        # 清理 pending tool Futures
        for call_id, future in list(self._pending_tools.items()):
            if not future.done():
                future.set_result("[连接断开] 工具调用未完成")
        self._pending_tools.clear()
        self._pending_tool_meta.clear()

        # 清理 pending response Futures
        for req_id, future in list(self._pending_responses.items()):
            if not future.done():
                future.set_result("[连接断开] 请求未完成")
        self._pending_responses.clear()

        # 设置 cancel 事件以中断正在执行的 call_llm
        if self._cancel_event:
            self._cancel_event.set()

        # 清理图片缓存
        self._image_cache.clear()
        self._tool_call_history_by_session.clear()

        # 清理已取消请求记录
        self._cancelled_requests.clear()

    # ──────────────────────────────────────────
    # ── search_replace 公共逻辑 ─────────────────
    # _handle_tool_invoke 和 invoke_tool_on_ide 都用此方法，消除重复。

    async def _fetch_and_replace_file(
        self, file_path: str, search_text: str, replace_text: str
    ) -> str | None:
        """读取文件内容并执行搜索替换（公共方法）。

        优先从缓存读取文件内容，缓存未命中时嵌套调用 read_file。
        找到 search_text 则执行替换并返回新内容，否则返回 None（调用方降级为全文替换）。
        """
        current_content = await self._ws_file_service.get_cached_content(file_path) if file_path else None
        if current_content is None and file_path:
            try:
                current_content = await asyncio.wait_for(
                    self.invoke_tool_on_ide("read_file", {"filePath": file_path}),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                logger.warning("[LSP4J] search_replace: read_file 超时 {}", file_path)
                current_content = None
            except Exception:
                logger.warning("[LSP4J] search_replace: 无法读取文件 {}", file_path)
                current_content = None
        if current_content and search_text and search_text in current_content:
            new_content = current_content.replace(search_text, replace_text)
            logger.info(
                "[LSP4J] search_replace: 替换成功 path={} occurrences={}",
                file_path,
                current_content.count(search_text),
            )
            return new_content
        if current_content and search_text:
            logger.warning(
                "[LSP4J] search_replace: 搜索文本未找到 path={} search_preview={}",
                file_path,
                search_text[:80],
            )
        return None

    # 存根与扩展方法
    # ──────────────────────────────────────────

    async def _handle_inline_edit(self, params: dict, msg_id: Any) -> None:
        """处理 textDocument/inlineEdit — 行内编辑建议。

        插件 TextDocumentService.java:308 定义为 CompletableFuture<InlineEditResult>，
        InlineEditResult 格式：{success: boolean, message: String}。
        当前不实现行内编辑功能，返回 success=false 让插件静默跳过。
        """
        logger.debug("[LSP4J] inlineEdit: uri={}", params.get("textDocument", {}).get("uri", ""))
        await self._send_response(msg_id, {"success": False, "message": ""})

    async def _handle_edit_predict(self, params: dict, msg_id: Any) -> None:
        """处理 textDocument/editPredict — 编辑预测。

        插件 TextDocumentService.java:318 定义为 CompletableFuture<Void>，
        属于 fire-and-forget 模式，返回 null 即可。
        当前不实现编辑预测功能。
        """
        logger.debug("[LSP4J] editPredict: uri={}", params.get("textDocument", {}).get("uri", ""))
        await self._send_response(msg_id, None)

    async def _handle_stub(self, params: dict, msg_id: Any) -> None:
        """通用存根处理器 — 返回空成功响应，避免 Method not found 错误。"""
        logger.debug("[LSP4J-STUB] method not implemented, returning empty response")
        await self._send_response(msg_id, {})

    @staticmethod
    def _epoch_ms(dt: datetime | None) -> int:
        if dt is None:
            return 0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz_utc)
        return int(dt.timestamp() * 1000)

    async def _handle_snapshot_list_by_session(self, params: dict, msg_id: Any) -> None:
        """snapshot/listBySession → ListSnapshotsResult（与 SnapshotService.java 一致）。"""
        session_id = params.get("sessionId") or ""
        snaps = await self._ws_file_service.list_snapshots_for_session(session_id)
        await self._send_response(
            msg_id,
            {
                "snapshots": [s.to_dict() for s in snaps],
                "errorCode": None,
                "errorMessage": None,
            },
        )
        logger.debug(
            "[LSP4J] snapshot/listBySession: sessionId={} count={}", session_id[:16] if session_id else "", len(snaps)
        )

    async def _handle_snapshot_operate(self, params: dict, msg_id: Any) -> None:
        """snapshot/operate → OperateCommonResult；在内存中更新工作区文件状态。"""
        snap_id = params.get("id") or ""
        op_type = (params.get("opType") or "").upper()
        inner = params.get("params") or {}
        target_snap = inner.get("targetSnapshotId") if isinstance(inner, dict) else None
        ws_items = params.get("workingSpaceFiles") or []

        err: str | None = None
        try:
            if op_type in ("ACCEPT_ALL",):
                await self._ws_file_service.apply_snapshot_accept_all(snap_id)
            elif op_type in ("REJECT_ALL",):
                await self._ws_file_service.apply_snapshot_reject_all(snap_id)
            elif op_type in ("SWITCH", "ACTIVATE") and target_snap:
                if not await self._ws_file_service.set_current_snapshot(target_snap):
                    err = "snapshot_not_found"
            elif op_type in ("ACCEPT", "APPLY"):
                for item in ws_items:
                    if not isinstance(item, dict):
                        continue
                    wid = item.get("id") or ""
                    if wid:
                        await self._ws_file_service.operate(wid, "ACCEPT", item.get("content"))
            elif op_type in ("REJECT",):
                for item in ws_items:
                    if not isinstance(item, dict):
                        continue
                    wid = item.get("id") or ""
                    if wid:
                        await self._ws_file_service.operate(wid, "REJECT", None)
            elif op_type in ("CANCEL", "UPDATE_CHAT_RECORD"):
                pass
            else:
                logger.debug("[LSP4J] snapshot/operate noop opType={} id={}", op_type, snap_id[:8] if snap_id else "")

            sess_for_sync = ""
            if target_snap and op_type in ("SWITCH", "ACTIVATE"):
                sinfo = await self._ws_file_service.find_snapshot_by_id(target_snap)
                sess_for_sync = sinfo.session_id if sinfo else ""
            elif snap_id:
                sinfo = await self._ws_file_service.find_snapshot_by_id(snap_id)
                sess_for_sync = sinfo.session_id if sinfo else (self._session_id or "")
            else:
                sess_for_sync = self._session_id or ""

            if sess_for_sync and op_type in (
                "ACCEPT_ALL",
                "REJECT_ALL",
                "ACCEPT",
                "REJECT",
                "APPLY",
                "SWITCH",
                "ACTIVATE",
            ):
                try:
                    await self._send_snapshot_sync_all(sess_for_sync)
                except Exception as e:
                    logger.warning("[WS-FILE] snapshot/operate 后 snapshot/syncAll 失败: {}", e)

            if err:
                await self._send_response(msg_id, {"errorCode": err, "errorMessage": err})
            else:
                await self._send_response(msg_id, {"errorCode": None, "errorMessage": None})
        except Exception as e:
            logger.warning("[LSP4J] snapshot/operate failed: {}", e)
            await self._send_response(msg_id, {"errorCode": "operate_failed", "errorMessage": str(e)})

    async def _handle_chat_list_all_sessions(self, params: dict, msg_id: Any) -> None:
        """chat/listAllSessions → IDE 历史列表（仅 ide_lsp4j 持久化会话）。"""
        project_uri = params.get("projectUri") or ""
        try:
            async with async_session() as db:
                r = await db.execute(
                    select(ChatSession)
                    .where(ChatSession.user_id == self._user_id)
                    .where(ChatSession.agent_id == self._agent_id)
                    .where(ChatSession.source_channel == "ide_lsp4j")
                    .order_by(ChatSession.created_at.desc())
                    .limit(50)
                )
                sessions = list(r.scalars().all())
        except Exception as e:
            logger.warning("[LSP4J] chat/listAllSessions DB error: {}", e)
            await self._send_response(msg_id, [])
            return

        out: list[dict[str, Any]] = []
        for sess in sessions:
            out.append(
                {
                    "sessionId": str(sess.id),
                    "sessionTitle": sess.title or "Chat",
                    "chatRecords": [],
                    "gmtCreate": self._epoch_ms(sess.created_at),
                    "gmtModified": self._epoch_ms(sess.last_message_at or sess.created_at),
                    "sessionType": "ASSISTANT",
                    "userId": str(sess.user_id),
                    "userName": "",
                    "projectId": "",
                    "projectUri": project_uri,
                    "projectName": "",
                }
            )
        await self._send_response(msg_id, out)

    async def _handle_chat_get_session_by_id(self, params: dict, msg_id: Any) -> None:
        """chat/getSessionById → 从 DB 组装可恢复的 ChatSession（含 REPLY_TASK 记录）。"""
        session_id = params.get("sessionId") or ""
        try:
            sid_uuid = uuid.UUID(session_id)
        except ValueError:
            await self._send_response(
                msg_id,
                {
                    "sessionId": session_id,
                    "sessionTitle": "",
                    "chatRecords": [],
                    "sessionType": "ASSISTANT",
                    "gmtCreate": 0,
                    "gmtModified": 0,
                },
            )
            return

        try:
            async with async_session() as db:
                sr = await db.execute(select(ChatSession).where(ChatSession.id == sid_uuid))
                sess = sr.scalar_one_or_none()
                if not sess or sess.user_id != self._user_id or sess.agent_id != self._agent_id:
                    await self._send_response(
                        msg_id,
                        {
                            "sessionId": session_id,
                            "sessionTitle": "",
                            "chatRecords": [],
                            "sessionType": "ASSISTANT",
                            "gmtCreate": 0,
                            "gmtModified": 0,
                        },
                    )
                    return

                mr = await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.conversation_id == str(sid_uuid))
                    .where(ChatMessage.agent_id == self._agent_id)
                    .where(ChatMessage.user_id == self._user_id)
                    .where(ChatMessage.role.in_(("user", "assistant", "tool_call")))
                    .order_by(ChatMessage.created_at.asc())
                )
                rows = list(mr.scalars().all())
        except Exception as e:
            logger.warning("[LSP4J] chat/getSessionById DB error: {}", e)
            await self._send_response(
                msg_id,
                {
                    "sessionId": session_id,
                    "sessionTitle": "",
                    "chatRecords": [],
                    "sessionType": "ASSISTANT",
                    "gmtCreate": 0,
                    "gmtModified": 0,
                },
            )
            return

        records: list[dict[str, Any]] = []
        i = 0
        while i < len(rows):
            row = rows[i]
            # 跳过 tool_call 行，仅处理 user/assistant 配对
            if row.role not in ("user", "assistant"):
                i += 1
                continue
            if row.role != "user":
                i += 1
                continue
            user_msg = row
            asst_msg = rows[i + 1] if i + 1 < len(rows) and rows[i + 1].role == "assistant" else None
            rid = str(uuid.uuid4())
            qtext = user_msg.content or ""
            ctx = json.dumps({"text": qtext, "displayText": qtext}, ensure_ascii=False)
            atext = (asst_msg.content if asst_msg else "") or ""
            think = (asst_msg.thinking if asst_msg else None) or ""
            records.append(
                {
                    "requestId": rid,
                    "sessionId": session_id,
                    "chatTask": "REPLY_TASK",
                    "chatContext": ctx,
                    "question": qtext,
                    "answer": atext,
                    "extra": "",
                    "likeStatus": 0,
                    "gmtCreate": self._epoch_ms(user_msg.created_at),
                    "gmtModified": self._epoch_ms(asst_msg.created_at if asst_msg else user_msg.created_at),
                    "filterStatus": "",
                    "sessionType": "ASSISTANT",
                    "summary": "",
                    "intentionType": "",
                    "reasoningContent": think,
                }
            )
            i += 2 if asst_msg else 1

        await self._send_response(
            msg_id,
            {
                "sessionId": session_id,
                "sessionTitle": sess.title or "Chat",
                "chatRecords": records,
                "sessionType": "ASSISTANT",
                "userId": str(sess.user_id),
                "userName": "",
                "projectId": "",
                "projectUri": "",
                "projectName": "",
                "gmtCreate": self._epoch_ms(sess.created_at),
                "gmtModified": self._epoch_ms(sess.last_message_at or sess.created_at),
            },
        )

    async def _handle_chat_delete_session_by_id(self, params: dict, msg_id: Any) -> None:
        """chat/deleteSessionById → 删除单个 IDE 会话 + 推送 chat/delete 通知。"""
        session_id = params.get("sessionId") or ""
        try:
            sid_uuid = uuid.UUID(session_id)
        except ValueError:
            await self._send_response(msg_id, None)
            return

        deleted = False
        try:
            async with async_session() as db:
                sr = await db.execute(select(ChatSession).where(ChatSession.id == sid_uuid))
                sess = sr.scalar_one_or_none()
                if (
                    sess
                    and sess.user_id == self._user_id
                    and sess.agent_id == self._agent_id
                    and sess.source_channel == "ide_lsp4j"
                ):
                    await db.execute(
                        delete(ChatMessage)
                        .where(ChatMessage.conversation_id == str(sid_uuid))
                        .where(ChatMessage.agent_id == self._agent_id)
                        .where(ChatMessage.user_id == self._user_id)
                    )
                    await db.delete(sess)
                    await db.commit()
                    deleted = True
        except Exception as e:
            logger.warning("[LSP4J] chat/deleteSessionById DB error: {}", e)

        if self._session_id == session_id:
            self._session_id = None
        self._tool_call_history_by_session.pop(session_id, None)
        await self._send_response(msg_id, None)

        if deleted:
            try:
                await self._send_client_request("chat/delete", {"sessionId": session_id, "requestId": ""})
                logger.debug("[LSP4J] chat/delete 已推送: sessionId={}", session_id)
            except Exception as e:
                logger.warning("[LSP4J] chat/delete 推送失败: {}", e)

    async def _handle_chat_clear_all_sessions(self, params: dict, msg_id: Any) -> None:
        """chat/clearAllSessions → 清空当前 agent+user 的 IDE 会话（Clawith-only）。"""
        _ = params
        try:
            async with async_session() as db:
                sr = await db.execute(
                    select(ChatSession.id)
                    .where(ChatSession.user_id == self._user_id)
                    .where(ChatSession.agent_id == self._agent_id)
                    .where(ChatSession.source_channel == "ide_lsp4j")
                )
                session_ids = [str(v) for v in sr.scalars().all()]
                if session_ids:
                    await db.execute(delete(ChatMessage).where(ChatMessage.conversation_id.in_(session_ids)))
                await db.execute(
                    delete(ChatSession)
                    .where(ChatSession.user_id == self._user_id)
                    .where(ChatSession.agent_id == self._agent_id)
                    .where(ChatSession.source_channel == "ide_lsp4j")
                )
                await db.commit()
        except Exception as e:
            logger.warning("[LSP4J] chat/clearAllSessions DB error: {}", e)

        self._session_id = None
        self._tool_call_history_by_session.clear()
        await self._ws_file_service.clear()
        await self._send_response(msg_id, None)

    async def _handle_chat_delete_chat_by_id(self, params: dict, msg_id: Any) -> None:
        """chat/deleteChatById → 删除单条聊天记录 + 推送 chat/delete 通知。"""
        session_id = params.get("sessionId") or ""
        request_id = params.get("requestId") or ""
        try:
            sid_uuid = uuid.UUID(session_id)
            rid_uuid = uuid.UUID(request_id)
        except ValueError:
            await self._send_response(msg_id, None)
            return

        deleted = False
        try:
            async with async_session() as db:
                res = await db.execute(
                    delete(ChatMessage)
                    .where(ChatMessage.id == rid_uuid)
                    .where(ChatMessage.conversation_id == str(sid_uuid))
                    .where(ChatMessage.agent_id == self._agent_id)
                    .where(ChatMessage.user_id == self._user_id)
                )
                await db.commit()
                deleted = res.rowcount > 0
        except Exception as e:
            logger.warning("[LSP4J] chat/deleteChatById DB error: {}", e)
        await self._send_response(msg_id, None)

        if deleted:
            try:
                await self._send_client_request(
                    "chat/delete",
                    {
                        "sessionId": session_id,
                        "requestId": request_id,
                    },
                )
                logger.debug("[LSP4J] chat/delete 已推送: sessionId={} requestId={}", session_id, request_id)
            except Exception as e:
                logger.warning("[LSP4J] chat/delete 推送失败: {}", e)

    # ── reasoning 模型名称模式（基于 provider + model 推断，避免 DB 迁移） ──
    _REASONING_PATTERNS: tuple[tuple[str, str], ...] = (
        # (provider 片段, model 片段) — 任一匹配即判定为 reasoning 模型
        ("deepseek", "r1"),
        ("deepseek", "reasoner"),
        ("openai", "o1"),
        ("openai", "o3"),
        ("openai", "o4"),
        ("anthropic", "opus"),
        ("qwen", "qwq"),
        ("qwen", "qwen3"),
    )

    @classmethod
    def _is_reasoning_model(cls, model_name: str, provider: str) -> bool:
        """基于模型名称模式检测 reasoning 能力。"""
        _model_lower = model_name.lower()
        _provider_lower = (provider or "").lower()
        for _p, _m in cls._REASONING_PATTERNS:
            if _p in _provider_lower and _m in _model_lower:
                return True
        # 通用模式：模型名含 "reasoning" / "thinking" / "thinker"
        if any(kw in _model_lower for kw in ("reasoning", "thinking", "thinker")):
            return True
        return False

    async def _handle_config_query_models(self, params: dict, msg_id: Any) -> None:
        """config/queryModels → Map<String, List<ChatModelItem>>（Clawith-only 真数据）。"""
        _ = params
        tenant_id = getattr(self._agent_obj, "tenant_id", None)
        try:
            async with async_session() as db:
                stmt = (
                    select(LLMModel)
                    .where(LLMModel.enabled.is_(True))
                    .order_by(LLMModel.updated_at.desc(), LLMModel.created_at.desc())
                )
                if tenant_id:
                    stmt = stmt.where((LLMModel.tenant_id.is_(None)) | (LLMModel.tenant_id == tenant_id))
                rows = list((await db.execute(stmt)).scalars().all())
        except Exception as e:
            logger.warning("[LSP4J] config/queryModels DB error: {}", e)
            rows = []

        models: list[dict[str, Any]] = []
        for m in rows:
            label = (m.label or "").strip() or m.model
            models.append(
                {
                    "key": str(m.id),
                    "displayName": label,
                    "format": m.provider or "openai",
                    "isVl": bool(m.supports_vision),
                    "isReasoning": self._is_reasoning_model(m.model, m.provider or ""),
                    "baseUrl": m.base_url or "",
                    "source": "tenant" if m.tenant_id else "system",
                }
            )

        if not models:
            models = [
                {
                    "key": "clawith-default",
                    "displayName": "Clawith",
                    "format": "openai",
                    "isVl": False,
                    "isReasoning": False,
                    "baseUrl": "",
                    "source": "system",
                }
            ]
        await self._send_response(msg_id, {"default": models})

    async def _handle_model_query_classes(self, params: dict, msg_id: Any) -> None:
        """model/queryClasses → 返回模型类别列表，供插件 UI 分类筛选。"""
        _ = params
        await self._send_response(
            msg_id,
            {
                "classes": [
                    {"key": "clawith", "displayName": "Clawith Models"},
                    {"key": "byok", "displayName": "BYOK (Bring Your Own Key)"},
                ],
            },
        )

    async def _handle_ping(self, params: dict, msg_id: Any) -> None:
        """ping → PingResult（通过 SELECT 1 检查 DB 连接健康）。"""
        _ = params
        try:
            from sqlalchemy import text

            async with async_session() as db:
                await db.execute(text("SELECT 1"))
            await self._send_response(msg_id, {"success": True})
        except Exception as e:
            logger.warning("[LSP4J] ping DB health check failed: {}", e)
            await self._send_response(
                msg_id,
                {
                    "success": False,
                    "errorMessage": f"Database health check failed: {e}",
                },
            )

    # ──────────────────────────────────────────
    # 工作区文件服务（workingSpaceFile/*）
    # ──────────────────────────────────────────

    async def _handle_ws_get_last_stable_content(self, params: dict, msg_id: Any) -> None:
        """workingSpaceFile/getLastStableContent — 返回编辑前的文件内容。

        插件 InlineDiffManager.showSingleDiff 调用此方法获取 diff 左侧内容。
        参数：WorkingSpaceFileParams { id, sessionId?, fileId?, version? }
        返回：WorkingSpaceFileContent { content, version, errorCode?, errorMessage? }
        """
        ws_id = params.get("id", "")
        ws_file = await self._ws_file_service.get_file_by_id(ws_id)
        if ws_file:
            logger.info("[WS-FILE] getLastStableContent: id={} len={}", ws_id[:8], len(ws_file.last_stable_content))
            await self._send_response(
                msg_id,
                {
                    "content": ws_file.last_stable_content,
                    "version": ws_file.version,
                    "errorCode": None,
                    "errorMessage": None,
                },
            )
        else:
            logger.warning("[WS-FILE] getLastStableContent: 未找到 id={}", ws_id)
            await self._send_response(
                msg_id,
                {
                    "content": "",
                    "version": "0",
                    "errorCode": None,
                    "errorMessage": None,
                },
            )

    async def _handle_ws_get_full_content(self, params: dict, msg_id: Any) -> None:
        """workingSpaceFile/getFullContent — 返回编辑后的文件内容。

        插件 InlineDiffManager.showSingleDiff 调用此方法获取 diff 右侧内容。
        """
        ws_id = params.get("id", "")
        ws_file = await self._ws_file_service.get_file_by_id(ws_id)
        if ws_file:
            logger.info("[WS-FILE] getFullContent: id={} len={}", ws_id[:8], len(ws_file.content))
            await self._send_response(
                msg_id,
                {
                    "content": ws_file.content,
                    "version": ws_file.version,
                    "errorCode": None,
                    "errorMessage": None,
                },
            )
        else:
            logger.warning("[WS-FILE] getFullContent: 未找到 id={}", ws_id)
            await self._send_response(
                msg_id,
                {
                    "content": "",
                    "version": "0",
                    "errorCode": None,
                    "errorMessage": None,
                },
            )

    async def _handle_ws_list_by_snapshot(self, params: dict, msg_id: Any) -> None:
        """workingSpaceFile/listBySnapshot — 列出某个快照下的所有工作区文件。

        参数：ListWorkingSpaceFileBySnapshotParams { snapshotId, sessionId, requestId }
        返回：ListWorkingSpaceFileResult { workingSpaceFiles, errorCode, errorMessage }
        """
        snapshot_id = params.get("snapshotId", "")
        files = await self._ws_file_service.list_by_snapshot(snapshot_id)
        logger.info(
            "[WS-FILE] listBySnapshot: snapshotId={} count={}", snapshot_id[:8] if snapshot_id else "?", len(files)
        )
        await self._send_response(
            msg_id,
            {
                "workingSpaceFiles": [f.to_wire_format() for f in files],
                "errorCode": None,
                "errorMessage": None,
            },
        )

    async def _handle_ws_operate(self, params: dict, msg_id: Any) -> None:
        """workingSpaceFile/operate — 接受/拒绝文件变更。

        参数：WorkingSpaceFileOperateParams { id, opType, content }
        """
        ws_id = params.get("id", "")
        op_type = params.get("opType", "")
        content = params.get("content")
        success = await self._ws_file_service.operate(ws_id, op_type, content)
        logger.info("[WS-FILE] operate: id={} op={} success={}", ws_id[:8], op_type, success)
        await self._send_response(
            msg_id,
            {
                "errorCode": None,
                "errorMessage": None,
            },
        )
        # 操作后发送状态更新通知
        if success:
            ws_file = await self._ws_file_service.get_file_by_id(ws_id)
            if ws_file:
                try:
                    await self._send_workspace_file_sync(ws_file, "MODIFIED")
                except Exception as e:
                    logger.warning("[WS-FILE] operate 后发送 sync 失败: {}", e)

    async def _handle_ws_update_content(self, params: dict, msg_id: Any) -> None:
        """workingSpaceFile/updateContent — 更新文件内容。

        参数：UpdateWorkingSpaceFileContentRequest { id, content?, localContent? }
        """
        ws_id = params.get("id", "")
        content = params.get("content")
        local_content = params.get("localContent")
        success = await self._ws_file_service.update_content(ws_id, content, local_content)
        logger.info("[WS-FILE] updateContent: id={} success={}", ws_id[:8], success)
        await self._send_response(
            msg_id,
            {
                "errorCode": None,
                "errorMessage": None,
            },
        )

    async def _handle_step_process_confirm(self, params: dict, msg_id: Any) -> None:
        """处理 agents/testAgent/stepProcessConfirm — 步骤确认请求。

        当 step callback 的 status="manual_confirm" 时，插件显示确认按钮。
        用户点击后发送此请求，Clawith 返回确认结果。

        返回格式：StepProcessConfirmResult { requestId, errorMessage, successful }
        """
        # 步骤确认日志
        logger.info(
            "[LSP4J] stepProcessConfirm: requestId={} params_keys={}", params.get("requestId", ""), list(params.keys())
        )
        await self._send_response(
            msg_id,
            {
                "requestId": params.get("requestId", ""),
                "successful": True,
                "errorMessage": "",
            },
        )

    # ──────────────────────────────────────────
    # 图片上传
    # ──────────────────────────────────────────

    def _cleanup_expired_images(self) -> None:
        """清理过期的图片缓存（过期 + LRU 大小限制）。"""
        now = time.time()
        # 清理过期缓存
        expired_keys = [k for k, (_, _, ts) in self._image_cache.items() if now - ts > self._image_cache_ttl]
        for k in expired_keys:
            del self._image_cache[k]
        # LRU 大小限制：超出 max_size 则按时间排序淘汰最旧的
        if len(self._image_cache) > self._image_cache_max_size:
            sorted_items = sorted(self._image_cache.items(), key=lambda x: x[1][2])
            excess = len(self._image_cache) - self._image_cache_max_size
            for k, _ in sorted_items[:excess]:
                del self._image_cache[k]
        if expired_keys:
            logger.debug("[LSP4J] 图片缓存清理: 过期={} 剩余={}", len(expired_keys), len(self._image_cache))

    async def _handle_image_upload(self, params: dict, msg_id: Any) -> None:
        """处理 image/upload 请求 — 双响应模式。

        参数格式（UploadImageParams）：imageUri, requestId
        1. 先校验图片（必须 data URI，大小≤10MB）
        2. 校验失败直接返回 success=False 同步错误响应
        3. 校验通过立即返回 UploadImageResult{requestId, result:{success:true}}
        4. 异步发送 image/uploadResultNotification{result:{requestId, imageUrl}}
        """
        image_uri = params.get("imageUri", "")
        request_id = params.get("requestId", "")

        # 清理过期缓存
        self._cleanup_expired_images()

        # 校验：必须为 data URI 格式
        if not image_uri.startswith("data:"):
            logger.warning("[LSP4J] image/upload: 不支持本地文件路径, requestId={}", request_id)
            await self._send_response(
                msg_id,
                {
                    "requestId": request_id,
                    "errorCode": "LOCAL_PATH_NOT_SUPPORTED",
                    "errorMessage": "LSP4J 模式暂不支持本地文件路径图片上传",
                    "result": {"success": False},
                },
            )
            return

        # 校验：base64 数据大小≤10MB
        base64_data = image_uri.split(",", 1)[1] if "," in image_uri else ""
        if len(base64_data) > 10 * 1024 * 1024:
            logger.warning("[LSP4J] image/upload: 图片超过 10MB 限制, requestId={}", request_id)
            await self._send_response(
                msg_id,
                {
                    "requestId": request_id,
                    "errorCode": "FILE_TOO_LARGE",
                    "errorMessage": "图片大小超过 10MB 限制",
                    "result": {"success": False},
                },
            )
            return

        # 校验通过，立即返回成功响应
        await self._send_response(
            msg_id,
            {
                "requestId": request_id,
                "result": {"success": True},
            },
        )

        # 异步发送 uploadResultNotification
        try:
            # 生成图片 URL（当前 MVP 直接使用 data URI 作为 imageUrl）
            image_url = image_uri
            # 缓存图片
            self._image_cache[request_id] = (image_url, base64_data, time.time())

            await self._send_notification(
                "image/uploadResultNotification",
                {
                    "result": {
                        "requestId": request_id,
                        "imageUrl": image_url,
                    },
                },
            )
            logger.info("[LSP4J] image/upload 成功: requestId={} base64_len={}", request_id, len(base64_data))
        except Exception as e:
            logger.warning("[LSP4J] image/upload 异步通知失败: requestId={} error={}", request_id, e)

    # ──────────────────────────────────────────
    # 配置端点
    # ──────────────────────────────────────────

    async def _handle_config_get_endpoint(self, params: dict, msg_id: Any) -> None:
        """处理 config/getEndpoint 请求。

        优先从 ide_plugin_configs 表读取持久化 endpoint，回退到 PUBLIC_BASE_URL。
        返回 GlobalEndpointConfig 格式：{"endpoint": "https://..."}。
        """
        _ = params
        endpoint = ""
        try:
            from app.models.plugin_config import IDEPluginConfig

            async with async_session() as db:
                sr = await db.execute(
                    select(IDEPluginConfig.config_value)
                    .where(IDEPluginConfig.scope_type == "agent")
                    .where(IDEPluginConfig.scope_id == self._agent_id)
                    .where(IDEPluginConfig.config_key == "lsp4j_endpoint")
                )
                row = sr.scalar_one_or_none()
                if row:
                    endpoint = row
        except Exception as e:
            logger.warning("[LSP4J] config/getEndpoint DB 回退: {}", e)

        if not endpoint:
            settings = get_settings()
            endpoint = settings.PUBLIC_BASE_URL or ""
        logger.debug("[LSP4J] config/getEndpoint: endpoint={}", endpoint[:80] if endpoint else "")
        await self._send_response(msg_id, {"endpoint": endpoint})

    async def _handle_config_update_endpoint(self, params: dict, msg_id: Any) -> None:
        """处理 config/updateEndpoint 请求。

        接收 GlobalEndpointConfig{endpoint: String}，
        持久化到 ide_plugin_configs 表（scope_type="agent", config_key="lsp4j_endpoint"）。
        返回 UpdateConfigResult{success: Boolean}。
        """
        endpoint = params.get("endpoint", "")
        if not endpoint:
            await self._send_response(msg_id, {"success": False, "errorMessage": "Empty endpoint"})
            return

        try:
            from app.models.plugin_config import IDEPluginConfig

            async with async_session() as db:
                sr = await db.execute(
                    select(IDEPluginConfig)
                    .where(IDEPluginConfig.scope_type == "agent")
                    .where(IDEPluginConfig.scope_id == self._agent_id)
                    .where(IDEPluginConfig.config_key == "lsp4j_endpoint")
                )
                config = sr.scalar_one_or_none()
                if config:
                    config.config_value = endpoint
                else:
                    config = IDEPluginConfig(
                        scope_type="agent",
                        scope_id=self._agent_id,
                        config_key="lsp4j_endpoint",
                        config_value=endpoint,
                    )
                    db.add(config)
                await db.commit()

            logger.info(
                "[LSP4J] config/updateEndpoint 已持久化: agent_id={} endpoint={}",
                self._agent_id,
                endpoint[:80] if endpoint else "",
            )
            await self._send_response(msg_id, {"success": True})
        except Exception as e:
            logger.warning("[LSP4J] config/updateEndpoint DB error: {}", e)
            await self._send_response(
                msg_id,
                {
                    "success": False,
                    "errorMessage": f"Persist failed: {e}",
                },
            )

    # ──────────────────────────────────────────
    # Commit 消息生成
    # ──────────────────────────────────────────

    async def _handle_commit_msg_generate(self, params: dict, msg_id: Any) -> None:
        """处理 commitMsg/generate 请求。

        参数格式（GenerateCommitMsgParam）：
        - requestId, codeDiffs: List, commitMessages: List, stream, preferredLanguage

        流程：
        1. 立即返回 GenerateCommitMsgResult{requestId, isSuccess:true, errorCode:0, errorMessage:""}
        2. 通过 commitMsg/answer 流式返回
        3. 通过 commitMsg/finish 通知完成

        ⚠️ call_llm 必填参数：role_description, messages 格式为 list[dict]
        """
        request_id = params.get("requestId", str(uuid.uuid4()))
        code_diffs = params.get("codeDiffs", [])
        commit_messages = params.get("commitMessages", [])
        stream = params.get("stream", True)
        preferred_language = params.get("preferredLanguage", "")

        # 1. 立即返回成功响应（必须在通知之前）
        await self._send_response(
            msg_id,
            {
                "requestId": request_id,
                "isSuccess": True,
                "errorCode": 0,
                "errorMessage": "",
            },
        )

        # 2. 构建 prompt（按文件粒度截断，保留所有文件路径 + 每文件前 600 字符）
        _diff_parts: list[str] = []
        _total_budget = 10000  # 总输入预算（字符）
        _per_file_cap = 600  # 单文件最大字符
        _used = 0
        for _d in code_diffs or []:
            _d_str = str(_d)
            if _used + len(_d_str) > _total_budget:
                # 超过总预算：截断当前文件剩余部分
                _remaining = _total_budget - _used
                if _remaining > 80:  # 至少保留有意义的片段
                    _d_str = _d_str[:_remaining] + "\n...\n[剩余内容已截断]"
                else:
                    break
            elif len(_d_str) > _per_file_cap:
                # 单文件超过上限：保留头尾各半
                _half = _per_file_cap // 2
                _d_str = _d_str[:_half] + f"\n...\n[省略 {len(_d_str) - _per_file_cap} 字符]\n...\n" + _d_str[-_half:]
            _diff_parts.append(_d_str)
            _used += len(_d_str)
        diff_text = "\n".join(_diff_parts) if _diff_parts else ""

        existing_msgs = "\n".join(str(m) for m in commit_messages) if commit_messages else ""
        lang_hint = f"请使用{preferred_language}。" if preferred_language else "请使用中文。"
        prompt = (
            f"根据以下代码变更，生成简洁的 Git commit message（不超过200字符）。\n{lang_hint}\n\n代码变更:\n{diff_text}"
        )
        if existing_msgs:
            prompt += f"\n\n已有的 commit messages:\n{existing_msgs[:1500]}"

        messages = [{"role": "user", "content": prompt}]

        # 3. 定义流式回调
        async def _on_chunk(text: str) -> None:
            if stream:
                await self._send_client_request(
                    "commitMsg/answer",
                    {
                        "requestId": request_id,
                        "text": text,
                        "timestamp": int(time.time() * 1000),
                    },
                )

        # 4. 加载 fallback 模型
        fallback_model_obj = None
        if self._agent_obj.fallback_model_id:
            async with async_session() as _fb_db:
                _fb_r = await _fb_db.execute(select(LLMModel).where(LLMModel.id == self._agent_obj.fallback_model_id))
                fallback_model_obj = _fb_r.scalar_one_or_none()

        # 5. 调用 call_llm_with_failover
        try:
            reply = await call_llm_with_failover(
                primary_model=self._model_obj,
                fallback_model=fallback_model_obj,
                messages=messages,
                agent_name="CommitMessageGenerator",
                role_description="Git commit message generator",
                agent_id=self._agent_id,
                user_id=self._user_id,
                on_chunk=_on_chunk if stream else None,
            )
        except Exception as e:
            logger.error("[LSP4J] commitMsg/generate call_llm error: {}", e)
            reply = f"[错误] {e}"

        # 5. 非流式模式一次性返回（截断保护，commit message 不超过 500 字符）
        if not stream and reply:
            _commit_reply = reply.strip()[:500]
            await self._send_client_request(
                "commitMsg/answer",
                {
                    "requestId": request_id,
                    "text": _commit_reply,
                    "timestamp": int(time.time() * 1000),
                },
            )

        # 6. 发送完成通知
        await self._send_client_request(
            "commitMsg/finish",
            {
                "requestId": request_id,
                "statusCode": 200,
                "reason": "",
            },
        )

    # ──────────────────────────────────────────
    # 多轮追问建议列表
    # ──────────────────────────────────────────

    async def _handle_chat_reply_request(self, params: dict, msg_id: Any) -> None:
        """处理 chat/replyRequest 请求。

        参数格式（ChatReplyRequestParam）：requestId, sessionId
        响应格式（ChatReplyListResult）：requestId, displayTasks, isSuccess

        插件端通过此接口获取多轮追问的建议列表（DisplayTask）。
        Clawith 当前不支持 DisplayTask 体系，返回空列表 + 成功。
        实际多轮对话能力通过 chat/ask{isReply:true} 实现。
        """
        await self._send_response(
            msg_id,
            {
                "requestId": params.get("requestId", ""),
                "displayTasks": [],
                "isSuccess": True,
            },
        )

    # ──────────────────────────────────────────
    # 点赞/踩反馈记录
    # ──────────────────────────────────────────

    async def _handle_chat_like(self, params: dict, msg_id: Any) -> None:
        """处理 chat/like 请求。

        参数格式（ChatLikeParam）：requestId, sessionId, like (int: 1=赞, -1=踩)
        响应格式（ChatLikeResult）：requestId, sessionId, isSuccess

        记录用户反馈到日志，后续可扩展写入数据库。
        """
        _like_val = params.get("like", 0)
        _label = "赞" if _like_val == 1 else ("踩" if _like_val == -1 else f"未知({_like_val})")
        logger.info(
            "[LSP4J] chat/like: requestId={} sessionId={} like={} label={}",
            params.get("requestId", ""),
            params.get("sessionId", ""),
            _like_val,
            _label,
        )
        await self._send_response(
            msg_id,
            {
                "requestId": params.get("requestId", ""),
                "sessionId": params.get("sessionId", ""),
                "isSuccess": True,
            },
        )

    # ──────────────────────────────────────────
    # 自定义命令查询
    # ──────────────────────────────────────────

    async def _handle_extension_query(self, params: dict, msg_id: Any) -> None:
        """处理 extension/query 请求。

        响应格式（CustomCommandGetResult）：
        - commands: List[CustomCommand] — 自定义命令列表
        - commandShowPosition: String — 命令显示位置
        - contextProviders: List[CustomContext] — 上下文提供者

        Clawith 当前无自定义命令体系，返回空列表。
        """
        _ = params
        await self._send_response(
            msg_id,
            {
                "commands": [],
                "commandShowPosition": "",
                "contextProviders": [],
            },
        )

    # ──────────────────────────────────────────
    # BYOK 模型配置
    # ──────────────────────────────────────────

    async def _handle_model_get_byok_config(self, params: dict, msg_id: Any) -> None:
        """处理 model/getByokConfig 请求。

        响应格式（ByokConfigResult）：enabled, providers, tags
        Clawith 不支持 BYOK（Bring Your Own Key），返回禁用状态。
        """
        _ = params
        await self._send_response(
            msg_id,
            {
                "enabled": False,
                "providers": [],
                "tags": [],
            },
        )

    async def _handle_model_check_byok_config(self, params: dict, msg_id: Any) -> None:
        """处理 model/checkByokConfig 请求。

        参数格式（CheckByokConfigParam）：provider, model, parameters
        响应格式（CheckByokConfigResult）：errorCode, errorMsg, success

        Clawith 不支持 BYOK，始终返回失败。
        """
        logger.info(
            "[LSP4J] model/checkByokConfig: provider={} model={}", params.get("provider", ""), params.get("model", "")
        )
        await self._send_response(
            msg_id,
            {
                "errorCode": "BYOK_NOT_SUPPORTED",
                "errorMsg": "Clawith 不支持 BYOK 自定义模型密钥配置",
                "success": False,
            },
        )

    async def _handle_tool_call_results(self, params: dict, msg_id: Any) -> None:
        """处理 tool/call/results 请求（Clawith-only：返回会话内历史）。"""
        session_id = params.get("sessionId", "")
        tool_results = self._tool_call_history_by_session.get(session_id, [])
        await self._send_response(
            msg_id,
            {
                "successful": True,
                "errorMessage": "",
                "toolResults": tool_results,
            },
        )

    async def _handle_auth_status(self, params: dict, msg_id: Any) -> None:
        """auth/status → AuthStatus（基于 WebSocket 认证 token 查询真实用户数据）。"""
        _ = params
        _name = ""
        _email = ""
        _avatar = ""
        _tenant_id = ""
        _tenant_name = ""
        try:
            async with async_session() as db:
                from app.models.user import User as UserModel

                ur = await db.execute(select(UserModel).where(UserModel.id == self._user_id))
                user_obj = ur.scalar_one_or_none()
                if user_obj is not None:
                    _name = user_obj.display_name or ""
                    _avatar = user_obj.avatar_url or ""
                    _email = getattr(user_obj, "email", None) or ""
                    _tenant_id = str(user_obj.tenant_id) if user_obj.tenant_id else ""
                    if user_obj.tenant_id:
                        from app.models.tenant import Tenant

                        tr = await db.execute(select(Tenant).where(Tenant.id == user_obj.tenant_id))
                        tenant_obj = tr.scalar_one_or_none()
                        if tenant_obj is not None:
                            _tenant_name = tenant_obj.name or ""
        except Exception as e:
            logger.warning("[LSP4J-AUTH] 查询用户信息失败，使用降级数据: {}", e)

        await self._send_response(
            msg_id,
            {
                "messageId": "",
                "status": 2,  # AuthStateEnum.LOGIN
                "name": _name or f"User {str(self._user_id)[:8]}",
                "id": str(self._user_id),
                "accountId": str(self._user_id),
                "token": "",
                "quota": 1,
                "whitelist": 3,  # AuthWhitelistStatusEnum.PASS
                "orgId": _tenant_id,
                "orgName": _tenant_name or "Clawith",
                "yxUid": str(self._user_id),
                "avatarUrl": _avatar,
                "userType": "clawith",
                "isSubAccount": False,
                "cloudType": "private",
                "email": _email,
                "userTag": "clawith-only",
                "privacyPolicyAgreed": True,
                "isPrivacyPolicyModifiable": False,
            },
        )

    async def _handle_session_get_current(self, params: dict, msg_id: Any) -> None:
        """session/getCurrent → ListCurrentSessionResult。"""
        _ = params
        current_session_ids = [self._session_id] if self._session_id else []
        await self._send_response(
            msg_id,
            {
                "errorCode": None,
                "errorMessage": None,
                "currentSessionIds": current_session_ids,
            },
        )

    async def _handle_code_change_apply(self, params: dict, msg_id: Any) -> None:
        """处理 chat/codeChange/apply — 交互式代码变更（diff 渲染入口）。

        当用户点击灵码聊天面板中代码块的 "Apply" 按钮时触发。
        插件期望收到 ChatCodeChangeApplyResult 格式的响应，
        其中 applyCode 为最终要应用到文件的代码内容。

        ChatCodeChangeApplyResult（9 个字段，源码验证）：
        - applyId, projectPath, filePath, applyCode, requestId, sessionId, extra, sessionType, mode

        安全守卫：空 codeEdit 直接拒绝，防止误清空用户文件。
        """
        apply_id = params.get("applyId", str(uuid.uuid4()))
        code_edit = params.get("codeEdit", "")
        file_path = params.get("filePath", "")
        request_id = params.get("requestId", "")
        session_id = params.get("sessionId", "")

        # diff 入口日志
        logger.info(
            "[LSP4J] codeChange/apply: applyId={} filePath={} requestId={} codeEdit_len={}",
            apply_id,
            file_path,
            request_id,
            len(code_edit),
        )

        # 空 codeEdit 安全守卫：拒绝请求，防止 IDE 端误将空内容写入文件
        if not code_edit or not code_edit.strip():
            logger.warning(
                "[LSP4J] codeChange/apply REJECTED: empty codeEdit, applyId={} filePath={}", apply_id, file_path
            )
            await self._send_response(
                msg_id,
                {
                    "applyId": apply_id,
                    "projectPath": params.get("projectPath", ""),
                    "filePath": file_path,
                    "applyCode": "",  # 必须为空，插件端校验后会拒绝应用
                    "requestId": request_id,
                    "sessionId": session_id,
                    "extra": params.get("extra", ""),
                    "sessionType": params.get("sessionType", ""),
                    "mode": params.get("mode", ""),
                },
            )
            # 发送拒绝通知，避免插件端长时间等待 diff
            await self._send_client_request(
                "chat/codeChange/apply/finish",
                {
                    "applyId": apply_id,
                    "filePath": file_path,
                    "success": False,
                    "errorMessage": "Empty codeEdit rejected by server safety guard",
                },
            )
            return

        # 构建响应（ChatCodeChangeApplyResult 9 个字段）
        result = {
            "applyId": apply_id,
            "projectPath": params.get("projectPath", ""),
            "filePath": file_path,
            "applyCode": code_edit,  # ← 关键：插件用此渲染 diff
            "requestId": request_id,
            "sessionId": session_id,
            "extra": params.get("extra", ""),
            "sessionType": params.get("sessionType", ""),
            "mode": params.get("mode", ""),
        }
        await self._send_response(msg_id, result)

        # 发送 apply/finish 通知（部分插件版本依赖此通知刷新 diff）
        await self._send_client_request(
            "chat/codeChange/apply/finish",
            {
                **result,
                "success": True,
                "errorMessage": None,
            },
        )
        logger.debug("[LSP4J] codeChange/apply/finish sent: applyId={}", apply_id)

    # ──────────────────────────────────────────
    # 方法路由表
    # ──────────────────────────────────────────

    _METHOD_MAP: dict[str, Any] = {
        "initialize": _handle_initialize,
        "shutdown": _handle_shutdown,
        "exit": _handle_exit,
        "chat/ask": _handle_chat_ask,
        "chat/stop": _handle_chat_stop,
        # Java @JsonSegment("tool/call") + @JsonRequest("approve") → wire method "tool/call/approve"
        "tool/call/approve": _handle_tool_call_approve,
        "tool/invokeResult": _handle_tool_invoke_result,
    }
    # ── 扩展方法（基于 ChatService.java 15 个方法 + ToolCallService + ToolService + TestAgentService） ──
    # ⚠️ 使用 .update() 追加，确保已有的 tool/invokeResult 等现有条目不被覆盖
    _METHOD_MAP.update(
        {
            # ── chat/ 方法（ChatService.java @JsonSegment("chat")） ──
            "chat/systemEvent": _handle_stub,
            "chat/getStage": _handle_stub,
            "chat/replyRequest": _handle_chat_reply_request,  # 多轮追问建议列表
            "chat/like": _handle_chat_like,  # 点赞/踩反馈记录
            "chat/codeChange/apply": _handle_code_change_apply,  # 完整实现（ChatCodeChangeApplyResult + apply/finish）
            "image/upload": _handle_image_upload,  # 双响应模式（校验→响应→异步通知）
            "chat/stopSession": _handle_stub,
            "chat/receive/notice": _handle_stub,
            "chat/quota/doNotRemindAgain": _handle_stub,
            "chat/listAllSessions": _handle_chat_list_all_sessions,  # ide_lsp4j 持久化会话列表
            "chat/getSessionById": _handle_chat_get_session_by_id,  # 从 DB 恢复聊天记录
            "chat/deleteSessionById": _handle_chat_delete_session_by_id,  # 删除会话
            "chat/clearAllSessions": _handle_chat_clear_all_sessions,  # 清空会话
            "chat/deleteChatById": _handle_chat_delete_chat_by_id,  # 删除单条消息
            # ── config/ 方法（ConfigService.java） ──
            "config/getEndpoint": _handle_config_get_endpoint,  # 返回 GlobalEndpointConfig
            "config/updateEndpoint": _handle_config_update_endpoint,  # 更新端点配置
            # ── commitMsg/ 方法（CommitMessageService.java） ──
            "commitMsg/generate": _handle_commit_msg_generate,  # 生成 commit message（流式）
            # ── tool/ 方法（ToolCallService.java + ToolService.java） ──
            "tool/call/results": _handle_tool_call_results,  # ToolCallService.listToolCallInfo（MVP 空列表）
            # ── 任务规划工具（ToolTypeEnum.java:56-60） ──
            "tool/invoke": _handle_tool_invoke,  # 工具调用入口（add_tasks/todo_write/search_replace 在此处理）
            # ── agents/ 方法（TestAgentService.java） ──
            "agents/testAgent/stepProcessConfirm": _handle_step_process_confirm,
            # ── textDocument/ 方法（TextDocumentService.java — inline edit，P2-3） ──
            "textDocument/completion": _handle_completion,  # 标准 LSP 补全（返回空列表，避免 -32601）
            "textDocument/preCompletion": _handle_pre_completion,  # IDE 补全预请求（返回 Void）
            "textDocument/inlineEdit": _handle_inline_edit,  # 行内编辑建议（返回 InlineEditResult）
            "textDocument/editPredict": _handle_edit_predict,  # 编辑预测（返回 Void）
            # ── LanguageServer.java 直接定义的 @JsonRequest 方法 ──
            "config/getGlobal": _handle_stub,  # 全局配置查询
            "config/queryModels": _handle_config_query_models,  # 模型查询（占位列表）
            "ping": _handle_ping,  # 心跳 ping
            "ide/update": _handle_stub,  # IDE 状态更新
            "dataPolicy/query": _handle_stub,  # 数据政策查询
            "dataPolicy/sign": _handle_stub,  # 同意数据政策
            "dataPolicy/cancel": _handle_stub,  # 拒绝数据政策
            "auth/profile/getUrl": _handle_stub,  # 获取用户资料 URL
            "auth/profile/update": _handle_stub,  # 更新用户资料
            "extension/query": _handle_extension_query,  # 查询自定义命令（返回空列表）
            "extension/contextProvider/loadComboBoxItems": _handle_stub,  # 上下文下拉项加载
            "codebase/recommendation": _handle_stub,  # 代码库推荐
            "kb/list": _handle_stub,  # 知识库列表
            "model/queryClasses": _handle_model_query_classes,  # 查询模型类别
            "model/getByokConfig": _handle_model_get_byok_config,  # BYOK 配置查询（不支持，返回空）
            "model/checkByokConfig": _handle_model_check_byok_config,  # BYOK 配置校验（不支持，返回失败）
            "user/plan": _handle_stub,  # 用户计划查询
            "webview/command/list": _handle_stub,  # WebView 命令列表
            # ── @JsonDelegate 服务: AuthService（6 个方法） ──
            "auth/login": _handle_stub,  # 登录
            "auth/status": _handle_auth_status,  # Clawith-only 登录态
            "auth/logout": _handle_stub,  # 登出
            "auth/grantInfos": _handle_stub,  # 授权信息（@Deprecated）
            "auth/grantInfosWrap": _handle_stub,  # 授权信息（新版）
            "auth/switchAccount": _handle_stub,  # 切换账号
            # ── @JsonDelegate 服务: LoginService（1 个方法） ──
            "login/generateUrl": _handle_stub,  # 生成登录 URL
            # ── @JsonDelegate 服务: FeedbackService（1 个方法） ──
            "feedback/submit": _handle_stub,  # 提交反馈
            # ── @JsonDelegate 服务: SnapshotService（2 个方法） ──
            "snapshot/listBySession": _handle_snapshot_list_by_session,
            "snapshot/operate": _handle_snapshot_operate,
            # ── @JsonDelegate 服务: WorkingSpaceFileService（5 个方法） ──
            "workingSpaceFile/operate": _handle_ws_operate,
            "workingSpaceFile/listBySnapshot": _handle_ws_list_by_snapshot,
            "workingSpaceFile/getLastStableContent": _handle_ws_get_last_stable_content,
            "workingSpaceFile/getFullContent": _handle_ws_get_full_content,
            "workingSpaceFile/updateContent": _handle_ws_update_content,
            # ── @JsonDelegate 服务: SessionService（1 个方法） ──
            "session/getCurrent": _handle_session_get_current,  # 获取当前会话
            # ── @JsonDelegate 服务: SystemService（1 个方法） ──
            "system/reportDiagnosisLog": _handle_stub,  # 上报诊断日志
            # ── @JsonDelegate 服务: SnippetService（2 个方法） ──
            "snippet/search": _handle_stub,  # 代码片段搜索
            "snippet/report": _handle_stub,  # 代码片段上报
        }
    )


# ──────────────────────────────────────────────
# 模块级工具调用入口（供 tool_hooks.py 调用）
# ──────────────────────────────────────────────


async def invoke_lsp4j_tool(tool_name: str, arguments: dict, agent_id: uuid.UUID, user_id: uuid.UUID) -> str:
    """通过 LSP4J WebSocket 调用 IDE 端工具。

    由 tool_hooks.py 中的 _lsp4j_aware_execute_tool 调用。
    通过 _active_routers 映射表查找对应 agent 的路由器实例。

    Args:
        tool_name: 插件原生工具名称（read_file, replace_text_by_path 等）
        arguments: 工具参数字典
        agent_id: 智能体 UUID
        user_id: 用户 UUID

    Returns:
        工具执行结果字符串
    """
    # ── 正常 LSP4J 工具调用 ──
    # 使用 (user_id, agent_id) 复合键查找路由器，确保不同用户的连接不会互相干扰
    agent_key = (str(user_id), str(agent_id))
    router_instance = await get_active_router(agent_key)
    if router_instance is None:
        # ★ 子 Agent 回退：当子 Agent（通过 send_message_to_agent 调用）没有独立
        # LSP4J 连接时，回退到同一 user_id 下主 Agent 的 LSP4J 连接。
        # 只要 user_id 相同，说明是同一用户的 IDE，工具调用结果可以正确返回。
        for (rk_uid, rk_aid), rk_instance in await list_active_routers():
            if rk_uid == str(user_id):
                logger.info(
                    "[LSP4J-TOOL] 子 Agent 回退: sub_agent={} → primary_agent={} tool={}", agent_id, rk_aid, tool_name
                )
                router_instance = rk_instance
                break
    if router_instance is None:
        active_keys = [key for key, _ in await list_active_routers()]
        logger.warning("[LSP4J-TOOL] 路由器未找到: agent_key={} active_keys={}", agent_key, active_keys)
        return (
            "[LSP4J 不可用] IDE 插件 WebSocket 未连接，工具调用无法执行。"
            "请在 IDE 中：1) 确认 Clawith 插件已安装启用；2) 检查插件状态栏连接状态；"
            "3) 如已断开，点击重连。"
            f"（user_id={user_id}）"
        )

    # ── 工具调用结果缓存 ──
    # 同一会话中 search_file/list_dir/read_file 经常被重复调用相同参数，
    # 缓存结果避免重复 WebSocket 往返和 IDE 端重复执行。
    import hashlib

    # 负缓存检查：避免对已确认空结果的搜索重复发起 WebSocket 请求
    _neg_key_parts = [tool_name, json.dumps(arguments, sort_keys=True, default=str)]
    _neg_key = "|".join(p for p in _neg_key_parts if p)
    if _neg_key in _search_zero_result_cache:
        _ts, _reason = _search_zero_result_cache[_neg_key]
        if time.monotonic() - _ts < _SEARCH_ZERO_CACHE_TTL:
            logger.info("[LSP4J-CACHE] negative hit (remote): {} reason={}", _neg_key[:120], _reason)
            return "[]" if tool_name == "grep_code" else json.dumps([])

    if tool_name in _CACHEABLE_TOOLS:
        params_str = json.dumps(arguments, sort_keys=True, default=str)
        params_hash = hashlib.md5(params_str.encode()).hexdigest()[:12]
        cache_key = f"{tool_name}:{params_hash}"
        if cache_key in _lsp4j_tool_cache:
            ts, cached = _lsp4j_tool_cache[cache_key]
            if time.monotonic() - ts < _LSP4J_TOOL_CACHE_TTL:
                logger.info("[LSP4J-CACHE] hit: {} elapsed={:.1f}s", cache_key, time.monotonic() - ts)
                return cached

    # 工具超时策略（秒）：按工具类型差异化，避免 read_file 300s 掩盖真实故障
    _TOOL_TIMEOUTS = {
        "read_file": 30.0,
        "search_file": 60.0,
        "search_codebase": 60.0,
        "grep_code": 60.0,
        "search_symbol": 60.0,
        "list_dir": 30.0,
        "replace_text_by_path": 30.0,
        "create_file_with_text": 30.0,
        "delete_file_by_path": 30.0,
        "get_problems": 30.0,
        "get_terminal_output": 30.0,
        "search_replace": 30.0,
        "add_tasks": 15.0,
        "todo_write": 15.0,
    }
    timeout = _TOOL_TIMEOUTS.get(tool_name, 120.0)
    # run_in_terminal: 按命令类型区分超时，避免编译命令被截断
    if tool_name == "run_in_terminal":
        command = str(arguments.get("command", ""))
        _BUILD_KEYWORDS = (
            "gradlew",
            "mvn ",
            "npm run build",
            "make ",
            "cargo build",
            "xcodebuild",
            "bazel build",
            "cmake --build",
            "msbuild",
        )
        _READONLY_KEYWORDS = (
            "git show",
            "git diff",
            "git log",
            "ls ",
            "cat ",
            "head ",
            "tail ",
            "grep ",
            "find ",
            "wc ",
            "pwd",
        )
        if any(kw in command for kw in _BUILD_KEYWORDS):
            timeout = 600.0  # 编译/构建: 10 分钟
        elif any(kw in command for kw in _READONLY_KEYWORDS):
            timeout = 30.0  # 读操作: 30 秒
        else:
            timeout = 180.0  # 其他命令: 3 分钟
    logger.info("[LSP4J-TOOL] 调用 IDE 工具: tool={} agent_key={} timeout={}", tool_name, agent_key, timeout)
    result = await router_instance.invoke_tool_on_ide(tool_name, arguments, timeout=timeout)

    # 非 Python 项目检测 Python 命令：注入反诱导提示，引导 LLM 使用 IDE 原生工具
    if tool_name == "run_in_terminal" and result:
        command = str(arguments.get("command", ""))
        _PYTHON_SHELL_PATTERNS = (
            "python3 -c", "python -c", "python3 <<", "python <<",
            "import re", "import json", "import os",
        )
        if any(p in command for p in _PYTHON_SHELL_PATTERNS):
            logger.warning("[LSP4J] 检测到 Python 命令: {}", command[:80])
            result = (
                "[系统提示] 当前项目不是 Python 项目，"
                "请直接使用 read_file/replace_text_by_path/grep_code 等 IDE 原生工具操作代码。"
                "生成 Python 脚本对非 Python 项目的代码修改没有帮助。\n\n"
            ) + result

    # 缓存写入: 只缓存读操作的成功结果
    if tool_name in _CACHEABLE_TOOLS and result and not result.startswith("[错误]"):
        _lsp4j_tool_cache[cache_key] = (time.monotonic(), result)

    # 负缓存写入: LSP4J 远程空搜索结果也缓存，避免重复 WebSocket 往返
    _search_tools = {"search_file", "search_codebase", "grep_code", "search_symbol"}
    if tool_name in _search_tools and result:
        try:
            _parsed = json.loads(result) if isinstance(result, str) else result
            _items = _parsed if isinstance(_parsed, list) else _parsed.get("results", [])
            if len(_items) == 0:
                _search_zero_result_cache[_neg_key] = (time.monotonic(), "lsp4j_remote")
                logger.info("[LSP4J-CACHE] negative cache write (remote): {}", _neg_key[:120])
        except Exception:
            pass

    return result


# ──────────────────────────────────────────────
# 对话持久化
# ──────────────────────────────────────────────


async def _persist_lsp4j_chat_turn(
    agent_id: uuid.UUID,
    session_id: str,
    user_text: str,
    reply_text: str,
    user_id: uuid.UUID,
    thinking_text: str | None = None,
) -> None:
    """持久化一轮 LSP4J 对话到数据库（fire-and-forget 后台任务）。

    参考 ACP 的 _persist_chat_turn（router.py:1724-1784），
    source_channel 使用 "ide_lsp4j" 以区分来源。

    工具调用由 _persist_lsp4j_tool_call 单独持久化，此函数仅持久化 user/assistant 消息。

    ⚠️ 边界条件：session_id 应为 UUID 格式（通义灵码使用 UUID.randomUUID().toString()），
    但某些代码路径可能传 null。uuid.UUID() 抛 ValueError 时静默返回。

    内部捕获所有异常，不影响 IDE 响应。
    """
    try:
        from app.models.chat_session import ChatSession
        from app.models.audit import ChatMessage
        from app.models.participant import Participant  # noqa: F401 — 避免外键警告
        from datetime import datetime, timezone as tz_persist

        async with async_session() as db:
            # 验证 session_id 是否为有效 UUID
            try:
                sid_uuid = uuid.UUID(session_id)
            except ValueError:
                logger.debug("LSP4J: persist 跳过非 UUID session_id={}", session_id)
                return

            # 查找或创建 ChatSession
            sr = await db.execute(select(ChatSession).where(ChatSession.id == sid_uuid))
            sess = sr.scalar_one_or_none()
            now = datetime.now(tz_persist.utc)
            local_now = now.astimezone()

            if not sess:
                sess = ChatSession(
                    id=sid_uuid,
                    agent_id=agent_id,
                    user_id=user_id,
                    title=f"LSP4J {local_now.strftime('%m-%d %H:%M')}",
                    source_channel="ide_lsp4j",
                    created_at=now,
                    last_message_at=now,
                )
                db.add(sess)
                # TODO(P2-4): ChatSession 还有 project_path / current_file / open_files 字段，
                # 待插件未来发送对应数据后可直接利用，当前不增加死代码
            else:
                sess.last_message_at = now
                # 同步 agent_id：同一 session 可能在不同 agent 间共享（同一用户切换 Agent），
                # 更新为最近使用的 agent，确保 WebUI 正确归类会话
                if str(sess.agent_id) != str(agent_id):
                    logger.info(
                        "[LSP4J] Session agent_id updated: session={} old_agent={} new_agent={}",
                        session_id,
                        sess.agent_id,
                        agent_id,
                    )
                    sess.agent_id = agent_id

            # 添加消息
            # ★ 显式设置 created_at，确保用户消息时间戳早于助手消息
            # 避免同一事务中两条消息的 server_default 时间戳相同导致排序错乱
            from datetime import timedelta

            if user_text:
                db.add(
                    ChatMessage(
                        agent_id=agent_id,
                        user_id=user_id,
                        role="user",
                        content=user_text,
                        conversation_id=str(sid_uuid),
                        created_at=now - timedelta(seconds=1),  # 用户消息时间戳早 1 秒
                    )
                )

            if reply_text:
                db.add(
                    ChatMessage(
                        agent_id=agent_id,
                        user_id=user_id,
                        role="assistant",
                        content=reply_text,
                        conversation_id=str(sid_uuid),
                        thinking=thinking_text,  # 构造时传入，非事后更新
                        created_at=now,  # 助手消息时间戳
                    )
                )

            await db.commit()
            # 持久化成功
            logger.debug(
                "[LSP4J] persist success: session_id={} user_len={} reply_len={}",
                session_id,
                len(user_text),
                len(reply_text),
            )

            # 通知 Clawith 前端 WebSocket 刷新 Web UI
            try:
                sid_normalized = str(sid_uuid)
                await ws_module.manager.send_to_session(
                    str(agent_id),
                    sid_normalized,
                    {"type": "done", "role": "assistant", "content": reply_text, "source": "lsp4j"},
                )
                # 前端通知已发送
                logger.debug("[LSP4J] 前端通知已发送: session_id={}", sid_normalized)
            except Exception as _fe:
                logger.debug("LSP4J persist: 前端通知失败: {}", _fe)

            # 记录活动日志
            from app.services.activity_logger import log_activity

            await log_activity(
                agent_id=agent_id,
                action_type="chat_reply",
                summary=f"回复了LSP4J编辑器 内容: {reply_text[:80]}...",
                detail={
                    "channel": "ide_lsp4j",
                    "user_text": user_text[:200],
                    "reply": reply_text[:500],
                },
                related_id=sid_uuid,
            )

    except Exception as e:
        logger.error("LSP4J: 对话持久化失败: {}", e)


async def _persist_lsp4j_tool_call(
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str,
    tool_name: str,
    parameters: dict | None,
    results: list | str | None,
    tool_call_id: str,
    request_id: str,
    status: str = "FINISHED",
) -> None:
    """持久化单次工具调用到 DB（fire-and-forget）。

    写入 ChatMessage 表，role="tool_call"，content 为 JSON。
    供 _load_lsp4j_history_from_db 在后续请求中恢复工具调用上下文。

    内部捕获所有异常，不影响 IDE 主流程。
    """
    try:
        from app.models.audit import ChatMessage
        from datetime import datetime, timezone as tz_persist

        async with async_session() as db:
            sid_uuid = uuid.UUID(session_id)
            payload = {
                "toolCallId": tool_call_id,
                "requestId": request_id,
                "name": tool_name,
                "status": status.lower() if status else "done",
                "args": parameters or {},
                "result": str(results or "")[:500],
                "reasoning_content": "",
            }
            db.add(
                ChatMessage(
                    agent_id=agent_id,
                    user_id=user_id,
                    role="tool_call",
                    content=json.dumps(payload, ensure_ascii=False, default=str),
                    conversation_id=str(sid_uuid),
                    created_at=datetime.now(tz_persist.utc),
                )
            )
            await db.commit()
            logger.info("[LSP4J] tool_call persisted: session={} tool={}", session_id, tool_name)
    except Exception:
        logger.exception("[LSP4J] tool_call persist failed: session={} tool={}", session_id, tool_name)


def _format_tool_context_message(tool_records: list[dict]) -> dict | None:
    """将工具调用记录格式化为 system 消息，注入 LLM 上下文。

    提取搜索/文件读取/终端命令的关键信息，让 LLM 知道之前已经执行过什么，
    避免在新一轮对话中重复搜索或读取相同文件。

    Args:
        tool_records: 工具调用记录列表，每条含 name/parameters/results 字段

    Returns:
        {"role": "system", "content": "..."} 或 None（无有效上下文时）
    """
    from .context_trimmer import (
        trim_tool_context_history, MAX_TOOL_HISTORY_ROUNDS, compress_tool_context_summary,
    )

    logger.info("[LSP4J-CTX] _format_tool_context_message: processing {} tool records", len(tool_records))

    # 智能压缩: 超过 5 条记录时，用分组摘要替代逐条详情
    summary_lines = compress_tool_context_summary(tool_records)
    if summary_lines:
        logger.info(
            "[LSP4J-CTX] smart_compress: {} records → {}-line summary (recent_detail + grouped)",
            len(tool_records),
            len(summary_lines),
        )
        result_content = "\n".join(summary_lines)
        logger.info("[LSP4J-CTX] compressed context len={}", len(result_content))
        return {"role": "system", "content": result_content}

    # 首轮或记录少时: 仍用逐条详情
    tool_records = trim_tool_context_history(tool_records)

    lines = ["[会话上下文] 本轮对话之前已执行的工具调用及结果：", ""]
    skipped_old_format = 0
    skipped_no_name = 0
    formatted_count = 0
    for i, tc in enumerate(tool_records, 1):
        name = tc.get("name") or tc.get("tool_name", "")
        params = tc.get("parameters") or tc.get("arguments") or tc.get("args") or {}
        results = tc.get("results") or tc.get("result") or []
        # 日志：记录每条 tool_call 的格式特征（帮助区分新旧格式）
        record_keys = list(tc.keys())
        has_new_format = "tool_name" in tc and "tool_call_id" in tc
        has_old_format = "name" in tc and "args" in tc
        logger.info(
            "[LSP4J-CTX]   record[{}]: name={} keys=[{}] new_fmt={} old_fmt={} params_keys=[{}] results_type={}",
            i,
            name,
            ",".join(record_keys[:8]),
            has_new_format,
            has_old_format,
            ",".join(list(params.keys())[:5]) if params else "none",
            type(results).__name__ if results else "none",
        )
        if isinstance(results, str):
            try:
                results = json.loads(results)
            except (json.JSONDecodeError, TypeError):
                results = [{"content": str(results)[:500]}]
        if results is None:
            results = []
        elif not isinstance(results, list):
            results = [results]
        # 日志：记录 results 结构
        logger.info(
            "[LSP4J-CTX]   record[{}]: results_count={} first_result_type={}",
            i,
            len(results),
            type(results[0]).__name__ if results else "empty",
        )

        if name in ("search_file", "search_codebase", "grep_code", "search_symbol", "list_dir"):
            line = f"{i}. {name}"
            param_info = ""
            if name in ("search_file",):
                param_info = params.get("file_pattern", "") or params.get("query", "")
            elif name in ("search_codebase", "grep_code"):
                param_info = params.get("query", "") or params.get("regex", "")
            elif name == "search_symbol":
                param_info = params.get("query", "")
            elif name == "list_dir":
                param_info = params.get("relative_workspace_path", "")
            if param_info:
                line += f"({param_info[:120]})"
            # 提取文件名列表
            file_paths = []
            for r in results[:15]:
                if isinstance(r, dict):
                    fp = r.get("filePath", "") or r.get("file_path", "") or r.get("path", "") or r.get("fileName", "")
                    if fp:
                        file_paths.append(fp)
            if file_paths:
                line += f": 找到 {len(results)} 个结果"
                lines.append(line)
                for fp in file_paths[:10]:
                    lines.append(f"   - {fp}")
                if len(file_paths) > 10:
                    lines.append(f"   ... 还有 {len(file_paths) - 10} 个")
            else:
                line += f": {len(results)} 个结果"
                lines.append(line)

        elif name == "read_file":
            fp = params.get("file_path", "") or params.get("filePath", "") or params.get("path", "")
            if fp:
                lines.append(f"{i}. read_file: 已读取 {fp}")
            else:
                lines.append(f"{i}. read_file: 已执行")

        elif name == "run_in_terminal":
            cmd = params.get("command", "")
            lines.append(f"{i}. run_in_terminal: {cmd[:150]}")

        elif name in (
            "replace_text_by_path",
            "search_replace",
            "create_file_with_text",
            "delete_file_by_path",
            "edit_file",
            "write_file",
        ):
            fp = params.get("filePath", "") or params.get("file_path", "")
            lines.append(f"{i}. {name}: {fp}" if fp else f"{i}. {name}: 已执行")
        else:
            # 非 IDE 原生工具（如 plaza_*, duckduckgo_*），仍然记录供 LLM 参考
            param_keys = list(params.keys())[:3] if params else []
            lines.append(
                f"{i}. {name}: 已执行 (params={','.join(param_keys)})" if param_keys else f"{i}. {name}: 已执行"
            )

        formatted_count += 1

    if len(lines) <= 2:
        logger.info(
            "[LSP4J-CTX] _format_tool_context_message: NO USEFUL CONTEXT (records={} formatted={} skipped_old={} skipped_noname={})",
            len(tool_records),
            formatted_count,
            skipped_old_format,
            skipped_no_name,
        )
        return None
    lines.append("")
    lines.append(
        "以上工具已在之前的对话轮次中执行完毕。如当前问题涉及相同文件或搜索，请优先参考上述结果，避免重复调用工具。"
    )
    result_content = "\n".join(lines)
    logger.info(
        "[LSP4J-CTX] _format_tool_context_message: produced context len={} lines={} tools_mentioned={}",
        len(result_content),
        len(lines),
        formatted_count,
    )
    logger.info("[LSP4J-CTX] context preview (first 300 chars): {}", result_content[:300])
    return {"role": "system", "content": result_content}


async def _load_lsp4j_history_from_db(session_id: str, agent_id: uuid.UUID, user_id: uuid.UUID) -> list[dict]:
    """从数据库加载 LSP4J 对话历史。

    验证 session_id UUID + 所有权后返回历史消息列表。
    同时加载 tool_call 记录，注入为 system 上下文消息，
    让 LLM 知晓之前会话中已执行过的搜索/文件读取结果。

    Args:
        session_id: 会话 ID（UUID 格式字符串）
        agent_id: 智能体 UUID
        user_id: 用户 UUID

    Returns:
        历史消息列表 [{"role": "user"/"assistant"/"system", "content": "..."}]
    """
    try:
        sid_uuid = uuid.UUID(session_id)
    except ValueError:
        return []

    async with async_session() as db:
        sr = await db.execute(select(ChatSession).where(ChatSession.id == sid_uuid))
        sess = sr.scalar_one_or_none()
        if not sess or sess.user_id != user_id:
            # 允许同用户跨 Agent 访问会话历史（session_id 才是会话标识符，agent_id 可变化）
            if sess and sess.agent_id != agent_id:
                logger.info(
                    "LSP4J hydrate cross-agent: session={} request_agent={} db_agent={}",
                    session_id,
                    agent_id,
                    str(sess.agent_id),
                )
            if not sess:
                return []
            if sess.user_id != user_id:
                logger.warning(
                    "LSP4J hydrate denied (wrong user): session={} request_user={} db_user={}",
                    session_id,
                    user_id,
                    str(sess.user_id),
                )
                return []

        mr = await db.execute(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == str(sid_uuid))
            .where(ChatMessage.user_id == user_id)
            .where(ChatMessage.role.in_(("user", "assistant", "tool_call")))
            .order_by(ChatMessage.created_at.asc())
        )
        rows = mr.scalars().all()

        # 在内存中分为聊天消息和工具调用记录
        history = [{"role": m.role, "content": m.content} for m in rows if m.role in ("user", "assistant")]
        try:
            tc_rows = [m for m in rows if m.role == "tool_call"]
            logger.info(
                "[LSP4J-CTX] _load_history: session={} user_msg_count={} tool_call_db_rows={}",
                session_id,
                len(history),
                len(tc_rows),
            )
            if tc_rows:
                tool_records = []
                parse_failures = 0
                for tc in tc_rows:
                    try:
                        tool_records.append(json.loads(tc.content))
                    except (json.JSONDecodeError, TypeError) as e:
                        parse_failures += 1
                        logger.warning(
                            "[LSP4J-CTX] _load_history: JSON parse failed for tool_call id={}: {}",
                            tc.id,
                            e,
                        )
                logger.info(
                    "[LSP4J-CTX] _load_history: parsed {} tool_records, parse_failures={}",
                    len(tool_records),
                    parse_failures,
                )
                if tool_records:
                    ctx_msg = _format_tool_context_message(tool_records)
                    if ctx_msg:
                        history.insert(0, ctx_msg)
                        logger.info(
                            "[LSP4J] tool_call context injected from DB: session_id={} tool_count={}",
                            session_id,
                            len(tool_records),
                        )
                    else:
                        logger.info(
                            "[LSP4J-CTX] _load_history: context NOT injected (no useful tool records after formatting), "
                            "session={} tool_count={}",
                            session_id,
                            len(tool_records),
                        )
                else:
                    logger.info(
                        "[LSP4J-CTX] _load_history: no valid tool_records after parsing (all {} rows failed)",
                        len(tc_rows),
                    )
            else:
                logger.info(
                    "[LSP4J-CTX] _load_history: no tool_call rows found for session={}",
                    session_id,
                )
        except Exception:
            logger.exception("[LSP4J] tool_call 历史加载失败（非致命）")

        logger.info("[LSP4J] history loaded from DB: session_id={} count={}", session_id, len(history))
        return history


# ──────────────────────────────────────────────
# 模型变更广播（供模型池变更时外部调用）
# ──────────────────────────────────────────────


async def broadcast_config_refresh_models() -> int:
    """向所有活跃的 LSP4J 客户端推送 config/refreshModels 通知。

    当模型池（LLMModel）发生变更时调用，遍历所有活跃的 JsonRpcRouter 实例，
    向每个连接的 IDE 发送通知让插件 UI 刷新模型列表。

    外部调用示例（如 app/api/llm.py 模型增删改成功后）：
        from app.plugins.clawith_lsp4j.jsonrpc_router import broadcast_config_refresh_models
        asyncio.ensure_future(broadcast_config_refresh_models())

    Returns:
        成功推送的客户端数量
    """
    from .context import list_active_routers

    routers = await list_active_routers()
    if not routers:
        logger.debug("[LSP4J] broadcast config/refreshModels: 无活跃连接")
        return 0

    count = 0
    for _key, router in routers:
        try:
            await router._send_client_request("config/refreshModels", {})
            count += 1
        except Exception as e:
            logger.warning("[LSP4J] broadcast config/refreshModels 失败: key={} error={}", _key, e)

    logger.info("[LSP4J] broadcast config/refreshModels: 已推送 {} 个客户端", count)
    return count
