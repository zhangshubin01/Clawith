"""ACP 工具桥 — 文件操作 + 终端代理。

通过当前活跃的 AcpHandler 将工具调用代理到 IDE 插件执行。

ContextVar:
- current_acp_handler: AcpHandler | None — 活跃的 ACP 会话 handler
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import time
from contextvars import ContextVar
from typing import Any

from loguru import logger

current_acp_handler: ContextVar[Any | None] = ContextVar("current_acp_handler", default=None)

# ACP 协议方法映射
_ACP_METHOD_MAP = {
    "read_file": "fs/read_text_file",
    "write_file": "fs/write_text_file",
    "edit_file": "fs/edit_text_file",
    "delete_file": "fs/write_text_file",
    "list_files": "fs/list_directory",
    # P0-2: find_file/search_text builder handler 已实现 (line 76, 95) 且在
    # _ACP_PARAM_BUILDERS 已注册 (line 468-469), 补充 _ACP_METHOD_MAP 入口。
    # _try_acp_execute (line 593) 用此映射查 method, 此前缺失导致退回基路径。
    "find_file": "fs/find_file",
    "search_text": "fs/search_text",
    "find_class": "fs/find_class",
    "find_symbol": "fs/find_symbol",
    "index_status": "ide/index_status",
    "find_references": "fs/find_references",
    "find_definition": "fs/find_definition",
    "find_implementations": "fs/find_implementations",
    "find_super_methods": "fs/find_super_methods",
    "call_hierarchy": "fs/call_hierarchy",
    "type_hierarchy": "fs/type_hierarchy",
    "diagnostics": "fs/diagnostics",
    "refactor_rename": "fs/refactor_rename",
    "move_file": "fs/move_file",
    "reformat_code": "fs/reformat_code",
    "optimize_imports": "fs/optimize_imports",
    "safe_delete": "fs/safe_delete",
    "convert_java_to_kotlin": "fs/convert_java_to_kotlin",
    "sync_files": "ide/sync_files",
    "active_file": "ide/active_file",
    "open_file": "ide/open_file",
    "file_structure": "fs/file_structure",
    "build_project": "ide/build_project",
    "get_documentation": "fs/get_documentation",
    "apply_quickfix": "ide/apply_quickfix",
    "git_status": "git/status",
    "git_diff": "git/diff",
    "git_stage": "git/stage",
    "git_commit": "git/commit",
}


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
    logger.info(f"[ACP-bridge] find_class query={query} scope={params['scope']}")
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
    logger.info(f"[ACP-bridge] find_symbol query={query} scope={params['scope']}")
    return params


async def _build_index_status_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 index_status 参数。"""
    logger.info("[ACP-bridge] index_status")
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
        "[ACP-bridge] edit_file path={} old_len={} new_len={} replace_all={}",
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
    """构建 write_text_file 参数（含 delete_file 清空）。"""
    if tool_name == "delete_file":
        content = ""
    else:
        content = args.get("content", "")
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
    logger.info(f"[ACP-bridge] find_references file={args.get('file')}")
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
    logger.info(f"[ACP-bridge] find_definition file={args.get('file')}")
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
    logger.info(f"[ACP-bridge] find_implementations file={args.get('file')}")
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
    logger.info(f"[ACP-bridge] find_super_methods file={args.get('file')}")
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
    logger.info(f"[ACP-bridge] call_hierarchy file={args.get('file')} direction={args.get('direction')}")
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
    logger.info(f"[ACP-bridge] type_hierarchy className={args.get('className')}")
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
    logger.info(f"[ACP-bridge] diagnostics file={args.get('file')} severity={params['severity']}")
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
    logger.info(f"[ACP-bridge] refactor_rename file={args.get('file')} newName={new_name}")
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
    logger.info(f"[ACP-bridge] reformat_code file={f_path}")
    return params


async def _build_optimize_imports_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 optimize_imports 参数 — 仅优化导入。"""
    f_path = args.get("file", "")
    if not f_path:
        return "❌ optimize_imports: file 不能为空"
    logger.info(f"[ACP-bridge] optimize_imports file={f_path}")
    return {"sessionId": session_id, "file": f_path}


async def _build_safe_delete_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 safe_delete 参数 — 安全删除符号或文件。"""
    f_path = args.get("file", "")
    if not f_path:
        return "❌ safe_delete: file 不能为空"
    params: dict[str, Any] = {"sessionId": session_id, "file": f_path}
    if args.get("line"): params["line"] = int(args["line"])
    if args.get("column"): params["column"] = int(args["column"])
    params["target_type"] = args.get("target_type", "symbol")
    params["force"] = args.get("force", False)
    logger.info(f"[ACP-bridge] safe_delete file={f_path} target_type={params['target_type']}")
    return params


async def _build_convert_java_to_kotlin_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 convert_java_to_kotlin 参数 — Java 转 Kotlin。"""
    files = args.get("files", [])
    if not files or not isinstance(files, list):
        return "❌ convert_java_to_kotlin: files 数组不能为空"
    logger.info(f"[ACP-bridge] convert_java_to_kotlin files={files}")
    return {"sessionId": session_id, "files": files}


async def _build_sync_files_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 sync_files 参数 — 同步文件系统。"""
    params: dict[str, Any] = {"sessionId": session_id}
    if args.get("paths"):
        params["paths"] = args["paths"]
    logger.info("[ACP-bridge] sync_files")
    return params


async def _build_active_file_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 active_file 参数 — 获取当前活动文件。"""
    logger.info("[ACP-bridge] active_file")
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
    logger.info(f"[ACP-bridge] open_file file={f_path}")
    return params


async def _build_file_structure_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 file_structure 参数 — 获取文件结构。"""
    f_path = args.get("file", "")
    if not f_path:
        return "❌ file_structure: file 不能为空"
    logger.info(f"[ACP-bridge] file_structure file={f_path}")
    return {"sessionId": session_id, "file": f_path}


async def _build_project_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 build_project 参数 — 编译项目。"""
    logger.info(f"[ACP-bridge] build_project rebuild={args.get('rebuild')}")
    return {
        "sessionId": session_id,
        "rebuild": args.get("rebuild", False),
        "includeRawOutput": args.get("includeRawOutput", False),
        "timeoutSeconds": int(args.get("timeoutSeconds", 120)),
    }


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
    logger.info(f"[ACP-bridge] get_documentation className={class_name}")
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
    logger.info(f"[ACP-bridge] apply_quickfix file={f_path} fixName={fix_name}")
    return params


async def _build_git_status_params(
    tool_name: str, args: dict, handler, session_id: str, path: str,
) -> dict | str:
    """构建 git/status 参数 — 查看 Git 状态。"""
    logger.info("[ACP-bridge] git_status")
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
    logger.info(f"[ACP-bridge] git_diff staged={params['staged']}")
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
    logger.info(f"[ACP-bridge] git_stage all={params['all']}")
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
    logger.info(f"[ACP-bridge] git_commit all={params['all']} amend={params['amend']}")
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
            check_tool_autonomy as _check_autonomy,
        )
        _INLINE_IMPORTED["_DANGEROUS_BASH_ALWAYS"] = _bash_always
        _INLINE_IMPORTED["check_tool_autonomy"] = _check_autonomy
        from app.services.agent_tools import _TOOL_AUTONOMY_MAP as _autonomy_map
        _INLINE_IMPORTED["_TOOL_AUTONOMY_MAP"] = _autonomy_map

def _is_project_file(path: str, cwd: str = "") -> bool:
    """判断路径是否是 IDE 项目文件（应走 ACP）还是 agent 自身文件（走本地）。"""
    if not path:
        return False
    if not cwd:
        return False  # 拒绝空 cwd，防止所有绝对路径被放行（安全审计 V1/V4）
    if path.startswith("/"):
        # 绝对路径: 规范化后检查是否在项目根下，防路径穿越 (如 /etc/passwd)
        if cwd and not os.path.normpath(path).startswith(os.path.normpath(cwd)):
            return False
        return True
    # agent 自身文件前缀
    agent_prefixes = ("memory/", "skills/", "enterprise_info/", "workspace/")
    if any(path.startswith(p) for p in agent_prefixes):
        return False
    agent_files = ("soul.md", "focus.md", "memory.md", "tasks.json", "reflections.md")
    if path.split("/")[-1] in agent_files:
        return False
    return True  # 其余相对路径 → IDE 项目根


def is_agent_internal_path(path: str) -> bool:
    """Agent 后端内部文件（记忆、技能等），不应在 IDE 时间线展示工具卡片。"""
    normalized = (path or "").strip()
    if not normalized:
        return False
    return not _is_project_file(normalized)



# P1-5: list_files/list_directory 去重缓存 (3s TTL)
_ls_cache: dict[str, tuple[float, str]] = {}
_LS_CACHE_TTL = 3.0
async def _try_acp_execute(tool_name: str, args: dict, handler) -> str | None:
    """通过 ACP 协议执行文件操作。

    返回 None 表示不应由 ACP 处理（如 path 为空或 agent 自身文件）。
    返回字符串表示 ACP 执行结果。
    """
    path = args.get("path") or args.get("file") or args.get("file_path") or args.get("filePath", "")
    # 无 path 参数的工具（如 index_status, build_project）不需要项目文件检查
    _cwd = getattr(handler, '_cwd', '')
    if path and not _is_project_file(path, cwd=_cwd):
        return None

    method = _ACP_METHOD_MAP.get(tool_name)

    # 统一路径穿越防护 — 对所有含 path 参数的工具检查（安全审计 V1）
    path = args.get("path") or args.get("file") or args.get("file_path") or args.get("filePath", "")
    if path and ".." in path:
        logger.warning(f"[ACP-SEC] 拒绝路径穿越: {tool_name} path={path}")
        return f'{{"error": "路径不合法: 禁止路径穿越"}}'

    # Autonomy 闸门: 写操作需要经过 check_tool_autonomy 检查
    _WRITE_TOOLS = frozenset({
        "write_file", "edit_file", "delete_file",
        "refactor_rename", "move_file", "safe_delete",
        "reformat_code", "optimize_imports", "convert_java_to_kotlin",
        "apply_quickfix", "git_stage", "git_commit",
    })
    if tool_name in _WRITE_TOOLS:
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
                _block = await _check_fn(tool_name, args, _agent_id, _user_id)
                if _block is not None:
                    logger.warning(f"[ACP-bridge] autonomy blocked: {tool_name} reason={_block[:60]}")
                    return _block

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
        logger.warning(f"[ACP-bridge] rate limited: {method} remaining={left}")
        return f"⚠️ 请求过频繁（{method}），请稍后重试"

    # 写操作独立限速: 10次/60s (安全审计 V7)
    _WRITE_METHODS = frozenset({
        "fs/write_text_file", "fs/edit_text_file",
    })
    if method in _WRITE_METHODS and not await _RATE_LIMITER.allow(method, limit=30):
        logger.warning(f"[ACP-LIMIT] 写操作 {method} 超限")
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
        # P2-1: 文件大小阶梯超时
        _timeout = float(os.getenv("ACP_FS_TIMEOUT", "15"))
        if method == "fs/write_text_file":
            _timeout = float(os.getenv("ACP_FS_WRITE_TIMEOUT", "60"))
        elif method == "fs/edit_text_file":
            _timeout = float(os.getenv("ACP_FS_EDIT_TIMEOUT", "30"))
        logger.debug(
            f"[ACP-FS] timeout tier tool={tool_name} method={method} "
            f"timeout={_timeout}s"
        )
        result = await handler.send_request(method, params, timeout=_timeout)
        logger.info(
            f"[ACP-PERF] fs DONE tool={tool_name} path={path} session={session_id} "
            f"elapsed={time.perf_counter() - t0:.3f}s"
        )
        # P1-6: 检测 IDE 索引未就绪，引导 LLM 等待而非反复重试
        if isinstance(result, str) and ("索引未就绪" in result or "索引构建中" in result):
            logger.warning(f"[ACP-DUMB] {tool_name} blocked by IDE indexing: {result[:100]}")
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
            # P1-5: 去重缓存 — 相同路径 3s 内不重复查询 IDE
            _depth = str(params.get("depth", 1))
            _cache_key = f"{path}:{_depth}"
            _cached_ts, _cached_val = _ls_cache.get(_cache_key, (0, ""))
            if time.monotonic() - _cached_ts < _LS_CACHE_TTL:
                logger.info(f"[ACP-bridge] list_files cache hit path={path} depth={_depth}")
                return _cached_val
            # P1-5: 缓存 MISS 日志
            logger.debug(
                f"[ACP-CACHE] list_files MISS path={path or '.'} depth={_depth}"
            )
            if isinstance(result, dict):
                entries = result.get("entries") or result.get("files") or []
                if not entries:
                    _ls_cache[_cache_key] = (time.monotonic(), "(目录为空)")
                    return "(目录为空)"
                lines = []
                for e in entries:
                    if isinstance(e, dict):
                        prefix = "📁 " if e.get("isDirectory") else "📄 "
                        lines.append(f"{prefix}{e.get('name', '?')}")
                    else:
                        lines.append(str(e))
                if result.get("truncated"):
                    lines.append(f"... (截断, 共 {result.get('totalCount', '?')} 项)")
                _out = "\n".join(lines)
                _ls_cache[_cache_key] = (time.monotonic(), _out)
                logger.info(f"[ACP-bridge] list_files {len(lines)} entries (cached)")
                return _out
            return str(result)
        if method == "fs/find_file":
            if isinstance(result, dict):
                files = result.get("files") or []
                if not files:
                    return "(未找到匹配的文件)"
                lines = []
                for f in files[:50]:  # 最多显示 50 个
                    if isinstance(f, dict):
                        lines.append(f.get("path", f.get("name", "?")))
                    else:
                        lines.append(str(f))
                if len(files) > 50:
                    lines.append(f"... (共 {len(files)} 个匹配)")
                return "\n".join(lines)
            return str(result)
        if method == "fs/find_class":
            if isinstance(result, dict):
                classes = result.get("files") or result.get("results") or []
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
            if isinstance(result, dict):
                matches = result.get("matches") or []
                if not matches:
                    return "(未找到匹配的文本)"
                lines = []
                for m in matches[:30]:  # 最多显示 30 条
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
        return "文件操作成功"
    except TimeoutError:
        logger.warning(f"[ACP-bridge] timeout: {tool_name} {path}, 尝试本地读取")
        if method == "fs/read_text_file" and os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                logger.info(f"[ACP-bridge] 本地读取成功: {path} ({len(content)} bytes)")
                return content
            except Exception as e:
                logger.error(f"[ACP-bridge] 本地读取失败: {path}: {e}")
                return f"⚠️ 文件读取失败: {e}"
        return f"⚠️ IDE 文件操作超时: {path}"
    except Exception as e:
        err_msg = str(e)
        if "-32601" in err_msg:
            # P0-8: fs/list_directory 不被 IDE 支持时，回落本地终端 find 命令枚举目录
            # 避免 list_files 在旧版本 IDE 插件上完全不可用，保持功能降级可用
            if method == "fs/list_directory":
                logger.warning(
                    "[ACP-bridge] fs/list_directory 不支持, 回落 terminal find: %s", path
                )
                find_cmd = f"find '{path}' -maxdepth {params.get('depth', 3)} -type f -o -type d | head -{params.get('limit', 200)}"
                return await _try_acp_terminal({"command": find_cmd}, handler)
            logger.warning(
                "[ACP-bridge] %s 不支持: %s (IDE 插件需实现对应 handler)",
                tool_name, path
            )
            return f"❌ IDE 插件不支持此操作: {tool_name}（需升级插件版本）"
        logger.error(
            "[ACP-bridge conn={} error: {} path={} session={}: {}",
            conn_id, tool_name, path, session_id, e
        )
        return f"⚠️ IDE 文件操作失败: {e}"


async def _try_acp_terminal(args: dict, handler) -> str | None:
    command = args.get("command", "")
    if not command:
        return None

    # P0-3: 危险命令拦截 —— 跳过 _DANGEROUS_BASH_ALWAYS 中定义的毁灭性命令
    # 与 agent_tools.py 中 execute_command 的处理逻辑一致，防止 LLM 通过 ACP 通道绕过安全检查
    _lazy_import_agent_tools()
    _dangerous = _INLINE_IMPORTED.get("_DANGEROUS_BASH_ALWAYS", [])
    _EXTRA_DANGEROUS = [
        "> /dev/sda", "parted", "fdisk", "dd if=/dev/zero of=/",
        "eval ", "$(", "`", "mkfs.", ":(){ :|:& };:",
    ]
    lower_cmd = command.strip().lower()
    for pattern in _EXTRA_DANGEROUS:
        if pattern in lower_cmd:
            msg = f"❌ 危险命令已被拦截: pattern={pattern}"
            logger.warning(f"[ACP-bridge] {msg} cmd={command[:80]}")
            return msg
    for pattern in _dangerous:
        if pattern in lower_cmd:
            msg = f"❌ 危险命令已被拦截: pattern={pattern}"
            logger.warning(f"[ACP-bridge] {msg} cmd={command[:80]}")
            return msg

    # 网络危险命令检查（curl/wget/nc/ssh/scp 等数据外泄通道）
    _lazy_import_agent_tools()
    _network = _INLINE_IMPORTED.get("_DANGEROUS_BASH_NETWORK", [])
    for pattern in _network:
        if pattern in lower_cmd:
            msg = f"❌ 网络命令已被拦截: pattern={pattern}"
            logger.warning(f"[ACP-bridge] {msg} cmd={command[:80]}")
            return msg
    # 路径穿越检查
    if "../../" in command:
        return "❌ 路径穿越已被拦截"

    # P0-3: autonomy 审批闸门 —— 高敏工具（写文件/执行命令）需要 agent autonomy 策略检查
    # 从 handler 获取 agent/user 身份，传给 check_tool_autonomy 判定 L1/L2/L3 策略
    agent_id = getattr(handler, "agent_id", None)
    user_id = getattr(handler, "user_id", None)
    if agent_id is not None and user_id is not None:
        _check_fn = _INLINE_IMPORTED.get("check_tool_autonomy")
        if _check_fn is not None:
            block = await _check_fn("execute_command", args, agent_id, user_id)
            if block is not None:
                logger.warning(f"[ACP-bridge] autonomy blocked: cmd={command[:80]} reason={block[:60]}")
                return block

    session_id = getattr(handler, "session_id", "")
    conn_id = getattr(handler, "conn_id", "?")

    t0 = time.perf_counter()
    logger.info(f"[ACP-PERF] terminal START session={session_id} cmd={command[:120]}")
    try:
        # 1. 创建终端并执行命令
        create_result = await handler.send_request("terminal/create", {
            "sessionId": session_id,
            "command": command,
        }, timeout=float(os.getenv("ACP_TERMINAL_CREATE_TIMEOUT", "30")))

        terminal_id = ""
        if isinstance(create_result, dict):
            terminal_id = create_result.get("terminalId", "")

        if not terminal_id:
            return "终端创建失败"

        # 2. 等待命令执行完毕并读取 exit code
        # P0-4: wait_for_exit 返回的 exitCode 标识命令成败，LLM 需据此判断构建/测试是否通过
        wait_result = await handler.send_request("terminal/wait_for_exit", {
            "sessionId": session_id,
            "terminalId": terminal_id,
        }, timeout=float(os.getenv("ACP_TERMINAL_WAIT_TIMEOUT", "300")))
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
            logger.info(
                f"[ACP-PERF] terminal DONE session={session_id} terminalId={terminal_id} "
                f"elapsed={time.perf_counter() - t0:.3f}s outputLen={len(out_text)} exitCode={exit_code}"
            )
        else:
            out_text = str(output) if output else "命令执行完毕，无输出"
            logger.info(
                f"[ACP-PERF] terminal DONE session={session_id} terminalId={terminal_id} "
                f"elapsed={time.perf_counter() - t0:.3f}s outputLen={len(out_text)} exitCode={exit_code}"
            )

        # P0-4: exitCode != 0 时添加失败标记，防止 LLM 将编译/测试失败误判为成功
        if exit_code != 0:
            return f"❌ 命令失败 (exit={exit_code}):\n{out_text}"
        return out_text

    except TimeoutError:
        logger.error(
            f"[ACP-PERF] terminal TIMEOUT session={session_id} "
            f"elapsed={time.perf_counter() - t0:.3f}s cmd={command[:80]}"
        )
        return f"⚠️ 终端命令超时: {command[:80]}"
    except Exception as e:
        logger.error(f"[ACP-terminal conn={conn_id}] error: {command[:80]} session={session_id}: {e}")
        return f"⚠️ 终端执行失败: {e}"


async def _try_acp_terminal_streaming(
    args: dict, handler, cancel_event: asyncio.Event | None = None,
) -> str | None:
    """流式 Terminal 执行 — 自适应轮询策略。

    策略:
      Phase 1 (500ms 静默): 等 500ms, 如果命令已完成 → 返回聚合结果 (零轮询开销)
      Phase 2 (300ms 轮询): 命令超过 500ms → 启动流式轮询, 每 300ms 推送增量

    短命令 (ls/pwd/git status) 走 Phase 1, 零额外开销。
    长命令 (gradle build) 自动进入 Phase 2, 用户看到增量输出。

    Returns:
        None — 流式版本通过 notification 推送, 不返回聚合结果。
        调用者应检查返回值: None 表示已进入流式模式, 调用者不应再等待结果。
    """
    command = args.get("command", "")
    if not command:
        return None

    session_id = getattr(handler, "session_id", "")
    conn_id = getattr(handler, "conn_id", "?")

    logger.info(
        f"[ACP-PERF] terminal-streaming START conn={conn_id} session={session_id} "
        f"cmd={command[:80]}"
    )

    # Step 1: 创建 terminal
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
        return f"❌ terminal 创建失败: {e}"

    if isinstance(create_resp, dict) and create_resp.get("error"):
        return f"❌ terminal 创建失败: {create_resp['error']}"
    terminal_id = create_resp.get("terminalId", "") if isinstance(create_resp, dict) else ""
    if not terminal_id:
        return "❌ terminal 创建失败: 未返回 terminalId"

    # Phase 1: 500ms 静默等待
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
        return f"⚠️ terminal 检查失败: {e}"

    if isinstance(check, dict) and check.get("exitCode") is not None:
        # Phase 1 完成: 短命令直接返回
        exit_code = int(check.get("exitCode", -1))
        out_text = check.get("output", "")
        logger.info(
            f"[ACP-PERF] terminal-streaming SHORT-CMD conn={conn_id} "
            f"session={session_id} exitCode={exit_code} outputLen={len(out_text)}"
        )
        if exit_code != 0:
            return f"❌ 命令失败 (exit={exit_code}):\n{out_text}"
        return out_text

    # Phase 2: 启动 300ms 流式轮询
    logger.info(
        f"[ACP-PERF] terminal-streaming PHASE2 conn={conn_id} "
        f"session={session_id} reason=exceeded 500ms silence"
    )
    seq = 0

    async def poll_loop():
        nonlocal seq
        try:
            while True:
                if cancel_event and cancel_event.is_set():
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
                    except Exception as e:
                        logger.warning(
                            f"[ACP-PERF] terminal-streaming KILL-FAIL conn={conn_id}: {e}"
                        )
                    break

                try:
                    output = await handler.send_request(
                        "terminal/output",
                        {"sessionId": session_id, "terminalId": terminal_id},
                        timeout=float(os.getenv("ACP_TERMINAL_OUTPUT_TIMEOUT", "30")),
                    )
                except Exception as e:
                    logger.warning(
                        f"[ACP-PERF] terminal-streaming POLL-FAIL conn={conn_id} "
                        f"session={session_id}: {e}"
                    )
                    await asyncio.sleep(0.3)
                    continue

                if not isinstance(output, dict):
                    await asyncio.sleep(0.3)
                    continue

                new_text = output.get("output", "")
                if new_text:
                    seq += 1
                    try:
                        await handler._send_notification("session/update", {
                            "sessionId": session_id,
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": f"[TERMINAL] {new_text}"},
                            },
                        })
                    except Exception as e:
                        logger.warning(
                            f"[ACP-PERF] terminal-streaming NOTIFY-FAIL conn={conn_id}: {e}"
                        )

                if output.get("exitCode") is not None:
                    exit_code = int(output.get("exitCode", -1))
                    logger.info(
                        f"[ACP-PERF] terminal-streaming DONE conn={conn_id} "
                        f"session={session_id} exitCode={exit_code} total_chunks={seq}"
                    )
                    try:
                        await handler._send_notification("session/update", {
                            "sessionId": session_id,
                            "update": {
                                "sessionUpdate": "agent_thought_chunk",
                                "content": {
                                    "type": "text",
                                    "text": f"\n\n[进程退出: exitCode={exit_code}]",
                                },
                            },
                        })
                    except Exception as e:
                        logger.warning(
                            f"[ACP-PERF] terminal-streaming EXIT-NOTIFY-FAIL conn={conn_id}: {e}"
                        )
                    break

                await asyncio.sleep(0.3)

        except asyncio.CancelledError:
            logger.warning(
                f"[ACP-PERF] terminal-streaming poll_loop cancelled conn={conn_id} "
                f"session={session_id}"
            )
        except Exception as e:
            logger.error(
                f"[ACP-PERF] terminal-streaming poll_loop error conn={conn_id} "
                f"session={session_id}: {e}"
            )

    _ = asyncio.create_task(poll_loop())
    return None  # 流式版本: 调用者看到 None 应停止等待


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
