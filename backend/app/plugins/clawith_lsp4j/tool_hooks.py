"""LSP4J 工具调用钩子 — 执行路径 + 注册路径。

扩展 ACP 已安装的 _custom_execute_tool 和 _custom_get_tools，
增加 LSP4J ContextVar 判断，使两条通道（ACP + LSP4J）共存。

关键设计：
1. LSP4J 工具名使用**插件原生名称**（read_file, save_file 等），
   不使用 ACP 的 ide_ 前缀名称（ide_read_file 等）。
   原因：插件 ToolInvokeProcessor 只识别 8 个原生名称，
   发送 ide_read_file 会触发 default 分支返回 "tool not support yet"。

2. 必须同时补丁执行路径和注册路径：
   - 执行路径：_lsp4j_aware_execute_tool — IDE 工具调用路由到 LSP4J
   - 注册路径：_lsp4j_aware_get_tools — LLM 看到 IDE 工具定义
   缺一不可：若只补丁执行路径不补丁注册路径，LLM 看不到 IDE 工具，永远不会调用。

3. 安装时机：install_lsp4j_tool_hooks() 在 __init__.py 的 register() 中调用，
   晚于 ACP 的模块级导入安装（router.py:854），保证获取到 ACP 的引用。
"""

from __future__ import annotations

import uuid

from loguru import logger

from app.services import agent_tools

from .context import current_lsp4j_ws

# ──────────────────────────────────────────────
# 插件原生工具名称（基于 ToolInvokeProcessor.java 源码验证）
# ──────────────────────────────────────────────

# 插件识别这些工具名，不支持 ide_ 前缀
# add_tasks: 灵码插件原生支持（ToolTypeEnum.java:56），AddTasksToolDetailPanel 渲染任务树
_LSP4J_IDE_TOOL_NAMES = frozenset(
    {
        "read_file",             # 读取文件（对应 ACP ide_read_file）
        "save_file",             # 保存文件（对应 ACP ide_write_file）
        "run_in_terminal",       # 执行终端命令（对应 ACP ide_execute_command）
        "get_terminal_output",   # 获取终端输出（对应 ACP ide_terminal_output）
        "replace_text_by_path",  # 文本替换（插件独有）
        "create_file_with_text", # 创建文件（对应 ACP ide_mkdir，语义不同）
        "delete_file_by_path",   # 删除文件（对应 ACP delete_file）
        "get_problems",          # 获取代码问题（插件独有）
        "add_tasks",             # 任务规划工具（灵码 AddTasksToolDetailPanel 渲染任务树）
    }
)

# ──────────────────────────────────────────────
# LSP4J IDE 工具定义（OpenAI function-calling 格式）
# ──────────────────────────────────────────────

# 使用插件原生名称，不复用 ACP 的 IDE_TOOLS
# 参数格式需匹配 ToolInvokeRequest.parameters 的实际字段
_LSP4J_IDE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取 IDE 本地文件系统中的文件内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {
                        "type": "string",
                        "description": "文件的绝对路径或相对路径",
                    },
                },
                "required": ["filePath"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_file",
            "description": "将内容保存到 IDE 本地文件系统中的文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {
                        "type": "string",
                        "description": "要保存的文件路径",
                    },
                    "content": {
                        "type": "string",
                        "description": "要写入的文件内容",
                    },
                },
                "required": ["filePath", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_in_terminal",
            "description": "在 IDE 终端中执行命令。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的终端命令",
                    },
                    "workDirectory": {
                        "type": "string",
                        "description": "工作目录（可选）",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_terminal_output",
            "description": "获取终端命令的输出结果。",
            "parameters": {
                "type": "object",
                "properties": {
                    "terminalId": {
                        "type": "string",
                        "description": "终端 ID",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_text_by_path",
            "description": "替换文件中的指定文本内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {
                        "type": "string",
                        "description": "文件路径",
                    },
                    "oldText": {
                        "type": "string",
                        "description": "要替换的原始文本",
                    },
                    "newText": {
                        "type": "string",
                        "description": "替换后的新文本",
                    },
                },
                "required": ["filePath", "oldText", "newText"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_file_with_text",
            "description": "创建新文件并写入内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {
                        "type": "string",
                        "description": "要创建的文件路径",
                    },
                    "content": {
                        "type": "string",
                        "description": "文件内容",
                    },
                },
                "required": ["filePath", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file_by_path",
            "description": "删除 IDE 本地文件系统中的文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {
                        "type": "string",
                        "description": "要删除的文件路径",
                    },
                },
                "required": ["filePath"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_problems",
            "description": "获取 IDE 中当前项目的代码问题（错误、警告等）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {
                        "type": "string",
                        "description": "文件路径（可选，不传则获取项目级别问题）",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_tasks",
            "description": "创建多步骤任务计划。灵码插件会渲染为可视化的任务树 UI。",
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "description": "任务列表",
                        "maxItems": 50,
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {
                                    "type": "string",
                                    "description": "任务标题",
                                    "maxLength": 200,
                                },
                                "description": {
                                    "type": "string",
                                    "description": "任务描述",
                                    "maxLength": 2000,
                                },
                                "status": {
                                    "type": "string",
                                    "description": "任务状态: pending/in_progress/completed",
                                    "enum": ["pending", "in_progress", "completed"],
                                },
                            },
                            "required": ["title"],
                        },
                    },
                },
                "required": ["tasks"],
            },
        },
    },
]

# ──────────────────────────────────────────────
# 钩子安装
# ──────────────────────────────────────────────

_installed = False


def install_lsp4j_tool_hooks() -> None:
    """安装 LSP4J 工具钩子（idempotent）。

    在 __init__.py 的 register() 中调用，晚于 ACP 的模块级安装。
    获取 ACP 已安装的 _custom_execute_tool / _custom_get_tools 引用，
    包裹增强后替换为 LSP4J 感知版本。
    """
    global _installed
    if _installed:
        # 钩子已安装，跳过
        logger.debug("[LSP4J-TOOL] 工具钩子已安装，跳过")
        return

    # 获取当前 ACP 已安装的钩子引用
    # ACP 在模块导入时调用 install_acp_tool_hooks()（router.py:854），
    # 此时 agent_tools.execute_tool 和 get_agent_tools_for_llm 已被替换为 ACP 版本
    acp_execute_tool = agent_tools.execute_tool
    acp_get_tools = agent_tools.get_agent_tools_for_llm

    # 定义增强版钩子
    async def _lsp4j_aware_execute_tool(
        tool_name: str,
        args: dict,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        session_id: str = "",
    ) -> str:
        """LSP4J 感知的工具执行路由。

        优先级：
        1. 若 current_lsp4j_ws 活跃 且 tool_name 在 _LSP4J_IDE_TOOL_NAMES 中 → 走 LSP4J 路径
        2. 否则走 ACP 原路径（ACP 的降级处理兜底：双 ContextVar 均 None 时返回中文提示）
        """
        lsp4j_ws = current_lsp4j_ws.get()
        is_lsp4j_tool = tool_name in _LSP4J_IDE_TOOL_NAMES
        logger.info("[LSP4J-TOOL] execute_tool: name={} lsp4j_ws={} is_lsp4j_tool={}",
                     tool_name, lsp4j_ws is not None, is_lsp4j_tool)

        if lsp4j_ws is not None and is_lsp4j_tool:
            # LSP4J 路径：通过 WebSocket 调用 IDE 端工具
            logger.info("[LSP4J-TOOL] 走 LSP4J 路径: tool={} args={}", tool_name,
                         {k: (v[:50] + "...") if isinstance(v, str) and len(v) > 50 else v
                          for k, v in args.items()})
            try:
                from .jsonrpc_router import invoke_lsp4j_tool
                result = await invoke_lsp4j_tool(tool_name, args, agent_id, user_id)
                logger.info("[LSP4J-TOOL] 结果: tool={} result_len={}", tool_name, len(result) if result else 0)
                return result
            except Exception as e:
                # LSP4J 工具调用异常（只记录不吞异常，继续向上传播）
                logger.exception("[LSP4J-TOOL] LSP4J 工具调用异常: tool={} error={}", tool_name, e)
                raise

        # ACP 原路径
        logger.debug("[LSP4J-TOOL] 走 ACP 路径: tool={}", tool_name)
        return await acp_execute_tool(tool_name, args, agent_id, user_id, session_id)

    async def _lsp4j_aware_get_tools(agent_id: uuid.UUID) -> list[dict]:
        """LSP4J 感知的工具注册路由。

        优先级：
        1. 若 current_lsp4j_ws 活跃 → 返回基础工具 + _LSP4J_IDE_TOOLS（插件原生名称）
        2. 否则走 ACP 原路径（ACP 的 _custom_get_tools 会在 current_acp_ws 活跃时追加 IDE_TOOLS）
        """
        lsp4j_ws = current_lsp4j_ws.get()
        if lsp4j_ws is not None:
            # LSP4J 活跃：使用插件原生名称的工具定义
            tools = await acp_get_tools(agent_id)
            ide_tool_names = [t["function"]["name"] for t in _LSP4J_IDE_TOOLS]
            logger.info("[LSP4J-TOOL] 注册工具: base_count={} ide_tools={}", len(tools), ide_tool_names)
            return tools + _LSP4J_IDE_TOOLS

        # ACP 原路径
        return await acp_get_tools(agent_id)

    # 替换 agent_tools 中的引用
    agent_tools.execute_tool = _lsp4j_aware_execute_tool
    agent_tools.get_agent_tools_for_llm = _lsp4j_aware_get_tools

    _installed = True
    logger.info("[LSP4J-TOOL] tool hooks installed (wrapping ACP hooks)")
