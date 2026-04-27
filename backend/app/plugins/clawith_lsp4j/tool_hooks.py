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
# 参数名必须严格匹配插件 ToolHandler 的取值逻辑：
#   read_file / save_file → file_path（snake_case，用 getRequestFilePathWithUnderLine）
#   save_file 不读取 content/text 参数（只调用 FileDocumentManager.saveDocument 持久化）
#   replace_text_by_path / create_file_with_text / delete_file_by_path → filePath（camelCase，用 getRequestFilePath）
#   replace_text_by_path → text（非 oldText/newText，插件直接替换整个文档内容）
#   create_file_with_text → text（getRequestText 查找 "text" 键，后端定义 "content" 需映射）
_LSP4J_IDE_TOOL_NAMES = frozenset(
    {
        "read_file",             # 读取文件（file_path）
        "save_file",             # 保存文件（file_path）
        "run_in_terminal",       # 执行终端命令（command, workDirectory?）
        "get_terminal_output",   # 获取终端输出（terminalId?）
        "replace_text_by_path",  # 文本替换（filePath, text）
        "create_file_with_text", # 创建文件（filePath, content）
        "delete_file_by_path",   # 删除文件（filePath）
        "get_problems",          # 获取代码问题（filePaths，复数数组）
        "add_tasks",             # 任务规划（纯 UI 工具，返回成功响应让插件渲染任务树）
        "todo_write",            # 待办列表（纯 UI 工具，委托给 add_tasks 渲染）
        "search_replace",        # 搜索替换（比 replace_text_by_path 更强大）
    }
)

# ★ 基础工具名 → 插件原生名的映射
# LLM 可能调用基础工具名（如 edit_file），需映射为插件 ToolInvokeProcessor 识别的名称
# 反向映射不存在：插件原生名称（如 replace_text_by_path）不需要映射回来
_TOOL_NAME_MAP = {
    "edit_file": "replace_text_by_path",    # 全文替换（非 diff）
    "create_file": "create_file_with_text", # 创建文件（LLM 可能用此名称调用）
    "write_file": "create_file_with_text",  # 创建文件（基础工具注册名）
    "delete_file": "delete_file_by_path",   # 删除文件
}

# ★ 工具参数名映射：后端工具定义 → 插件 ToolHandler 期望的参数名
# 插件各 ToolHandler 的参数名约定不一致：
#   read_file / save_file → file_path（snake_case，用 getRequestFilePathWithUnderLine）
#   create_file_with_text / delete_file_by_path → filePath（camelCase，用 getRequestFilePath）
#   create_file_with_text → text（用 getRequestText，查找 "text" 键）
# 后端工具定义中 create_file_with_text 的内容参数命名为 "content"，需映射为 "text"
_PARAM_NAME_MAP = {
    "create_file_with_text": {"content": "text"},
}

# ★ 基础工具中与 IDE 工具重名/重叠的名称，LSP4J 活跃时需过滤
# 避免向 LLM 注册两套同名工具（基础版 + IDE 版），只保留 IDE 版
_LSP4J_OVERLAP_BASE_TOOL_NAMES = frozenset({
    "edit_file", "write_file", "delete_file",
    "search_files", "find_files",
})

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
                    "file_path": {
                        "type": "string",
                        "description": "文件的绝对路径",
                    },
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_file",
            "description": "保存 IDE 编辑器中已修改的文件到磁盘。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "要保存的文件路径",
                    },
                },
                "required": ["file_path"],
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
                    "isBackground": {
                        "type": "boolean",
                        "description": "是否在后台运行（true=后台执行，不阻塞等待输出）",
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
            "description": "替换文件内容为指定文本（全文替换）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {
                        "type": "string",
                        "description": "文件路径",
                    },
                    "text": {
                        "type": "string",
                        "description": "替换后的完整文件内容（Java 转义序列会自动反转义）",
                    },
                },
                "required": ["filePath", "text"],
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
                    "filePaths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "文件路径列表（可选，不传则获取项目级别问题）",
                    },
                },
                "required": [],
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

        # ★ 工具名映射：基础工具名 → 插件原生名（如 edit_file → replace_text_by_path）
        # LSP4J 活跃时，LLM 可能调用基础工具名，需映射后才能被插件识别
        if lsp4j_ws is not None and tool_name in _TOOL_NAME_MAP:
            mapped_name = _TOOL_NAME_MAP[tool_name]
            logger.info("[LSP4J-TOOL] 工具名映射: {} → {}", tool_name, mapped_name)
            tool_name = mapped_name
            is_lsp4j_tool = tool_name in _LSP4J_IDE_TOOL_NAMES

        logger.info("[LSP4J-TOOL] execute_tool: name={} lsp4j_ws={} is_lsp4j_tool={}",
                     tool_name, lsp4j_ws is not None, is_lsp4j_tool)

        if lsp4j_ws is not None and is_lsp4j_tool:
            # ★ 参数名映射：后端工具定义 → 插件 ToolHandler 期望的参数名
            if tool_name in _PARAM_NAME_MAP:
                name_map = _PARAM_NAME_MAP[tool_name]
                args = {name_map.get(k, k): v for k, v in args.items()}
                logger.debug("[LSP4J-TOOL] 参数名映射: tool={} map={}", tool_name, name_map)

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
            # ★ 过滤掉基础工具中与 IDE 工具重名/重叠的（只保留 IDE 版本）
            tools = [t for t in tools
                     if t.get("function", {}).get("name", "") not in _LSP4J_OVERLAP_BASE_TOOL_NAMES]
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
