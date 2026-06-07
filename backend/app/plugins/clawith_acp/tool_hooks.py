"""ACP 工具桥钩子 — 参考 LSP4J tool_hooks.py 模式。

直接包裹 agent_tools.execute_tool 和 get_agent_tools_for_llm，
通过 ContextVar(current_acp_handler) 在 ACP 会话中将工具路由到 IDE 插件执行。

三层钩子：
1. 执行路径：_acp_aware_execute_tool — 文件/终端工具走 ACP WebSocket
2. 注册路径：_acp_aware_get_tools — LLM 看到 ACP 代理工具定义
3. caller 模块修补 — 防止 from import 本地引用绕过钩子
"""

from __future__ import annotations

import time
import uuid

from loguru import logger

from app.services import agent_tools
from .tool_bridge import current_acp_handler

# ──────────────────────────────────────────────
# ACP 代理工具定义（替换基础工具中同名者）
# ──────────────────────────────────────────────

_ACP_IDE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "读取 IDE 项目文件内容。\n"
                "路径: 绝对路径（如 /Users/xxx/project/src/Main.kt）\n"
                "      相对路径（如 src/Main.kt）相对于项目根目录。\n"
                "支持 line/limit 参数分页读取。\n"
                "⚠️ Agent 自身文件（memory/、skills/ 前缀、soul.md/focus.md）不使用此工具。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径（绝对或相对项目根）"},
                    "line": {"type": "integer", "description": "起始行（1-indexed）"},
                    "limit": {"type": "integer", "description": "最大行数（默认 2000）"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "创建或覆盖 IDE 项目文件。\n"
                "局部修改优先用 edit_file（参数完全相同）。\n"
                "路径: 绝对路径或相对项目根目录的相对路径。\n"
                "⚠️ Agent 自身文件（memory/、skills/ 前缀）不使用此工具。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "content": {"type": "string", "description": "文件完整内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "编辑 IDE 项目文件（全量替换）。\n"
                "对长文件做局部修改: 先 read_file 读取全文 → 修改局部 → 调 edit_file 写回。\n"
                "路径: 绝对路径或相对项目根目录的相对路径。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "content": {"type": "string", "description": "修改后的文件完整内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": (
                "删除 IDE 项目文件。\n"
                "路径: 绝对路径或相对项目根目录的相对路径。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_command",
            "description": (
                "在 IDE 项目根目录执行终端命令。\n"
                "✅ 构建: ./gradlew build / assembleDebug\n"
                "✅ 测试: ./gradlew test\n"
                "✅ Git: git log / diff / status / show\n"
                "✅ 搜索: grep -r PATTERN src/\n"
                "❌ 禁止 cat/head/tail/less → 请用 read_file\n"
                "❌ 禁止 find/ls -R → 上方项目文件列表已包含文件树"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的 shell 命令"},
                    "requires_approval": {"type": "boolean", "description": "是否需要用户审批（默认 false）"},
                },
                "required": ["command"],
            },
        },
    },
]

# ACP 活跃时过滤基础工具中与 ACP 代理工具重名的
_ACP_OVERLAP_BASE_TOOL_NAMES = frozenset({
    "read_file", "write_file", "edit_file", "delete_file",
    "find_files", "search_files", "list_files",  # 系统提示替代
    "tree",                                      # 系统提示替代
    "execute_bash",                              # 替换为 execute_command
})

# 无关工具：IDE 编辑场景不需要的工具
_ACP_IRRELEVANT_TOOL_NAMES = frozenset({
    "convert_csv_to_xlsx",
    "convert_html_to_pdf",
    "convert_html_to_pptx",
    "convert_markdown_to_docx",
    "convert_markdown_to_pdf",
    "plaza_add_comment",
    "plaza_create_post",
    "plaza_create_reply",
    "generate_image",
    "generate_audio",
    "generate_video",
    "convert_pdf_to_text",
    "convert_docx_to_markdown",
})


# ──────────────────────────────────────────────
# 钩子安装
# ──────────────────────────────────────────────

_installed = False


def install_acp_tool_hooks() -> None:
    """安装 ACP 工具钩子（idempotent）。

    包裹 agent_tools.execute_tool 和 get_agent_tools_for_llm，
    在 ACP 活跃时路由到 IDE 插件。
    """
    global _installed
    if _installed:
        logger.debug("[ACP-HOOKS] 工具钩子已安装，跳过")
        return

    _base_execute_tool = agent_tools.execute_tool
    _base_get_tools = agent_tools.get_agent_tools_for_llm

    # ── 工具名映射 ──
    _ACP_TOOL_MAP = {
        "read_file": "fs/read_text_file",
        "write_file": "fs/write_text_file",
        "edit_file": "fs/write_text_file",
        "delete_file": "fs/write_text_file",  # 暂通过 write 空内容实现
    }

    async def _acp_aware_execute_tool(
        tool_name: str,
        args: dict,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        session_id: str = "",
        on_output=None,
    ) -> str:
        """ACP 感知的工具执行路由。

        优先级：
        1. ACP handler 活跃 + 文件工具 → ACP WebSocket
        2. ACP handler 活跃 + terminal 工具 → ACP terminal
        3. 其余 → 基础入口
        """
        handler = current_acp_handler.get()
        route_t0 = time.perf_counter()

        if handler is not None:
            # 文件工具 → ACP
            if tool_name in _ACP_TOOL_MAP:
                from .tool_bridge import _try_acp_execute as _acp_file_exec
                result = await _acp_file_exec(tool_name, args, handler)
                if result is not None:
                    logger.info(
                        "[ACP-HOOKS] route=acp tool={} path={} elapsed={:.3f}s",
                        tool_name,
                        args.get("path", ""),
                        time.perf_counter() - route_t0,
                    )
                    return result

            # terminal 工具 → ACP
            if tool_name in ("execute_command", "bash"):
                from .tool_bridge import _try_acp_terminal as _acp_term_exec
                result = await _acp_term_exec(args, handler)
                if result is not None:
                    logger.info(
                        "[ACP-HOOKS] route=acp tool={} cmd={} elapsed={:.3f}s",
                        tool_name,
                        str(args.get("command", ""))[:80],
                        time.perf_counter() - route_t0,
                    )
                    return result

        # 回落基础入口
        logger.info(
            "[ACP-HOOKS] route=local tool={} elapsed={:.3f}s session={}",
            tool_name,
            time.perf_counter() - route_t0,
            session_id,
        )
        return await _base_execute_tool(
            tool_name, args, agent_id, user_id, session_id, on_output=on_output
        )

    async def _acp_aware_get_tools(agent_id: uuid.UUID) -> list[dict]:
        """ACP 感知的工具注册路由。"""
        handler = current_acp_handler.get()
        if handler is not None:
            tools = await _base_get_tools(agent_id)
            # 过滤重名基础工具
            tools = [t for t in tools
                     if t.get("function", {}).get("name", "") not in _ACP_OVERLAP_BASE_TOOL_NAMES]
            # 过滤无关工具
            tools = [t for t in tools
                     if t.get("function", {}).get("name", "") not in _ACP_IRRELEVANT_TOOL_NAMES]
            ide_names = [t["function"]["name"] for t in _ACP_IDE_TOOLS]
            logger.info("[ACP-HOOKS] 注册工具: base_count={} acp_tools={}", len(tools), ide_names)
            return tools + _ACP_IDE_TOOLS
        return await _base_get_tools(agent_id)

    # 替换 agent_tools 属性
    agent_tools.execute_tool = _acp_aware_execute_tool
    agent_tools.get_agent_tools_for_llm = _acp_aware_get_tools

    # 修补 caller.py 本地引用
    try:
        import app.services.llm.caller as _caller_mod
        _caller_mod.execute_tool = _acp_aware_execute_tool  # type: ignore[attr-defined]
        _caller_mod.get_agent_tools_for_llm = _acp_aware_get_tools  # type: ignore[attr-defined]
        logger.info("[ACP-HOOKS] patched caller module local references")
    except Exception as _patch_e:
        logger.warning("[ACP-HOOKS] failed to patch caller module: {}", _patch_e)

    _installed = True
    logger.info("[ACP-HOOKS] tool hooks installed (wrapping agent_tools base entry)")
