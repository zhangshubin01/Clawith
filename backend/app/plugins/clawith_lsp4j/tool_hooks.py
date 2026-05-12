"""LSP4J 工具调用钩子 — 执行路径 + 注册路径。

扩展 ACP 已安装的 _custom_execute_tool 和 _custom_get_tools，
增加 LSP4J ContextVar 判断，使两条通道（ACP + LSP4J）共存。

关键设计：
1. LSP4J 工具名使用**插件原生名称**（read_file, run_in_terminal 等），
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
from .tool_constants import LSP4J_IDE_TOOL_NAMES, TOOL_NAME_MAP

# ──────────────────────────────────────────────
# 插件原生工具名称（基于 ToolInvokeProcessor.java 源码验证）
# ──────────────────────────────────────────────

# 插件识别这些工具名，不支持 ide_ 前缀
# 参数名必须严格匹配插件 ToolHandler 的取值逻辑：
#   read_file → file_path（snake_case，用 getRequestFilePathWithUnderLine）
#   read_file 不读取 content/text 参数
#   replace_text_by_path / create_file_with_text / delete_file_by_path → filePath（camelCase，用 getRequestFilePath）
#   replace_text_by_path → text（非 oldText/newText，插件直接替换整个文档内容）
#   create_file_with_text → text（getRequestText 查找 "text" 键，后端定义 "content" 需映射）
# 工具名称映射直接使用导入的常量（tool_constants.LSP4J_IDE_TOOL_NAMES / TOOL_NAME_MAP）

# ★ 工具参数名映射：后端工具定义 → 插件 ToolHandler 期望的参数名
# 插件各 ToolHandler 的参数名约定不一致：
#   read_file → file_path（snake_case，用 getRequestFilePathWithUnderLine）
#   create_file_with_text / delete_file_by_path → filePath（camelCase，用 getRequestFilePath）
#   create_file_with_text → text（用 getRequestText，查找 "text" 键）
# 后端工具定义中 create_file_with_text 的内容参数命名为 "content"，需映射为 "text"
_PARAM_NAME_MAP = {
    "create_file_with_text": {"content": "text"},
}

# ★ 本地回退参数名映射：IDE 工具参数名 → 基础工具参数名
# 当 LSP4J 工具因路径为相对路径而回退到本地执行时，
# 需要将 IDE 工具定义的参数名映射为基础工具期望的参数名。
# 例如：IDE read_file 用 file_path，但本地 read_file 用 path。
_LOCAL_FALLBACK_PARAM_MAP = {
    "read_file": {"file_path": "path"},
    "list_dir": {"relative_workspace_path": "path"},
}

# ★ 基础工具中与 IDE 工具重名/重叠的名称，LSP4J 活跃时需过滤
# 避免向 LLM 注册两套同名工具（基础版 + IDE 版），只保留 IDE 版
_LSP4J_OVERLAP_BASE_TOOL_NAMES = frozenset({
    "edit_file", "write_file", "delete_file",
    "search_files", "find_files",
    "create_file",
    "read_file", "list_files",
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
            "description": "读取文件内容。支持两种路径：绝对路径（如 /Users/xxx/project/src/Main.java）读取 IDE 项目文件；相对路径（如 soul.md, memory/memory.md）读取 Agent 自身文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "文件路径。绝对路径（/开头）读取 IDE 项目文件；相对路径仅用于 Agent 自身文件（如 soul.md, focus.md, memory/memory.md, skills/xxx）",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "起始行号（0-indexed，默认 0），用于分页读取大文件",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最大读取行数（默认 2000），用于分页读取大文件",
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
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出指定目录的内容（文件和子目录）。支持两种路径：绝对路径（如 /Users/xxx/project/src/）列出 IDE 项目目录；相对路径或空字符串列出 Agent 自身目录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "relative_workspace_path": {
                        "type": "string",
                        "description": "目录路径。绝对路径（/开头）列出 IDE 项目目录；相对路径或空字符串列出 Agent 自身目录",
                    },
                },
                "required": ["relative_workspace_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_file",
            "description": "搜索指定目录下的文件（支持 glob 模式匹配）。用于查找文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索查询或文件模式（如 *.py, test*.js），用于 scope label 显示",
                    },
                    "file_pattern": {
                        "type": "string",
                        "description": "glob 文件匹配模式（如 **/*.py, *Test.java）",
                    },
                    "path": {
                        "type": "string",
                        "description": "搜索起始路径（默认为工作区根目录）",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_code",
            "description": "使用正则表达式在项目文件中搜索代码内容。用于精确的模式匹配搜索。",
            "parameters": {
                "type": "object",
                "properties": {
                    "regex": {
                        "type": "string",
                        "description": "正则表达式模式（如 'class\\s+\\w+', 'import\\s+.*'）",
                    },
                },
                "required": ["regex"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_codebase",
            "description": "在项目代码库中搜索指定文本内容。用于关键词和文本片段搜索。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词或文本片段",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_symbol",
            "description": "在项目中搜索类、函数等代码符号。Android 项目中文件名即类名，按文件名匹配。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "符号名称（类名/函数名）",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_tasks",
            "description": "在 IDE 任务面板中创建任务列表，用于规划多步骤任务并跟踪进度。任务树会在 IDE 侧边栏中渲染为可折叠的树形 UI。",
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "summary": {
                                    "type": "string",
                                    "description": "任务摘要（简短标题）",
                                },
                                "description": {
                                    "type": "string",
                                    "description": "任务详细描述",
                                },
                            },
                            "required": ["summary"],
                        },
                        "description": "要显示在 IDE 任务面板中的任务列表",
                    },
                },
                "required": ["tasks"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo_write",
            "description": (
                "在 IDE 中写入待办事项列表。与 add_tasks 类似，将任务内容渲染为可折叠的树形 UI。"
                "每完成一项后必须调用 update_tasks：taskId 与列表顺序一致时为字符串 \"1\"..\"N\"（与未显式 id 的条目对应），status 如 completed/in_progress。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "summary": {
                                    "type": "string",
                                    "description": "待办事项摘要",
                                },
                                "description": {
                                    "type": "string",
                                    "description": "待办事项详细描述",
                                },
                            },
                            "required": ["summary"],
                        },
                        "description": "要显示的待办事项列表",
                    },
                },
                "required": ["tasks"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_replace",
            "description": "在文件中搜索指定文本并替换。适用于精确的文本搜索替换操作。注意：这会将搜索和替换文本发送到 IDE 执行，比 replace_text_by_path（全文替换）更适合局部修改。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {
                        "type": "string",
                        "description": "要修改的文件路径（绝对路径）",
                    },
                    "searchText": {
                        "type": "string",
                        "description": "要搜索的文本内容",
                    },
                    "replaceText": {
                        "type": "string",
                        "description": "替换后的文本内容",
                    },
                },
                "required": ["filePath", "searchText", "replaceText"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": "将 unified diff patch 应用到 IDE 项目文件中。用于精确的代码修改操作。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {
                        "type": "string",
                        "description": "要修改的文件路径（绝对路径）",
                    },
                    "patch": {
                        "type": "string",
                        "description": "unified diff 格式的 patch 内容",
                    },
                },
                "required": ["filePath", "patch"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_file",
            "description": "保存当前在 IDE 中打开的文件到磁盘。请在确认编辑内容无误后调用此工具，以便 IDE 将缓冲区内容写入文件系统。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {
                        "type": "string",
                        "description": "要保存的文件路径（绝对路径）",
                    },
                },
                "required": ["filePath"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_tasks",
            "description": "更新 IDE 任务面板中的任务状态（标记完成、修改标题等）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "taskId": {
                        "type": "string",
                        "description": "要更新的任务 ID",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed", "cancelled"],
                        "description": "新状态",
                    },
                    "title": {
                        "type": "string",
                        "description": "新标题（可选，不传则保持原标题）",
                    },
                },
                "required": ["taskId"],
            },
        },
    },
]

# ──────────────────────────────────────────────
# 路径判断辅助函数
# ──────────────────────────────────────────────

# ★ 涉及文件路径读取/写入的工具（需判断路径是否指向 IDE 项目）
_LSP4J_FILE_PATH_TOOLS = frozenset({
    "read_file",
    "replace_text_by_path",
    "create_file_with_text",
    "delete_file_by_path",
    "save_file",
})

# Agent 内部文件 — 始终在本地后端执行，不路由到 IDE
_AGENT_INTERNAL_FILE_NAMES = frozenset({
    "soul.md", "focus.md", "tasks.json", "memory.md",
})

# Agent 内部目录前缀 — 始终在本地后端执行
# ⚠️ "workspace/" 不在此列表：LLM 常用 workspace/xxx 路径操作项目文件，
# 此时应路由到 IDE 以便文件变更直接体现在用户项目中。
# Agent 内部文件（soul.md 等）由 _AGENT_INTERNAL_FILE_NAMES 单独保护。
_AGENT_INTERNAL_DIR_PREFIXES = ("memory/", "skills/", "enterprise_info/")


def _extract_file_path(tool_name: str, args: dict) -> str | None:
    """从工具参数中提取文件路径。

    LLM 可能使用不同的参数名调用同一工具：
      - filePath  (camelCase) — 插件原生协议
      - file_path (snake_case) — ACP 基础工具
      - path                  — LLM 常用，最终兜底
    """
    return args.get("filePath") or args.get("file_path") or args.get("path")


def _is_agent_internal_path(file_path: str) -> bool:
    """判断路径是否属于 Agent 内部文件（应在本地执行，不路由到 IDE）。"""
    basename = file_path.split("/")[-1]
    if basename in _AGENT_INTERNAL_FILE_NAMES:
        return True
    for prefix in _AGENT_INTERNAL_DIR_PREFIXES:
        if file_path.startswith(prefix):
            return True
    return False


def _should_route_to_ide(tool_name: str, args: dict) -> bool:
    """判断文件工具调用是否应路由到 IDE 插件。

    规则：
    1. 非文件工具始终路由
    2. 绝对路径（/ 开头）→ IDE
    3. Agent 内部文件（soul.md, memory.md, workspace/xxx, skills/xxx）→ 本地
    4. 其余相对路径 → IDE（由 invoke_tool_on_ide 解析为绝对路径）
    """
    if tool_name not in _LSP4J_FILE_PATH_TOOLS:
        return True

    file_path = _extract_file_path(tool_name, args)
    if not file_path:
        logger.debug("[LSP4J-TOOL] {} 未提供文件路径，回退到本地执行", tool_name)
        return False

    # 绝对路径 → IDE
    if file_path.startswith("/"):
        return True

    # Agent 内部文件 → 本地
    if _is_agent_internal_path(file_path):
        logger.info("[LSP4J-TOOL] {} Agent 内部路径，本地执行: path={}",
                    tool_name, file_path)
        return False

    # 其余相对路径 → IDE（由 invoke_tool_on_ide 拼接 project_path）
    logger.info("[LSP4J-TOOL] {} 相对路径路由到 IDE: path={}",
                tool_name, file_path)
    return True


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
        1. 若 current_lsp4j_ws 活跃 且 tool_name 在 LSP4J_IDE_TOOL_NAMES 中 → 走 LSP4J 路径
        2. 否则走 ACP 原路径（ACP 的降级处理兜底：双 ContextVar 均 None 时返回中文提示）
        """
        lsp4j_ws = current_lsp4j_ws.get()
        is_lsp4j_tool = tool_name in LSP4J_IDE_TOOL_NAMES

        # ★ 工具名映射：基础工具名 → 插件原生名（如 edit_file → replace_text_by_path）
        # LSP4J 活跃时，LLM 可能调用基础工具名，需映射后才能被插件识别
        if lsp4j_ws is not None and tool_name in TOOL_NAME_MAP:
            mapped_name = TOOL_NAME_MAP[tool_name]
            logger.info("[LSP4J-TOOL] 工具名映射: {} → {}", tool_name, mapped_name)
            tool_name = mapped_name
            is_lsp4j_tool = tool_name in LSP4J_IDE_TOOL_NAMES

        logger.info("[LSP4J-TOOL] execute_tool: name={} lsp4j_ws={} is_lsp4j_tool={}",
                     tool_name, lsp4j_ws is not None, is_lsp4j_tool)

        # ★ 路径判断：相对路径 → agent 工作空间文件（如 focus.md），走本地执行
        # 绝对路径 → IDE 项目文件，走 LSP4J 路由
        # 解决 read_file 同时被注册为基础工具和 IDE 工具时，
        # LLM 用相对路径读 agent 工作空间文件却被误路由到 IDE 插件的问题
        _should_route = _should_route_to_ide(tool_name, args)
        if lsp4j_ws is not None and is_lsp4j_tool and _should_route:
            # ★ 参数校验：read_file 的 file_path 不能为空
            if tool_name == "read_file":
                file_path = args.get("file_path") or args.get("filePath")
                if not file_path or not str(file_path).strip():
                    logger.warning("[LSP4J-TOOL][read_file] file_path is blank, args={}, tool_name={}", args, tool_name)
                    return '{"success": false, "error": "file_path parameter is required and cannot be blank", "errorCode": "INVALID_PARAMETER"}'
                # 清理路径前后空格
                if isinstance(file_path, str):
                    args["file_path"] = file_path.strip()
                    logger.info("[LSP4J-TOOL][read_file] file_path trimmed: {}", args["file_path"])

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

                # 截断过长的工具结果，防止 context 膨胀导致 LLM 推理延迟飙升
                # 终端输出用更小的上限（2000），因其重复性高、信息密度低
                from .context_trimmer import trim_tool_result, MAX_TOOL_RESULT_CHARS, MAX_TERMINAL_RESULT_CHARS

                _max_chars = MAX_TERMINAL_RESULT_CHARS if tool_name == "run_in_terminal" else MAX_TOOL_RESULT_CHARS
                if isinstance(result, str) and len(result) > _max_chars:
                    result = trim_tool_result(result, tool_name, max_chars=_max_chars)

                logger.info("[LSP4J-TOOL] 结果: tool={} result_len={}", tool_name, len(result) if result else 0)
                return result
            except Exception as e:
                # LSP4J 工具调用异常（只记录不吞异常，继续向上传播）
                logger.exception("[LSP4J-TOOL] LSP4J 工具调用异常: tool={} error={}", tool_name, e)
                raise

        # ACP 原路径（或 LSP4J 工具回退到本地执行）
        # ★ 本地回退参数名映射：IDE 工具参数名 → 基础工具参数名
        # 例如 read_file 的 file_path → path，list_dir 的 relative_workspace_path → path
        if is_lsp4j_tool and tool_name in _LOCAL_FALLBACK_PARAM_MAP:
            fallback_map = _LOCAL_FALLBACK_PARAM_MAP[tool_name]
            args = {fallback_map.get(k, k): v for k, v in args.items()}
            logger.debug("[LSP4J-TOOL] 本地回退参数映射: tool={} map={}", tool_name, fallback_map)
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

    # ★ 关键修复：caller.py 使用了 from import 本地引用，模块加载时已绑定旧函数。
    # agent_tools 模块属性替换不影响 caller.py 的本地引用，需显式修补。
    try:
        import app.services.llm.caller as _caller_mod
        _caller_mod.execute_tool = _lsp4j_aware_execute_tool  # type: ignore[attr-defined]
        _caller_mod.get_agent_tools_for_llm = _lsp4j_aware_get_tools  # type: ignore[attr-defined]
        logger.info("[LSP4J-TOOL] patched caller module local references")
    except Exception as _patch_e:
        logger.warning("[LSP4J-TOOL] failed to patch caller module: {}", _patch_e)

    _installed = True
    logger.info("[LSP4J-TOOL] tool hooks installed (wrapping ACP hooks)")
