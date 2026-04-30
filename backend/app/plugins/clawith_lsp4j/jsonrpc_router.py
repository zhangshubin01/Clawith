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
"""

from __future__ import annotations

import asyncio
import json
import os
import time
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
from app.services import agent_tools
from app.services.llm.caller import call_llm
import app.api.websocket as ws_module

from .context import (
    current_lsp4j_ws,
    current_lsp4j_pending_tools,
    current_lsp4j_message_history,
    current_lsp4j_session_id,
    current_lsp4j_user_id,
    current_lsp4j_agent_id,
    get_active_router,
    list_active_routers,
)
from .lsp_protocol import LSPBaseProtocolParser, ParseError
from .tool_constants import (
    LSP4J_IDE_TOOL_NAMES,
    TOOL_DISPLAY_NAME_MAP,
    TOOL_NAME_MAP,
)
from .workspace_file_service import WorkspaceFileService

# ──────────────────────────────────────────────
# LSP4J IDE 工具名称（与 tool_hooks._LSP4J_IDE_TOOL_NAMES 保持一致）
# ──────────────────────────────────────────────
_LSP4J_IDE_TOOL_NAMES = LSP4J_IDE_TOOL_NAMES

# LSP4J 文件编辑工具（需要 filePath 转换 + fileId 注入）
# 这些工具在 invoke_tool_on_ide 中统一处理参数转换和 results 注入
_LSP4J_FILE_EDIT_TOOLS = frozenset({
    "replace_text_by_path",
    "search_replace",
    "create_file_with_text",
    "delete_file_by_path",
})

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

# ★ 基础工具名 → 插件原生名的映射（与 tool_hooks._TOOL_NAME_MAP 保持同步）
# LLM 可能调用基础工具名（如 edit_file），需映射为插件 ToolInvokeProcessor 识别的名称
_TOOL_NAME_MAP = TOOL_NAME_MAP

# ★ 插件原生名 → 用户友好名（反向映射，用于 markdown 块显示）
# 当 LLM 直接调用插件原生名称（如 replace_text_by_path）时，
# 插件 ToolTypeEnum.getByToolName() 不识别插件原生名，返回 UNKNOWN，
# 导致文件工具分支不进入、无 diff 卡片。因此 markdown 块必须使用 LLM 侧名称。
_TOOL_DISPLAY_NAME_MAP = TOOL_DISPLAY_NAME_MAP

# ──────────────────────────────────────────────
# 本地工具执行（不在 IDE 端，由后端直接执行）
# ──────────────────────────────────────────────

def _execute_local_tool(tool_name: str, arguments: dict) -> tuple[str, list[dict]]:
    """本地执行 list_dir / search_file，返回 (result_json_str, results_list).

    results_list 格式匹配插件期望：
    - list_dir → DirItem[] (fileName, fileCount, fileSize, type, path)
      插件 ListDirToolDetailPanel.refreshToolDetailPanel() 从 results 反序列化为 List<DirItem>
      点击 DirItem 可打开文件或导航到目录
    - search_file → FileItem[] (fileName, path)
      插件 SearchFileToolDetailPanel.refreshToolDetailPanel() 从 results 反序列化为 List<FileItem>
      点击 FileItem 可打开文件
    """
    from pathlib import Path

    if tool_name == "list_dir":
        # LLM 可能使用 relative_workspace_path 或 path 参数
        rel_path = arguments.get("relative_workspace_path") or arguments.get("path") or "."
        ws_path = Path(rel_path)
        if not ws_path.is_absolute():
            ws_path = Path.cwd() / rel_path
        items: list[dict] = []
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
                    items.append({
                        "fileName": entry.name,
                        "fileCount": "",
                        "fileSize": file_size,
                        "type": "directory" if is_dir else "file",
                        "path": str(entry.absolute()),
                    })
        except PermissionError:
            logger.warning("[LSP4J-TOOL] list_dir 权限不足: path={}", ws_path)
        result_str = json.dumps(items, ensure_ascii=False, default=str)
        return result_str, items

    elif tool_name == "search_file":
        pattern = arguments.get("file_pattern", "") or arguments.get("query", "") or "*"
        search_path = arguments.get("path", ".")
        search_dir = Path(search_path)
        if not search_dir.is_absolute():
            search_dir = Path.cwd() / search_path
        items: list[dict] = []
        try:
            if search_dir.exists() and search_dir.is_dir():
                matched = list(search_dir.rglob(pattern)) if pattern else []
                matched = matched[:50]
                for p in sorted(matched):
                    try:
                        if p.is_file():
                            items.append({
                                "fileName": p.name,
                                "path": str(p.absolute()),
                            })
                    except OSError:
                        pass
        except PermissionError:
            logger.warning("[LSP4J-TOOL] search_file 权限不足: path={}", search_dir)
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


import re

def _sanitize_lang(lang: str) -> str:
    """清洗编程语言标识符，防止 Markdown 代码围栏注入。

    只允许字母、数字、+、-、.，并限制长度。
    """
    return re.sub(r'[^a-zA-Z0-9+\-.]', '', lang)[:20]


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
    if params.chatTask in ("CODE_GENERATE_COMMENT", "OPTIMIZE_CODE", "INLINE_EDIT", "DESCRIPTION_GENERATE_CODE", "CODE_PROBLEM_SOLVE"):
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

        # 图片上传缓存: request_id → (image_url, base64_data, timestamp)
        self._image_cache: dict[str, tuple[str, str, float]] = {}
        self._image_cache_max_size: int = 10
        self._image_cache_ttl: float = 600.0  # 10 分钟
        self._image_cleanup_task: asyncio.Task | None = None

        # tool/call/results 历史缓存（按 sessionId 分桶，Clawith-only 内存实现）。
        self._tool_call_history_by_session: dict[str, list[dict[str, Any]]] = {}
        self._tool_call_history_limit: int = 200

        # 已取消请求的 RPC ID 集合（防止超时后迟达响应干扰，OrderedDict 保证 FIFO）
        self._cancelled_requests: dict[int, None] = {}
        self._MAX_CANCELLED_REQUESTS_SIZE: int = 100

        # 工作区文件服务（diff 卡片支持）
        self._ws_file_service = WorkspaceFileService()

    # ──────────────────────────────────────────
    # 主路由入口
    # ──────────────────────────────────────────

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
            if "method" not in msg:
                logger.info("[LSP4J ←] response: id={} has_result={} has_error={}",
                             msg.get("id"), "result" in msg, "error" in msg)
                await self._handle_response(msg)
                return

        method = msg.get("method", "")
        params = msg.get("params", {})
        msg_id = msg.get("id")

        # 协议追踪日志：记录收到的每条请求/通知
        logger.info("[LSP4J ←] method={} id={} params_keys={}", method, msg_id,
                     list(params.keys()) if isinstance(params, dict) else type(params).__name__)

        # 2. 核心方法路由
        handler = self._METHOD_MAP.get(method)
        if handler:
            await handler(self, params, msg_id)
            return

        # 3. 非核心方法通用处理（插件 JsonNotification / LSP 标准，无业务实现，静默忽略以免 -32601）
        if method in (
            "initialized",               # 生命周期通知，无需响应
            "textDocument/didOpen",       # 文档同步（通义灵码自动发送，忽略）
            "textDocument/didChange",
            "textDocument/didClose",
            "textDocument/didSave",
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

        # 4. 未知方法：如果有 id 则返回 method-not-found 错误
        if msg_id is not None:
            await self._send_error_response(
                msg_id, -32601, f"Method not found: {method}"
            )
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
        else:
            logger.warning("[LSP4J-LIFE] projectPath 未能提取: rootUri={} workspaceFolders={}",
                           root_uri[:80] if root_uri else "(empty)",
                           [f.get("uri", "")[:80] for f in (params.get("workspaceFolders") or [])])

        await self._send_response(msg_id, {
            "capabilities": {
                "textDocumentSync": {"openClose": True, "change": 1},
                "completionProvider": {"resolveProvider": False, "triggerCharacters": ["."]},
            },
            "serverInfo": {"name": "Clawith LSP4J", "version": "0.1.0"},
        })

    async def _ensure_project_path_ready(self, msg_id: Any, method: str) -> bool:
        """确保 projectPath 已就绪，防止空 projectPath 导致跨事件路由异常。"""
        if self._project_path:
            return True
        message = (
            "projectPath is empty; initialize with rootUri/workspaceFolders before chat."
        )
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
        if self._closed:
            logger.warning("[LSP4J] chat/ask rejected: connection closed")
            return

        # 解析参数（兼容旧版插件缺少字段）
        ask = ChatAskParam(**{k: v for k, v in params.items() if k in ChatAskParam.__dataclass_fields__})

        request_id = ask.requestId or str(uuid.uuid4())
        session_id = ask.sessionId
        question_text = (
            (ask.questionText or "").strip()
            or (str(ask.chatContext or "")).strip()
        )
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

        logger.info("[LSP4J] chat/ask 开始处理: requestId={} sessionId={} stream={} chatTask={} mode={}",
                     request_id, session_id, ask.stream, ask.chatTask, ask.mode)

        # 并发保护：同一连接只允许一个 chat/ask 同时执行
        if self._chat_lock.locked():
            # 并发请求拒绝
            logger.warning("[LSP4J] chat/ask rejected: concurrent request, requestId={} current={}", request_id, self._current_request_id)
            await self._send_error_response(msg_id, -32602, "Another chat is in progress")
            return

        async with self._chat_lock:
            # 记录当前请求 ID（后续推送消息需要携带）
            self._current_request_id = request_id
            _ask_start = time.monotonic()  # 计时起点

            # 设置 session_id
            if session_id:
                self._session_id = session_id
                current_lsp4j_session_id.set(session_id)

                # 自动生成会话标题（取用户消息前 40 字符，替换换行为空格）
                auto_title = question_text[:40].replace("\n", " ").strip()
                if auto_title:
                    await self._send_session_title_update(session_id, auto_title)

            # 创建 cancel 事件
            self._cancel_event = asyncio.Event()

            # ★ 重置 toolCallId 队列（新请求开始时清空）
            self._tool_call_id_queue = []

            # ★ 创建 snapshot 并发送 snapshot/syncAll（diff 卡片支持）
            self._ws_file_service.get_or_create_snapshot(session_id, request_id)
            try:
                await self._send_snapshot_sync_all(session_id)
            except Exception as e:
                logger.warning("[WS-FILE] snapshot/syncAll 发送失败（非致命）: {}", e)

            # ★ 立即返回 JSON-RPC 响应，避免 IDE 端 LSP4J 框架请求超时
            # chat/ask 是 @JsonRequest，LSP4J 框架会等待响应；但 call_llm 可能运行数分钟，
            # 所以必须先返回响应确认收到请求，后续内容通过通知推送。
            # 这与 commitMsg/generate 的模式一致。
            await self._send_response(msg_id, {
                "isSuccess": True,
                "requestId": request_id,
                "status": "processing",
            })

            # 1. 发送思考状态（ChatThinkingParams 格式）
            await self._send_chat_think(session_id, "思考中...", "start", request_id)

            # 2. 从数据库回填历史消息
            message_history: list[dict] = current_lsp4j_message_history.get() or []
            if session_id and not message_history:
                loaded = await _load_lsp4j_history_from_db(
                    session_id, self._agent_id, self._user_id
                )
                if loaded:
                    message_history = loaded
                    current_lsp4j_message_history.set(message_history)
            # 历史消息加载完成
            logger.debug("[LSP4J] history loaded: {} messages, session_id={}", len(message_history), session_id)

            # 拼接用户消息
            full_text = question_text
            if chat_context and chat_context != question_text:
                full_text = f"{question_text}\n\n[附加上下文]\n{chat_context}"

            message_history.append({"role": "user", "content": full_text})

            # 3. 定义流式回调
            reply_parts: list[str] = []
            thinking_chunks: list[str] = []
            _thinking_started: bool = False

            # ★ 流式输出缓冲：按“完整行”发送；Markdown 表格按整块发送，避免被拆成半截列。
            _line_buffer = ""
            _ready_segments: list[str] = []
            _table_lines: list[str] = []
            _table_mode = False
            _BUFFER_THRESHOLD = 200  # 非表格文本累计阈值
            _TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")
            _TABLE_SEPARATOR_RE = re.compile(r"^\s*\|\s*[:\-\| ]+\|\s*$")

            def _is_table_line(line: str) -> bool:
                return bool(_TABLE_LINE_RE.match(line))

            def _is_valid_table_block(lines: list[str]) -> bool:
                return len(lines) >= 2 and any(_TABLE_SEPARATOR_RE.match(ln) for ln in lines[1:])

            def _drain_ready_text(max_len_only: bool = False) -> str:
                if not _ready_segments:
                    return ""
                if max_len_only:
                    acc = []
                    total = 0
                    while _ready_segments and total < _BUFFER_THRESHOLD:
                        seg = _ready_segments[0]
                        if total > 0 and total + len(seg) > _BUFFER_THRESHOLD:
                            break
                        acc.append(_ready_segments.pop(0))
                        total += len(seg)
                    return "".join(acc)
                text = "".join(_ready_segments)
                _ready_segments.clear()
                return text

            def _flush_table_lines() -> None:
                nonlocal _table_lines
                if not _table_lines:
                    return
                if _is_valid_table_block(_table_lines):
                    _ready_segments.append("".join(f"{ln}\n" for ln in _table_lines))
                else:
                    for ln in _table_lines:
                        _ready_segments.append(f"{ln}\n")
                _table_lines = []

            async def _flush_buffer(force: bool = False) -> None:
                nonlocal _line_buffer, _table_mode, _table_lines
                if force:
                    if _line_buffer:
                        # 结束阶段若尾部是未换行的残片，也要一并送出。
                        if _table_mode:
                            _table_lines.append(_line_buffer)
                            _flush_table_lines()
                            _table_mode = False
                        else:
                            _ready_segments.append(_line_buffer)
                        _line_buffer = ""
                    if _table_mode:
                        _flush_table_lines()
                        _table_mode = False
                    payload = _drain_ready_text(max_len_only=False)
                    if payload:
                        await self._send_chat_answer(session_id, payload, request_id)
                        logger.debug("[LSP4J] buffer flushed (force): text_len={}", len(payload))
                    return

                while "\n" in _line_buffer:
                    line, _line_buffer = _line_buffer.split("\n", 1)
                    if _table_mode:
                        if _is_table_line(line):
                            _table_lines.append(line)
                            continue
                        _flush_table_lines()
                        _table_mode = False
                        if line == "":
                            _ready_segments.append("\n")
                        elif _is_table_line(line):
                            _table_mode = True
                            _table_lines = [line]
                        else:
                            _ready_segments.append(f"{line}\n")
                    else:
                        if _is_table_line(line):
                            _table_mode = True
                            _table_lines = [line]
                        else:
                            _ready_segments.append(f"{line}\n")

                payload = _drain_ready_text(max_len_only=True)
                if payload:
                    await self._send_chat_answer(session_id, payload, request_id)
                    logger.debug("[LSP4J] buffer flushed: text_len={}", len(payload))

            async def on_chunk(text: str) -> None:
                """流式文本回调 — 推送 chat/answer（ChatAnswerParams 格式）

                使用缓冲区累积小 chunk，按行或阈值发送，避免 markdown 表格被拆分成单个字符，
                导致 MarkdownStreamPanel 无法正确解析。
                """
                nonlocal _thinking_started, _line_buffer
                # 思考结束标记：收到首个正文 chunk 即表示思考阶段结束
                if _thinking_started:
                    _thinking_started = False
                    await self._send_chat_think(session_id, "", "end", request_id)

                # 检查取消事件
                if self._cancel_event and self._cancel_event.is_set():
                    logger.info("[LSP4J] on_chunk: cancelled by chat/stop, chunks_sent={}", len(reply_parts))
                    raise asyncio.CancelledError("chat/stop requested")

                reply_parts.append(text)

                # ★ 缓冲逻辑：累积 chunk，按行或阈值发送
                if not getattr(self, "_stream_mode", True):
                    # 非流式模式：不发送，在 finish 中一次性返回
                    return

                _line_buffer += text
                # 仅在形成完整行时切分发送；表格由 _table_mode 聚合成整块再发。
                if "\n" in text or len(_line_buffer) >= _BUFFER_THRESHOLD:
                    await _flush_buffer()

            async def on_thinking(text: str) -> None:
                """推理过程回调 — 推送 chat/think 通知

                不再通过 chat/answer 发送 think markdown 块，
                仅使用 chat/think 通知控制 UI "思考中"状态。
                """
                nonlocal _thinking_started
                thinking_chunks.append(text)
                if not _thinking_started:
                    _thinking_started = True
                    await self._send_chat_think(session_id, "思考中...", "start", request_id)

            async def on_tool_call(data: dict) -> None:
                """工具调用回调 — 推送状态通知给 IDE + step callback + toolCall markdown"""
                status = data.get("status", "running")
                tool_name = data.get("name", "unknown")
                if status == "running":
                    # ★ 应用工具名映射（与 tool_hooks._TOOL_NAME_MAP 保持一致）
                    # LLM 可能调用基础工具名（如 edit_file），需映射为插件原生名称（如 replace_text_by_path）
                    # 映射后队列中的名称与 invoke_tool_on_ide 收到的一致，保证匹配
                    original_name = tool_name
                    tool_name = _TOOL_NAME_MAP.get(tool_name, tool_name)
                    if tool_name != original_name:
                        logger.debug("[LSP4J] on_tool_call 工具名映射: {} → {}", original_name, tool_name)

                    # ★ 显示名映射：插件 ToolTypeEnum.getByToolName() 不识别插件原生名称，
                    # 只识别 LLM 侧名称（如 edit_file）。markdown 块必须用 LLM 侧名称。
                    # 当 LLM 直接调用插件原生名（如 replace_text_by_path）时，反向映射为显示名。
                    display_name = _TOOL_DISPLAY_NAME_MAP.get(original_name, original_name)

                    # ★ 只对 LSP4J IDE 工具生成 toolCallId 并入队（供后续 invoke_tool_on_ide 按序匹配）
                    # 非IDE 工具不需要 toolCallId，因为不走 tool/call/sync 通道
                    is_lsp4j_tool = tool_name in _LSP4J_IDE_TOOL_NAMES
                    tool_call_id = ""
                    if is_lsp4j_tool:
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
                        # read_file/save_file 的链接渲染依赖 parameters["file_path"]，
                        # 但模型常传 "path"；这里统一补齐给 ToolPanel 用。
                        if tool_name in ("read_file", "save_file"):
                            fp = params.get("file_path") or params.get("path") or params.get("filePath")
                            if fp and "file_path" not in params:
                                params["file_path"] = fp
                        self._tool_params[tool_call_id] = params
                        llm_call_id = data.get("call_id", "")
                        if llm_call_id:
                            self._call_id_to_tool_id[llm_call_id] = tool_call_id
                        logger.debug("[LSP4J] toolCallId 入队: name={} callId={} queue_len={}",
                                     tool_name, tool_call_id[:8], len(self._tool_call_id_queue))
                        logger.info("[LSP4J-TOOL] toolCall 入队: display={} mapped={} callId={} queue_len={}",
                                    display_name, tool_name, tool_call_id[:8], len(self._tool_call_id_queue))
                        logger.info(
                            "[LSP4J-TRACE] 入队 trace=req={} call={} logical={} invoke={}",
                            request_id[:8],
                            tool_call_id[:8],
                            display_name,
                            tool_name,
                        )

                    if is_lsp4j_tool:
                        # ★ 发送 toolCall markdown 块（双通道之 markdown 通道）
                        # 插件 MarkdownStreamPanel 解析此格式创建工具卡片 UI
                        # 插件正则：```([\w#+.-]+)::([^\n]+)::([^\n]+)\n+(.*?)```
                        # group(1)=toolCall, group(2)=name, group(3)=toolCallId
                        # ⚠️ 关键：markdown 块仅用于创建工具卡片，不在这里发送 INIT/FINISHED。
                        # 状态流转统一走 tool/call/sync，避免 UI 出现 “list_files FINISHED” 刷屏。
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

                        logger.info("[LSP4J-TOOL] 准备发送 toolCall markdown 块: mapped={} callId={}",
                                     tool_name, tool_call_id[:8])
                        logger.info("[LSP4J-TOOL] markdown 块使用显示名: name={} callId={}",
                                    display_name, tool_call_id[:8])
                        if getattr(self, "_stream_mode", True):
                            await self._send_chat_answer(session_id, markdown_block, request_id)
                            logger.info("[LSP4J-TOOL] toolCall markdown 块已发送: callId={}", tool_call_id[:8])
                        else:
                            logger.info("[LSP4J-TOOL] 非流式模式，跳过 toolCall markdown 块发送: callId={}", tool_call_id[:8])

                        # ★ 等待插件 UI 线程处理 markdown 块、注册 toolPanel（500ms）
                        # 问题：markdown 块（chat/answer 通道）需经 Swing UI 线程渲染后，
                        # ToolMarkdownComponent 才调用 registerPanel。而 tool/call/sync（LSP4J
                        # 事件通道）被 ChatToolEventProcessor 的消费线程立即处理，此时
                        # toolPanel 若未注册，会进入 wait toolPanel 循环（每1s重试，最多60s）。
                        # 实测 Swing UI 渲染延迟约 300-1000ms，500ms 覆盖大部分场景。
                        await asyncio.sleep(0.5)

                        # 发送 PENDING sync（双通道之事件通道）
                        # 插件 ChatToolEventProcessor 收到后更新卡片参数（scopeLabel 依赖 parameters）
                        # ★ 使用 LLM 侧名称 + snake_case 参数
                        # ToolPanel 存储 sync 中的 toolName 用于后续 FINISHED 时判断是否为文件工具，
                        # 同时 parameters["file_path"] 用于 constructFileItem() 渲染文件链接。
                        await self._send_tool_call_sync(
                            session_id, request_id, tool_call_id,
                            "PENDING", tool_name=original_name, parameters=params,
                        )

                    await self._send_process_step_callback(
                        session_id, request_id,
                        step=f"tool_{tool_name}", description=f"正在执行: {tool_name}",
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
                    tool_name = _TOOL_NAME_MAP.get(tool_name, tool_name)
                    if tool_name != original_name:
                        logger.debug("[LSP4J] on_tool_call done 工具名映射: {} → {}", original_name, tool_name)

                    # ★ IDE 工具：FINISHED sync 已由 invoke_tool_on_ide 发送（携带实际执行结果和 fileId），
                    # 跳过此处的重复 FINISHED，避免：1) 空结果覆盖 invoke_tool_on_ide 的真实结果；
                    # 2) invoke_tool_on_ide 已从队列 pop 后此处匹配失败导致新建兜底 UUID（两个不同 callId）。
                    is_lsp4j_tool = tool_name in _LSP4J_IDE_TOOL_NAMES
                    if is_lsp4j_tool:
                        logger.debug("[LSP4J] on_tool_call done: 跳过 IDE 工具的 FINISHED sync, "
                                     "已由 invoke_tool_on_ide 发送, original={} mapped={}",
                                     original_name, tool_name)
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
                                    logger.debug("[LSP4J] toolCallId 队列匹配 (done): name={} callId={}",
                                                 tool_name, finished_call_id[:8])
                                    break

                        done_params = self._tool_params.pop(finished_call_id, {}) if finished_call_id else {}

                        if not finished_call_id:
                            # ★ 没有匹配的 toolCallId → 没有对应的 ToolPanel
                            # 不能发送 FINISHED sync：若使用随机 UUID，ChatToolEventProcessor
                            # 会为每个幽灵事件等待 10s（timeoutWaitPanel），堵塞所有后续工具事件。
                            logger.warning("[LSP4J] toolCallId 未匹配 (done)，跳过 FINISHED sync: name={}",
                                           tool_name)
                        else:
                            await self._send_tool_call_sync(
                                session_id, request_id, finished_call_id,
                                "FINISHED", tool_name=original_name, parameters=done_params,
                            )

                    await self._send_process_step_callback(
                        session_id, request_id,
                        step=f"tool_{tool_name}", description=f"已完成: {tool_name}",
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
                                content=json.dumps({
                                    "name": tool_name,
                                    "args": data.get("args"),
                                    "status": "done",
                                    "result": (data.get("result") or "")[:500],
                                    "reasoning_content": data.get("reasoning_content"),
                                }, ensure_ascii=False),
                                agent_id=self._agent_id,
                                user_id=self._user_id,
                            )
                            _tc_db.add(tc_msg)
                            await _tc_db.commit()
                            logger.debug("[LSP4J] tool_call 持久化成功: tool={} sessionId={}", tool_name, session_id)
                    except Exception as _tc_e:
                        logger.warning("[LSP4J] tool_call 持久化失败: tool={} sessionId={} error={}", tool_name, session_id, _tc_e)

            # 4. 调用 call_llm
            # 构建 IDE 环境提示（chatTask、codeLanguage 等注入 role_description）
            ide_prompt = _build_lsp4j_ide_prompt(ask)
            role_desc = getattr(self._agent_obj, "system_prompt", "") or ""
            if ide_prompt:
                role_desc = role_desc + ide_prompt
            # 注入工具可用性提示和项目路径
            tool_hint = "\n[工具可用性] 已连接本地 IDE 环境，可直接使用 read_file、replace_text_by_path、save_file、run_in_terminal、get_terminal_output、create_file_with_text、delete_file_by_path、get_problems 等工具访问项目文件。"
            if self._project_path:
                tool_hint += f"\n[项目根路径] {self._project_path}"
            
            # ★ 优先工具链路：文件改动应通过 IDE 工具调用触发 ToolPanel / diff 卡片。
            # CODE_EDIT_BLOCK 仅作为兜底（当工具调用明确不可用时才允许）。
            tool_hint += (
                "\n[执行策略] 对于读写文件、创建文件、删除文件、搜索、终端命令，必须优先使用工具调用。"
                "\n优先使用 read_file / replace_text_by_path / create_file_with_text / save_file / delete_file_by_path / list_dir / search_file / run_in_terminal。"
                "\n不要先输出大段“开始重构/步骤说明”代码块再改文件，优先直接执行工具并回传结果。"
                "\n目标展示应为工具卡片链路（toolCall + tool/call/sync + workingSpaceFile/sync），而不是纯 markdown 代码块。"
                "\n仅当工具调用不可用或明确失败时，才允许输出 CODE_EDIT_BLOCK 作为兜底。"
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
                session_id, request_id,
                step="step_start", description="开始处理", status="doing",
            )

            cancelled = False
            error_status_code = 200  # 默认成功
            try:
                reply = await call_llm(
                    model=model_obj,
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
                )
            except asyncio.CancelledError:
                cancelled = True
                reply = ""
                # 用户取消不是服务端错误，statusCode 仍为 200
            except Exception as e:
                logger.exception("LSP4J call_llm error")
                error_status_code = 500
                reply = f"[错误] {type(e).__name__}: {str(e)[:200]}"

            # 发送思考完成状态
            await self._send_chat_think(session_id, "", "done", request_id)

            # ★ 兜底发送思考结束通知（修复：只输出 thinking 不输出正文时思考状态不结束）
            # 场景：某些模型（如 DeepSeek-R1）纯思考拒绝回答、工具调用后直接结束、无正文输出
            # 原因：若 on_chunk 永不调用则思考结束通知永远不会发送
            # 判断依据：_thinking_started=True 表示思考阶段未结束
            if _thinking_started:
                _thinking_started = False
                await self._send_chat_think(session_id, "", "end", request_id)

            # 发送步骤结束通知（chat/process_step_callback）
            await self._send_process_step_callback(
                session_id, request_id,
                step="step_end", description="处理完成", status="done",
            )

            logger.info("[LSP4J] chat/ask 处理完成: requestId={} cancelled={} reply_len={} elapsed={:.1f}s",
                         request_id, cancelled, len(reply), time.monotonic() - _ask_start)

            # 5. 后台持久化
            if session_id and reply:
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
                _lsp4j_background_tasks.add(_t)
                _t.add_done_callback(_lsp4j_background_tasks.discard)

            # 更新消息历史
            message_history.append({"role": "assistant", "content": reply})
            current_lsp4j_message_history.set(message_history)

            # ★ 刷新缓冲区，确保所有累积的文本都已发送
            await _flush_buffer(force=True)

            # 6. 发送完成信号（ChatFinishParams 格式）
            # statusCode 映射：200=成功, 200=取消(非错误), 500=异常
            finish_reason = "cancelled" if cancelled else ("success" if error_status_code == 200 else "error")
            await self._send_chat_finish(session_id, finish_reason, reply, request_id, status_code=error_status_code)

            # 发送 REQUEST_FINISHED 清理通知（tool/call/sync）
            await self._send_tool_call_sync(
                session_id, request_id, "",
                "REQUEST_FINISHED",
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
        logger.info("[LSP4J] chat/stop: requestId={} cancel_set={}", params.get("requestId", ""), self._cancel_event.is_set() if self._cancel_event else False)
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
        logger.debug("[LSP4J] preCompletion: requestId={} triggerMode={}",
                     params.get("requestId", ""), params.get("triggerMode", ""))
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
            import json
            result = {
                "success": True,
                "message": f"{tool_name} 工具调用成功",
                "tool_name": tool_name,
                "parameters": parameters,
            }
            await self._send_response(msg_id, {
                "requestId": request_id,
                "errorCode": None,  # 成功时必须为 null，不能是 ""
                "errorMessage": None,
                "result": result,
            })
            logger.info("[LSP4J] tool/invoke: {} 纯 UI 工具，返回成功响应", tool_name)
            return
        
        if tool_name == "search_replace":
            # 降级为 replace_text_by_path
            logger.info("[LSP4J] tool/invoke: search_replace 降级为 replace_text_by_path")
            tool_name = "replace_text_by_path"
            # 参数转换
            if "searchText" in parameters and "replaceText" in parameters:
                parameters = {
                    "filePath": parameters.get("filePath", ""),
                    "text": parameters.get("replaceText", ""),
                }
        
        # 正常工具调用：通过 invoke_tool_on_ide 发送到 IDE
        try:
            result = await self.invoke_tool_on_ide(tool_name, parameters)
            await self._send_response(msg_id, {
                "requestId": request_id,
                "errorCode": None,  # 成功时必须为 null，不能是 ""
                "errorMessage": None,
                "result": result,
            })
            logger.info("[LSP4J] tool/invoke: {} 调用成功", tool_name)
        except Exception as e:
            logger.exception("[LSP4J] tool/invoke: {} 调用失败", tool_name)
            await self._send_response(msg_id, {
                "requestId": request_id,
                "errorCode": "TOOL_INVOKE_FAILED",  # 错误时保留错误码
                "errorMessage": str(e),
                "result": None,
            })
    
    
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
            logger.info("[LSP4J-TOOL] 工具审批拒绝: toolCallId={} name={}",
                        tool_call_id[:8] if tool_call_id else "", params.get("name"))
            if tool_call_id:
                future = self._pending_tools.get(str(tool_call_id))
                if future and not future.done():
                    future.set_result("[用户拒绝] 工具调用已被用户拒绝")
                    logger.info("[LSP4J-TOOL] 已取消 pending Future: toolCallId={}", tool_call_id[:8])
        else:
            logger.info("[LSP4J-TOOL] 工具审批通过: toolCallId={} name={}",
                        tool_call_id[:8] if tool_call_id else "", params.get("name"))

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
        tool_call_id = params.get("toolCallId")
        if not tool_call_id:
            if _LSP4J_STRICT_TOOLCALL_ID:
                logger.warning(
                    "[LSP4J-TOOL] invokeResult 缺失 toolCallId（strict 模式拒绝）: requestId={} name={}",
                    str(params.get("requestId", ""))[:8],
                    str(params.get("name", "")),
                )
                await self._send_response(
                    msg_id, {"status": "error", "message": "Missing toolCallId in strict mode"}
                )
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
                    tool_call_id[:8], request_id[:8], tool_name,
                )
            elif len(candidates) > 1:
                logger.warning(
                    "[LSP4J-TOOL] invokeResult toolCallId 缺失且候选冲突: requestId={} name={} candidates={}",
                    request_id[:8], tool_name, [c[:8] for c in candidates],
                )
                await self._send_response(
                    msg_id, {"status": "error", "message": "Missing toolCallId and ambiguous fallback match"}
                )
                return
            else:
                logger.warning(
                    "[LSP4J-TOOL] invokeResult toolCallId 缺失且无候选: requestId={} name={}",
                    request_id[:8], tool_name,
                )
                await self._send_response(msg_id, {"status": "error", "message": "Missing toolCallId"})
                return

        # 查找 pending Future
        future = self._pending_tools.get(str(tool_call_id))
        if future and not future.done():
            # 异步工具结果日志
            logger.info("[LSP4J-TOOL] invokeResult matched: toolCallId={} success={} name={} pending_count={}",
                         tool_call_id[:8], params.get("success", True), params.get("name"),
                         len(self._pending_tools))
            if params.get("success", True):
                result = params.get("result", {})
                # result 可能是 dict，需要转为 JSON 字符串
                if isinstance(result, dict):
                    result_str = json.dumps(result, ensure_ascii=False)
                else:
                    result_str = str(result)
                future.set_result(result_str)
            else:
                error_msg = params.get("errorMessage", "Tool execution failed")
                future.set_result(f"[工具错误] {error_msg}")
        elif not future or future.done():
            # 无匹配 Future（可能已超时）
            logger.warning("[LSP4J-TOOL] invokeResult: toolCallId={} 无匹配 Future（可能已超时）", tool_call_id)

        # tool/invokeResult 是 @JsonRequest，需要返回 OperateCommonResult 格式
        # 插件 ToolService.invokeResult() 期望返回 OperateCommonResult {errorCode, errorMessage}
        # ⚠️ 关键：成功时 errorCode 必须为 null（不是空字符串），否则插件会认为是错误响应
        # 插件源码：if (result.getErrorCode() != null) { log.warn("error response"); }
        await self._send_response(msg_id, {
            "errorCode": None,  # 成功时必须为 null，不能是 ""
            "errorMessage": None,
        })

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
                logger.warning("[LSP4J-TOOL] 工具响应错误: id={} code={} msg={}", msg_id, error.get("code"), error.get("message"))
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
                        logger.warning("[LSP4J-TOOL] 工具执行失败: id={} name={} error={}", msg_id, result.get("name"), result.get("errorMessage", ""))
                        future.set_result(f"[工具错误] {result.get('errorMessage', 'Execution failed')}")
                else:
                    future.set_result(str(result))
        else:
            if future is None:
                # 未匹配的响应（可能是 tool/invoke 的 ack 响应，正常情况）
                logger.debug("[LSP4J-TOOL] 收到未匹配的响应: id={} pending_responses={} pending_tools={}",
                              msg_id, list(self._pending_responses.keys()), list(self._pending_tools.keys())[:3])
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

    async def _send_workspace_file_sync(
        self, ws_file: Any, sync_type: str = "MODIFIED"
    ) -> None:
        """发送 workingSpaceFile/sync 通知（WorkspaceFileSyncResult 格式）。

        插件 LanguageClientImpl.syncWorkspaceFile 收到后：
        - type=ADD → WorkspaceFileAddedNotifier（新增文件卡片）
        - type=MODIFIED → WorkspaceFileModifiedNotifier（更新状态/diff/showNewDiff）
        - type=FRUSH → VFS 刷新
        """
        payload = self._ws_file_service.build_sync_result(
            ws_file, self._project_path, sync_type)
        logger.info("[WS-FILE] 发送 workingSpaceFile/sync: type={} fileId={} status={} +{} -{}",
                    sync_type, ws_file.file_id, ws_file.status,
                    ws_file.diff_info.add, ws_file.diff_info.delete)
        await self._send_client_request("workingSpaceFile/sync", payload)

    async def _send_snapshot_sync_all(self, session_id: str, sync_type: str = "ADD") -> None:
        """发送 snapshot/syncAll 通知（SnapshotSyncAllResult 格式）。"""
        payload = self._ws_file_service.build_snapshot_sync_all(
            session_id, self._project_path, sync_type)
        logger.info("[WS-FILE] 发送 snapshot/syncAll: type={} snapshot_count={} file_count={}",
                    sync_type, len(payload["snapshots"]), len(payload["workingSpaceFiles"]))
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
        snap = self._ws_file_service.get_or_create_snapshot(session_id, request_id)

        # 确定变更类型
        if tool_name == "create_file_with_text":
            mode = "ADD"
        elif tool_name == "delete_file_by_path":
            mode = "DELETE"
        else:
            mode = "MODIFIED"

        # 创建或更新工作区文件
        ws_file = self._ws_file_service.create_or_update_file(
            session_id, snap.id, file_path, mode, tool_call_id)

        # 获取编辑前后内容
        if tool_name == "create_file_with_text":
            last_stable = ""
            full_content = arguments.get("text") or arguments.get("content") or ""
        elif tool_name == "delete_file_by_path":
            last_stable = self._ws_file_service.get_cached_content(file_path) or ""
            full_content = ""
        else:
            # replace_text_by_path: 全文替换，新内容就是 text 参数
            last_stable = self._ws_file_service.get_cached_content(file_path) or ""
            full_content = arguments.get("text") or ""

        # 计算 DiffInfo 并存储（大文件 difflib 可能较慢，避免阻塞事件循环）
        await asyncio.to_thread(
            self._ws_file_service.set_content, file_path, last_stable, full_content
        )

        # 设置状态为 APPLIED
        self._ws_file_service.update_status(file_path, "APPLIED")

        logger.info("[WS-FILE] 文件编辑完成: tool={} path={} mode={} wsId={} +{} -{}",
                    tool_name, file_path, mode, ws_file.id[:8],
                    ws_file.diff_info.add, ws_file.diff_info.delete)
        return ws_file

    async def invoke_tool_on_ide(
        self, tool_name: str, arguments: dict, timeout: float = 120.0
    ) -> str:
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
                logger.info("[LSP4J-TOOL] toolCallId 队列匹配: original={} mapped={} callId={} queue_remaining={}",
                             original_name, tool_name, tool_call_id[:8], len(self._tool_call_id_queue))
                break
        queue_matched = bool(tool_call_id)
        if not tool_call_id:
            tool_call_id = str(uuid.uuid4())
            logger.info("[LSP4J-TOOL] toolCallId 队列未匹配，新建兜底: name={} callId={}",
                         tool_name, tool_call_id[:8])
        request_id = self._current_request_id or str(uuid.uuid4())

        logger.info("[LSP4J-TOOL] invoke_tool_on_ide: tool={} callId={} requestId={} timeout={} queue_matched={}",
                    tool_name, tool_call_id[:8], request_id[:8], timeout, queue_matched)

        # ★ 本地工具（list_dir, search_file）：后端本地执行，不发送到 IDE
        if tool_name in ("list_dir", "search_file"):
            try:
                result_str, results_list = _execute_local_tool(tool_name, arguments)
                if not queue_matched:
                    # 队列未匹配 → on_tool_call 未被调用 → 没有 markdown_block → 没有 ToolPanel
                    # 发送 FINISHED sync 只会导致消费线程阻塞等待不存在的 panel
                    logger.warning("[LSP4J-TOOL] 跳过本地工具 FINISHED sync（无匹配 panel）: tool={} callId={}",
                                   tool_name, tool_call_id[:8])
                else:
                    # 等待插件 UI 注册 panel（on_tool_call 已发送 markdown_block + 500ms sleep + PENDING）
                    await asyncio.sleep(1.0)
                    await self._send_tool_call_sync(
                        self._session_id, request_id, tool_call_id,
                        "FINISHED", tool_name=original_name, parameters=arguments,
                        results=results_list,
                    )
                logger.info("[LSP4J-TOOL] 本地工具执行完成: tool={} results_count={}",
                             tool_name, len(results_list))
                return result_str
            except Exception as e:
                logger.exception("[LSP4J-TOOL] 本地工具执行失败: tool={}", tool_name)
                if queue_matched:
                    await self._send_tool_call_sync(
                        self._session_id, request_id, tool_call_id,
                        "ERROR", tool_name=original_name, parameters=arguments,
                        error_msg=str(e),
                    )
                return f"[错误] 工具 {tool_name} 执行失败: {e}"

        # ★ 参数名统一转换：LLM 使用 snake_case，插件 ToolHandler 期望 camelCase
        # 按插件 ToolInvokeProcessor 中每个 handler 的取参方法分类：
        #   - replace_text_by_path / create_file_with_text / delete_file_by_path
        #     → handler 调用 getRequestFilePath()，取 filePath (camelCase)
        #   - read_file / save_file
        #     → handler 调用 getRequestFilePathWithUnderLine()，取 file_path (snake_case)
        # 因此前 3 个需转换 file_path→filePath，后 2 个保留 file_path 不变。
        # 注意：read_file 的 LLM 参数名是 "path"（非 "file_path"），仅它特殊。
        params = dict(arguments)
        invoke_tool_name = tool_name
        if tool_name == "search_replace":
            # search_replace 需要保持逻辑名称不变（用于 toolCallId 队列匹配与前端展示），
            # 但 IDE 端实际执行器使用 replace_text_by_path。
            invoke_tool_name = "replace_text_by_path"
            # search_replace 参数通常是 search_text/replace_text；插件 replace_text_by_path 只认 text。
            # 这里退化为全量替换文本，至少保证 IDE 有可执行输入，不再落空文本导致“生成中+无diff”。
            if "text" not in params:
                replacement_text = params.get("replace_text") or params.get("replaceText")
                if isinstance(replacement_text, str):
                    params["text"] = replacement_text
        if tool_name in _LSP4J_FILE_EDIT_TOOLS:
            if "file_path" in params and "filePath" not in params:
                params["filePath"] = params.pop("file_path")
        elif tool_name == "read_file":
            if "path" in params and "filePath" not in params:
                params["filePath"] = params.pop("path")
        elif tool_name == "save_file":
            # save_file 的 ToolHandler 使用 getRequestFilePathWithUnderLine()，
            # 读取的是 file_path (snake_case)，不需要转换。此处保留代码以消除歧义。
            # 但若 LLM 误传 filePath，需转回 file_path。
            if "filePath" in params and "file_path" not in params:
                params["file_path"] = params.pop("filePath")
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
        trace_key = (
            f"req={request_id[:8]} call={tool_call_id[:8]} "
            f"logical={original_name} invoke={invoke_tool_name}"
        )
        logger.info("[LSP4J-TRACE] 队列状态 trace={} matched={}", trace_key, queue_matched)

        # ★ 使用 LLM 侧名称发送 RUNNING sync，参数保留原始 snake_case
        # ToolPanel 用 toolName 判断工具类型（如 edit_file 而非 replace_text_by_path），
        # 用 parameters["file_path"] 渲染文件链接。
        sync_parameters = dict(arguments)
        # ToolPanel 链接构建优先取 parameters["file_path"]，补齐避免“查看文件无链接”。
        if tool_name in ("read_file", "save_file"):
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

        if queue_matched:
            await self._send_tool_call_sync(
                self._session_id, request_id, tool_call_id,
                "RUNNING", tool_name=original_name, parameters=sync_parameters,
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
        logger.info("[LSP4J-TOOL] Future registered: toolCallId={} pending_count={} waiting for invokeResult",
                     tool_call_id[:8], len(self._pending_tools))
        logger.info("[LSP4J-TRACE] 调用工具 trace={}", trace_key)

        # 发送 tool/invoke 请求（带 id，触发插件 @JsonRequest("invoke") 处理器）
        # 但不注册到 _pending_responses —— 插件的 ack 响应不是工具结果，
        # 真实结果通过 tool/invokeResult 异步回传
        rpc_id = self._next_request_id()
        await self._send_message({
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": "tool/invoke",
            "params": {
                "requestId": request_id,
                "toolCallId": tool_call_id,
                "name": invoke_tool_name,  # 实际调用名（search_replace 在此降级为 replace_text_by_path）
                "parameters": params,      # 已转换参数名（如 path → filePath）
                "async": True,             # 异步执行，结果通过 invokeResult 回传
            },
        })

        # 等待异步结果
        file_path_for_id = arguments.get("file_path") or arguments.get("filePath")
        try:
            result = await asyncio.wait_for(tool_future, timeout=timeout)
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
                    self._ws_file_service.cache_file_content(path, content)
                logger.info("[LSP4J-TOOL] read_file FINISHED results 注入 path: path={}", path)
            elif tool_name in _LSP4J_FILE_EDIT_TOOLS and file_path_for_id:
                # ★ 文件编辑工具：创建工作区文件 + 计算 diff + 发送 workingSpaceFile/sync
                ws_file = await self._handle_file_edit_finished(
                    tool_name, file_path_for_id, arguments, tool_call_id, request_id)

                # ★ 注入工作区文件 UUID 作为 fileId（而非文件路径）
                # ToolPanel.constructFileItem 用 results[0]["fileId"] 设置 FileItem.id，
                # 后续 workingSpaceFile/sync 的 WorkingSpaceFileInfo.id 需与此一致，
                # AIDevFilePanel.syncWorkspaceFile 才能通过 id 匹配更新。
                ws_id = ws_file.id if ws_file else file_path_for_id
                results = [{"path": file_path_for_id, "fileId": ws_id,
                            "message": result[:500] if result else ""}]
                if "file_path" not in arguments:
                    arguments["file_path"] = file_path_for_id
                logger.info("[LSP4J-TOOL] FINISHED results 注入 path+fileId: path={} wsId={} tool={}",
                            file_path_for_id, ws_id[:8], tool_name)

            # P1 兜底：文件工具 FINISHED 结果为空/缺 fileId 时，按 file_path 回填一次，避免 ToolPanel 早退。
            if tool_name in _LSP4J_FILE_EDIT_TOOLS and file_path_for_id:
                first_result = results[0] if isinstance(results, list) and results else {}
                has_file_id = isinstance(first_result, dict) and bool(first_result.get("fileId"))
                has_path = isinstance(first_result, dict) and bool(first_result.get("path"))
                if not (has_file_id and has_path):
                    ws_file_fallback = self._ws_file_service.get_file(file_path_for_id)
                    fallback_ws_id = ws_file_fallback.id if ws_file_fallback else file_path_for_id
                    results = [{
                        "path": file_path_for_id,
                        "fileId": fallback_ws_id,
                        "message": result[:500] if isinstance(result, str) else "",
                    }]
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
                await self._send_tool_call_sync(
                    self._session_id, request_id, tool_call_id,
                    "FINISHED", tool_name=original_name, parameters=arguments,
                    results=results,
                )
                logger.info(
                    "[LSP4J-TRACE] FINISHED 已发送 trace={} backfilled={}",
                    trace_key,
                    finished_backfilled,
                )
            else:
                logger.warning(
                    "[LSP4J-TOOL] 队列未匹配，跳过 FINISHED sync（避免幽灵事件）: tool={} callId={} requestId={}",
                    tool_name,
                    tool_call_id[:8],
                    request_id[:8],
                )

            # ★ 文件编辑工具完成后发送 workingSpaceFile/sync 通知（APPLIED 状态）
            # 必须在 toolCallSync FINISHED 之后发送，确保 ToolPanel 已创建 AIDevFilePanel
            if tool_name in _LSP4J_FILE_EDIT_TOOLS and file_path_for_id:
                ws_file = self._ws_file_service.get_file(file_path_for_id)
                if ws_file:
                    await asyncio.sleep(0.5)
                    # 插件双通道路由差异：
                    # 1) WorkingSpacePanel 只通过 ADD notifier 把文件加入“变更文件”列表；
                    # 2) ToolPanel 内 FileItemPanel 通过 MODIFIED notifier 更新状态（GENERATING→APPLIED）。
                    # 为保证“列表完整 + 卡片状态正确”，文件编辑工具统一双发 ADD + MODIFIED。
                    await self._send_workspace_file_sync(ws_file, "ADD")
                    await self._send_workspace_file_sync(ws_file, "MODIFIED")
                    logger.info("[LSP4J-TRACE] WS 双发已完成 trace={} sent=ADD,MODIFIED", trace_key)
                    if self._session_id:
                        try:
                            await self._send_snapshot_sync_all(self._session_id)
                        except Exception as e:
                            logger.warning("[WS-FILE] 工具编辑后 snapshot/syncAll 失败（非致命）: {}", e)

            return result
        except asyncio.TimeoutError:
            logger.warning("LSP4J: 工具调用超时 tool={} callId={}", tool_name, tool_call_id)
            self._cancelled_requests[rpc_id] = None
            if len(self._cancelled_requests) > self._MAX_CANCELLED_REQUESTS_SIZE:
                self._cancelled_requests.pop(next(iter(self._cancelled_requests)))
            if queue_matched:
                await self._send_tool_call_sync(
                    self._session_id, request_id, tool_call_id,
                    "ERROR", tool_name=original_name, parameters=arguments,
                    error_msg=f"工具 {tool_name} 执行超时（{timeout}s）",
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
                ws_file = self._ws_file_service.update_status(
                    file_path_for_id, "APPLYING_FAILED", f"超时（{timeout}s）")
                if ws_file:
                    await self._send_workspace_file_sync(ws_file, "MODIFIED")
            return f"[超时] 工具 {tool_name} 执行超时（{timeout}s）"
        finally:
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
        text = re.sub(
            r'```[^`]*```',
            protect_codeblock,
            text,
            flags=re.DOTALL
        )

        # 2. 提取并保护 Markdown 表格：防止表格单元格中的路径被转换
        table_placeholder_prefix = "__LSP4J_TABLE_PLACEHOLDER__"
        table_placeholders: list[str] = []

        def protect_table(m: re.Match) -> str:
            table_placeholders.append(m.group(0))
            return f"{table_placeholder_prefix}{len(table_placeholders) - 1}__"

        # 保护 Markdown 表格：匹配包含 | 的多行文本块（至少 2 行，其中一行包含 |---|）
        text = re.sub(
            r'((?:^|\n)(?:\|[^\n\|]+\|[^\n]*\n)+(?:\|\s*:?-+:?\s*\|[^\n]*\n)(?:\|[^\n\|]+\|[^\n]*\n)*)',
            protect_table,
            text
        )

        # 3. 提取并保护 Markdown 链接：[text](url) → 占位符
        placeholder_prefix = "__LSP4J_LINK_PLACEHOLDER__"
        link_placeholders: list[str] = []

        def protect_link(m: re.Match) -> str:
            link_placeholders.append(m.group(0))
            return f"{placeholder_prefix}{len(link_placeholders) - 1}__"

        # 保护 Markdown 链接：[...](...)
        text = re.sub(r'\[[^\]]*\]\([^)]+\)', protect_link, text)

        # 4. 提取并保护反引号代码内容：`code` → 占位符
        inline_code_placeholders: list[str] = []

        def protect_inline_code(m: re.Match) -> str:
            inline_code_placeholders.append(m.group(0))
            return f"__LSP4J_INLINECODE_{len(inline_code_placeholders) - 1}__"

        text = re.sub(r'`[^`]+`', protect_inline_code, text)

        # 4. 现在可以安全地转换纯文本中的文件路径了
        path_pattern = re.compile(
            r'('
            r'/(?:[a-zA-Z0-9_\-./]+[a-zA-Z0-9_\-/]|bin|etc|usr|home|Users|tmp|var|opt)[a-zA-Z0-9_\-./]*'
            r'|[a-zA-Z]:[/\\\\][a-zA-Z0-9_\-./\\\\]+'
            r')'
            r'(?::(\d+)|#L(\d+)(?:-L(\d+))?)?'
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
        text = re.sub(
            r"```([a-zA-Z0-9_-]+)\n\|CODE_EDIT_BLOCK\|",
            r"```\1|CODE_EDIT_BLOCK|",
            text
        )

        # 日志：如果发生了修复，记录差异
        if text != original:
            logger.debug(
                "[LSP4J] CODE_EDIT_BLOCK 格式已修复\n"
                "  修复前: {}\n"
                "  修复后: {}",
                original[:100], text[:100]
            )

        return text

    async def _send_chat_answer(
        self, session_id: str | None, text: str, request_id: str
    ) -> None:
        """发送 chat/answer（ChatAnswerParams 格式）。

        ⚠️ 字段名必须严格匹配：
        - text（不是 content）
        - requestId（必须携带）
        - overwrite: False（流式追加，不覆盖）
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

        # chat/answer 发送追踪
        logger.debug("[LSP4J] chat/answer: requestId={} text_len={}", request_id, len(text))

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

        await self._send_client_request("chat/answer", {
            "requestId": request_id,
            "sessionId": session_id or "",
            "text": converted_text,
            "overwrite": False,
            "isFiltered": False,
            "timestamp": int(time.time() * 1000),
            "extra": extra,
        })

    async def _send_chat_think(
        self, session_id: str | None, text: str, step: str, request_id: str
    ) -> None:
        """发送 chat/think（ChatThinkingParams 格式）。

        ⚠️ 字段名必须严格匹配：
        - text（不是 content）
        - step: "start" 或 "done"（不是 thinking: true）
        - requestId（必须携带）
        - timestamp（毫秒时间戳）
        """
        await self._send_client_request("chat/think", {
            "requestId": request_id,
            "sessionId": session_id or "",
            "text": text,
            "step": step,  # "start" 或 "done"
            "timestamp": int(time.time() * 1000),
        })

    async def _send_chat_finish(
        self, session_id: str | None, reason: str, full_answer: str,
        request_id: str, status_code: int = 200,
    ) -> None:
        """发送 chat/finish（ChatFinishParams 格式）。

        ⚠️ 字段名必须严格匹配：
        - requestId（必须携带）
        - statusCode: 200（成功），408（超时），500（服务端错误）
          灵码插件 BaseChatPanel.stopGenerate() 检查 statusCode == 200 判断成功，
          其他值进入对应错误分支。绝对不能用 0 表示成功。
        - fullAnswer（完整回答文本）
        """
        logger.info("[LSP4J] chat/finish: requestId={} statusCode={} reason={}",
                     request_id, status_code, reason)
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
        
        await self._send_client_request("chat/finish", {
            "requestId": request_id,
            "sessionId": session_id or "",
            "reason": reason,
            "statusCode": status_code,
            "fullAnswer": converted_full_answer,
            "extra": extra if extra else None,  # 空 dict 发 null 避免无意义数据
        })

    async def _send_session_title_update(
        self, session_id: str | None, title: str
    ) -> None:
        """发送 session/title/update 通知（SessionTitleRequest 格式）。

        参数：sessionId, sessionTitle
        """
        await self._send_client_request("session/title/update", {
            "sessionId": session_id or "",
            "sessionTitle": title,
        })

    async def _send_process_step_callback(
        self, session_id: str | None, request_id: str,
        step: str, description: str, status: str,
        result: Any = None, message: str = "",
    ) -> None:
        """发送 chat/process_step_callback 通知（ChatProcessStepCallbackParams 格式）。

        插件收到后会在聊天面板渲染步骤进度列表。
        status 取值：doing / done / error / manual_confirm
        step 取值参考 ChatStepEnum：step_start, step_end, step_refine_query 等
        """
        # 步骤推送日志
        logger.info("[LSP4J] process_step_callback: requestId={} step={} status={} desc={}", request_id, step, status, description[:80])
        await self._send_client_request("chat/process_step_callback", {
            "requestId": request_id,
            "sessionId": session_id or "",
            "step": step,
            "description": description,
            "status": status,
            "result": result,
            "message": message,
        })

    async def _send_message(self, message: dict[str, Any]) -> None:
        """发送 LSP Base Protocol 格式的消息到 WebSocket。"""
        # 连接已关闭，不再发送
        if self._closed:
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
                        logger.debug("[LSP4J →] method={} id={} params_keys={}", _method, message.get("id"), _pkeys)
                else:
                    logger.info("[LSP4J →] method={} id={} params_keys={}", _method, message.get("id"), _pkeys)
            elif "result" in message:
                logger.debug("[LSP4J →] response id={} result_type={}", message.get("id"), type(message["result"]).__name__)
            elif "error" in message:
                logger.warning("[LSP4J →] error id={} code={} msg={}", message.get("id"),
                               message.get("error", {}).get("code"), message.get("error", {}).get("message"))

            frame = LSPBaseProtocolParser.format_message(message)
            await self._ws.send_text(frame)
        except Exception as e:
            logger.error("LSP4J: WebSocket 发送失败: {}", e)

    async def _send_response(self, msg_id: Any, result: Any) -> None:
        """发送 JSON-RPC 成功响应。"""
        await self._send_message({
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": result,
        })

    async def _send_error_response(
        self, msg_id: Any, code: int, message: str
    ) -> None:
        """发送 JSON-RPC 错误响应。"""
        await self._send_message({
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": code, "message": message},
        })

    async def _send_request(
        self, method: str, params: dict, request_id: int
    ) -> None:
        """发送 JSON-RPC 请求（server → client）。"""
        await self._send_message({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        })

    async def _send_notification(self, method: str, params: dict) -> None:
        """发送 JSON-RPC 通知（无 id，不期望响应）。

        仅用于插件 LanguageClient.java 中标注为 @JsonNotification 的方法
        （如 image/uploadResultNotification）。
        对于 @JsonRequest 方法（chat/answer, chat/think, chat/finish 等），
        必须使用 _send_client_request 以确保 LSP4J 正确分发。
        """
        await self._send_message({
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        })

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
        await self._send_message({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        })

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
        snaps = self._ws_file_service.list_snapshots_for_session(session_id)
        await self._send_response(msg_id, {
            "snapshots": [s.to_dict() for s in snaps],
            "errorCode": None,
            "errorMessage": None,
        })
        logger.debug("[LSP4J] snapshot/listBySession: sessionId={} count={}", session_id[:16] if session_id else "", len(snaps))

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
                self._ws_file_service.apply_snapshot_accept_all(snap_id)
            elif op_type in ("REJECT_ALL",):
                self._ws_file_service.apply_snapshot_reject_all(snap_id)
            elif op_type in ("SWITCH", "ACTIVATE") and target_snap:
                if not self._ws_file_service.set_current_snapshot(target_snap):
                    err = "snapshot_not_found"
            elif op_type in ("ACCEPT", "APPLY"):
                for item in ws_items:
                    if not isinstance(item, dict):
                        continue
                    wid = item.get("id") or ""
                    if wid:
                        self._ws_file_service.operate(wid, "ACCEPT", item.get("content"))
            elif op_type in ("REJECT",):
                for item in ws_items:
                    if not isinstance(item, dict):
                        continue
                    wid = item.get("id") or ""
                    if wid:
                        self._ws_file_service.operate(wid, "REJECT", None)
            elif op_type in ("CANCEL", "UPDATE_CHAT_RECORD"):
                pass
            else:
                logger.debug("[LSP4J] snapshot/operate noop opType={} id={}", op_type, snap_id[:8] if snap_id else "")

            sess_for_sync = ""
            if target_snap and op_type in ("SWITCH", "ACTIVATE"):
                sinfo = self._ws_file_service.find_snapshot_by_id(target_snap)
                sess_for_sync = sinfo.session_id if sinfo else ""
            elif snap_id:
                sinfo = self._ws_file_service.find_snapshot_by_id(snap_id)
                sess_for_sync = sinfo.session_id if sinfo else (self._session_id or "")
            else:
                sess_for_sync = self._session_id or ""

            if sess_for_sync and op_type in (
                "ACCEPT_ALL", "REJECT_ALL", "ACCEPT", "REJECT", "APPLY", "SWITCH", "ACTIVATE",
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
            out.append({
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
            })
        await self._send_response(msg_id, out)

    async def _handle_chat_get_session_by_id(self, params: dict, msg_id: Any) -> None:
        """chat/getSessionById → 从 DB 组装可恢复的 ChatSession（含 REPLY_TASK 记录）。"""
        session_id = params.get("sessionId") or ""
        try:
            sid_uuid = uuid.UUID(session_id)
        except ValueError:
            await self._send_response(msg_id, {
                "sessionId": session_id,
                "sessionTitle": "",
                "chatRecords": [],
                "sessionType": "ASSISTANT",
                "gmtCreate": 0,
                "gmtModified": 0,
            })
            return

        try:
            async with async_session() as db:
                sr = await db.execute(select(ChatSession).where(ChatSession.id == sid_uuid))
                sess = sr.scalar_one_or_none()
                if not sess or sess.user_id != self._user_id or sess.agent_id != self._agent_id:
                    await self._send_response(msg_id, {
                        "sessionId": session_id,
                        "sessionTitle": "",
                        "chatRecords": [],
                        "sessionType": "ASSISTANT",
                        "gmtCreate": 0,
                        "gmtModified": 0,
                    })
                    return

                mr = await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.conversation_id == str(sid_uuid))
                    .where(ChatMessage.agent_id == self._agent_id)
                    .where(ChatMessage.user_id == self._user_id)
                    .where(ChatMessage.role.in_(("user", "assistant")))
                    .order_by(ChatMessage.created_at.asc())
                )
                rows = list(mr.scalars().all())
        except Exception as e:
            logger.warning("[LSP4J] chat/getSessionById DB error: {}", e)
            await self._send_response(msg_id, {
                "sessionId": session_id,
                "sessionTitle": "",
                "chatRecords": [],
                "sessionType": "ASSISTANT",
                "gmtCreate": 0,
                "gmtModified": 0,
            })
            return

        records: list[dict[str, Any]] = []
        i = 0
        while i < len(rows):
            row = rows[i]
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
            records.append({
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
            })
            i += 2 if asst_msg else 1

        await self._send_response(msg_id, {
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
        })

    async def _handle_chat_delete_session_by_id(self, params: dict, msg_id: Any) -> None:
        """chat/deleteSessionById → 删除单个 IDE 会话（Clawith-only）。"""
        session_id = params.get("sessionId") or ""
        try:
            sid_uuid = uuid.UUID(session_id)
        except ValueError:
            await self._send_response(msg_id, None)
            return

        try:
            async with async_session() as db:
                sr = await db.execute(select(ChatSession).where(ChatSession.id == sid_uuid))
                sess = sr.scalar_one_or_none()
                if sess and sess.user_id == self._user_id and sess.agent_id == self._agent_id and sess.source_channel == "ide_lsp4j":
                    await db.execute(
                        delete(ChatMessage)
                        .where(ChatMessage.conversation_id == str(sid_uuid))
                        .where(ChatMessage.agent_id == self._agent_id)
                        .where(ChatMessage.user_id == self._user_id)
                    )
                    await db.delete(sess)
                    await db.commit()
        except Exception as e:
            logger.warning("[LSP4J] chat/deleteSessionById DB error: {}", e)

        if self._session_id == session_id:
            self._session_id = None
        await self._send_response(msg_id, None)

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
        await self._send_response(msg_id, None)

    async def _handle_chat_delete_chat_by_id(self, params: dict, msg_id: Any) -> None:
        """chat/deleteChatById → 删除单条聊天记录（若 requestId 可映射为消息 UUID）。"""
        session_id = params.get("sessionId") or ""
        request_id = params.get("requestId") or ""
        try:
            sid_uuid = uuid.UUID(session_id)
            rid_uuid = uuid.UUID(request_id)
        except ValueError:
            await self._send_response(msg_id, None)
            return

        try:
            async with async_session() as db:
                await db.execute(
                    delete(ChatMessage)
                    .where(ChatMessage.id == rid_uuid)
                    .where(ChatMessage.conversation_id == str(sid_uuid))
                    .where(ChatMessage.agent_id == self._agent_id)
                    .where(ChatMessage.user_id == self._user_id)
                )
                await db.commit()
        except Exception as e:
            logger.warning("[LSP4J] chat/deleteChatById DB error: {}", e)
        await self._send_response(msg_id, None)

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
                    "isReasoning": False,
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
        await self._send_response(msg_id, {
            "classes": [
                {"key": "clawith", "displayName": "Clawith Models"},
                {"key": "byok", "displayName": "BYOK (Bring Your Own Key)"},
            ],
        })

    async def _handle_ping(self, params: dict, msg_id: Any) -> None:
        """ping → PingResult（success 不可为 null，避免 IDE 端 NPE）。"""
        _ = params
        await self._send_response(msg_id, {"success": True})

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
        ws_file = self._ws_file_service.get_file_by_id(ws_id)
        if ws_file:
            logger.info("[WS-FILE] getLastStableContent: id={} len={}",
                        ws_id[:8], len(ws_file.last_stable_content))
            await self._send_response(msg_id, {
                "content": ws_file.last_stable_content,
                "version": ws_file.version,
                "errorCode": None,
                "errorMessage": None,
            })
        else:
            logger.warning("[WS-FILE] getLastStableContent: 未找到 id={}", ws_id)
            await self._send_response(msg_id, {
                "content": "",
                "version": "0",
                "errorCode": None,
                "errorMessage": None,
            })

    async def _handle_ws_get_full_content(self, params: dict, msg_id: Any) -> None:
        """workingSpaceFile/getFullContent — 返回编辑后的文件内容。

        插件 InlineDiffManager.showSingleDiff 调用此方法获取 diff 右侧内容。
        """
        ws_id = params.get("id", "")
        ws_file = self._ws_file_service.get_file_by_id(ws_id)
        if ws_file:
            logger.info("[WS-FILE] getFullContent: id={} len={}",
                        ws_id[:8], len(ws_file.content))
            await self._send_response(msg_id, {
                "content": ws_file.content,
                "version": ws_file.version,
                "errorCode": None,
                "errorMessage": None,
            })
        else:
            logger.warning("[WS-FILE] getFullContent: 未找到 id={}", ws_id)
            await self._send_response(msg_id, {
                "content": "",
                "version": "0",
                "errorCode": None,
                "errorMessage": None,
            })

    async def _handle_ws_list_by_snapshot(self, params: dict, msg_id: Any) -> None:
        """workingSpaceFile/listBySnapshot — 列出某个快照下的所有工作区文件。

        参数：ListWorkingSpaceFileBySnapshotParams { snapshotId, sessionId, requestId }
        返回：ListWorkingSpaceFileResult { workingSpaceFiles, errorCode, errorMessage }
        """
        snapshot_id = params.get("snapshotId", "")
        files = self._ws_file_service.list_by_snapshot(snapshot_id)
        logger.info("[WS-FILE] listBySnapshot: snapshotId={} count={}",
                    snapshot_id[:8] if snapshot_id else "?", len(files))
        await self._send_response(msg_id, {
            "workingSpaceFiles": [f.to_wire_format() for f in files],
            "errorCode": None,
            "errorMessage": None,
        })

    async def _handle_ws_operate(self, params: dict, msg_id: Any) -> None:
        """workingSpaceFile/operate — 接受/拒绝文件变更。

        参数：WorkingSpaceFileOperateParams { id, opType, content }
        """
        ws_id = params.get("id", "")
        op_type = params.get("opType", "")
        content = params.get("content")
        success = self._ws_file_service.operate(ws_id, op_type, content)
        logger.info("[WS-FILE] operate: id={} op={} success={}", ws_id[:8], op_type, success)
        await self._send_response(msg_id, {
            "errorCode": None,
            "errorMessage": None,
        })
        # 操作后发送状态更新通知
        if success:
            ws_file = self._ws_file_service.get_file_by_id(ws_id)
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
        success = await asyncio.to_thread(
            self._ws_file_service.update_content, ws_id, content, local_content
        )
        logger.info("[WS-FILE] updateContent: id={} success={}", ws_id[:8], success)
        await self._send_response(msg_id, {
            "errorCode": None,
            "errorMessage": None,
        })

    async def _handle_step_process_confirm(self, params: dict, msg_id: Any) -> None:
        """处理 agents/testAgent/stepProcessConfirm — 步骤确认请求。

        当 step callback 的 status="manual_confirm" 时，插件显示确认按钮。
        用户点击后发送此请求，Clawith 返回确认结果。

        返回格式：StepProcessConfirmResult { requestId, errorMessage, successful }
        """
        # 步骤确认日志
        logger.info("[LSP4J] stepProcessConfirm: requestId={} params_keys={}", params.get("requestId", ""), list(params.keys()))
        await self._send_response(msg_id, {
            "requestId": params.get("requestId", ""),
            "successful": True,
            "errorMessage": "",
        })

    # ──────────────────────────────────────────
    # 图片上传
    # ──────────────────────────────────────────

    def _cleanup_expired_images(self) -> None:
        """清理过期的图片缓存（过期 + LRU 大小限制）。"""
        now = time.time()
        # 清理过期缓存
        expired_keys = [
            k for k, (_, _, ts) in self._image_cache.items()
            if now - ts > self._image_cache_ttl
        ]
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
            await self._send_response(msg_id, {
                "requestId": request_id,
                "errorCode": "LOCAL_PATH_NOT_SUPPORTED",
                "errorMessage": "LSP4J 模式暂不支持本地文件路径图片上传",
                "result": {"success": False},
            })
            return

        # 校验：base64 数据大小≤10MB
        base64_data = image_uri.split(",", 1)[1] if "," in image_uri else ""
        if len(base64_data) > 10 * 1024 * 1024:
            logger.warning("[LSP4J] image/upload: 图片超过 10MB 限制, requestId={}", request_id)
            await self._send_response(msg_id, {
                "requestId": request_id,
                "errorCode": "FILE_TOO_LARGE",
                "errorMessage": "图片大小超过 10MB 限制",
                "result": {"success": False},
            })
            return

        # 校验通过，立即返回成功响应
        await self._send_response(msg_id, {
            "requestId": request_id,
            "result": {"success": True},
        })

        # 异步发送 uploadResultNotification
        try:
            # 生成图片 URL（当前 MVP 直接使用 data URI 作为 imageUrl）
            image_url = image_uri
            # 缓存图片
            self._image_cache[request_id] = (image_url, base64_data, time.time())

            await self._send_notification("image/uploadResultNotification", {
                "result": {
                    "requestId": request_id,
                    "imageUrl": image_url,
                },
            })
            logger.info("[LSP4J] image/upload 成功: requestId={} base64_len={}", request_id, len(base64_data))
        except Exception as e:
            logger.warning("[LSP4J] image/upload 异步通知失败: requestId={} error={}", request_id, e)

    # ──────────────────────────────────────────
    # 配置端点
    # ──────────────────────────────────────────

    async def _handle_config_get_endpoint(self, params: dict, msg_id: Any) -> None:
        """处理 config/getEndpoint 请求。

        返回 GlobalEndpointConfig 格式：{"endpoint": "https://..."}。
        插件 GlobalEndpointConfig.java 只有 endpoint: String 字段。
        """
        _ = params
        settings = get_settings()
        endpoint = settings.PUBLIC_BASE_URL or ""
        logger.debug("[LSP4J] config/getEndpoint: endpoint={}", endpoint)
        await self._send_response(msg_id, {"endpoint": endpoint})

    async def _handle_config_update_endpoint(self, params: dict, msg_id: Any) -> None:
        """处理 config/updateEndpoint 请求。

        接收 GlobalEndpointConfig{endpoint: String}，返回 UpdateConfigResult{success: Boolean}。
        MVP 阶段不做持久化，仅返回成功。
        """
        endpoint = params.get("endpoint", "")
        if endpoint:
            logger.info("[LSP4J] config/updateEndpoint: endpoint={}", endpoint)
        await self._send_response(msg_id, {"success": True})

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
        await self._send_response(msg_id, {
            "requestId": request_id,
            "isSuccess": True,
            "errorCode": 0,
            "errorMessage": "",
        })

        # 2. 构建 prompt
        diff_text = "\n".join(str(d) for d in code_diffs) if code_diffs else ""
        existing_msgs = "\n".join(str(m) for m in commit_messages) if commit_messages else ""
        lang_hint = f"请使用{preferred_language}。" if preferred_language else "请使用中文。"
        prompt = f"根据以下代码变更，生成简洁的 Git commit message。\n{lang_hint}\n\n代码变更:\n{diff_text[:8000]}"
        if existing_msgs:
            prompt += f"\n\n已有的 commit messages:\n{existing_msgs[:2000]}"

        messages = [{"role": "user", "content": prompt}]

        # 3. 定义流式回调
        async def _on_chunk(text: str) -> None:
            if stream:
                await self._send_client_request("commitMsg/answer", {
                    "requestId": request_id,
                    "text": text,
                    "timestamp": int(time.time() * 1000),
                })

        # 4. 调用 call_llm
        try:
            reply = await call_llm(
                model=self._model_obj,
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

        # 5. 非流式模式一次性返回
        if not stream and reply:
            await self._send_client_request("commitMsg/answer", {
                "requestId": request_id,
                "text": reply,
                "timestamp": int(time.time() * 1000),
            })

        # 6. 发送完成通知
        await self._send_client_request("commitMsg/finish", {
            "requestId": request_id,
            "statusCode": 0,
            "reason": "",
        })

    async def _handle_tool_call_results(self, params: dict, msg_id: Any) -> None:
        """处理 tool/call/results 请求（Clawith-only：返回会话内历史）。"""
        session_id = params.get("sessionId", "")
        tool_results = self._tool_call_history_by_session.get(session_id, [])
        await self._send_response(msg_id, {
            "successful": True,
            "errorMessage": "",
            "toolResults": tool_results,
        })

    async def _handle_auth_status(self, params: dict, msg_id: Any) -> None:
        """auth/status → AuthStatus（Clawith-only 固定为已登录可用态）。"""
        _ = params
        await self._send_response(msg_id, {
            "messageId": "",
            "status": 2,  # AuthStateEnum.LOGIN
            "name": "Clawith User",
            "id": str(self._user_id),
            "accountId": str(self._user_id),
            "token": "",
            "quota": 1,
            "whitelist": 3,  # AuthWhitelistStatusEnum.PASS
            "orgId": "",
            "orgName": "Clawith",
            "yxUid": str(self._user_id),
            "avatarUrl": "",
            "userType": "clawith",
            "isSubAccount": False,
            "cloudType": "private",
            "email": "",
            "userTag": "clawith-only",
            "privacyPolicyAgreed": True,
            "isPrivacyPolicyModifiable": False,
        })

    async def _handle_session_get_current(self, params: dict, msg_id: Any) -> None:
        """session/getCurrent → ListCurrentSessionResult。"""
        _ = params
        current_session_ids = [self._session_id] if self._session_id else []
        await self._send_response(msg_id, {
            "errorCode": None,
            "errorMessage": None,
            "currentSessionIds": current_session_ids,
        })

    async def _handle_code_change_apply(self, params: dict, msg_id: Any) -> None:
        """处理 chat/codeChange/apply — 交互式代码变更（diff 渲染入口）。

        当用户点击灵码聊天面板中代码块的 "Apply" 按钮时触发。
        插件期望收到 ChatCodeChangeApplyResult 格式的响应，
        其中 applyCode 为最终要应用到文件的代码内容。

        ChatCodeChangeApplyResult（9 个字段，源码验证）：
        - applyId, projectPath, filePath, applyCode, requestId, sessionId, extra, sessionType, mode

        MVP 策略：直接将 codeEdit 作为 applyCode 返回（无 merge 逻辑）。
        后续可增强：基于 filePath 读取当前文件内容，计算三方 merge。
        """
        apply_id = params.get("applyId", str(uuid.uuid4()))
        code_edit = params.get("codeEdit", "")
        file_path = params.get("filePath", "")
        request_id = params.get("requestId", "")
        session_id = params.get("sessionId", "")

        # diff 入口日志
        logger.info("[LSP4J] codeChange/apply: applyId={} filePath={} requestId={} codeEdit_len={}", apply_id, file_path, request_id, len(code_edit))

        # 空内容警告
        if not code_edit:
            logger.warning("[LSP4J] codeChange/apply: empty codeEdit, applyId={} filePath={}", apply_id, file_path)

        # 构建响应（ChatCodeChangeApplyResult 9 个字段）
        result = {
            "applyId": apply_id,
            "projectPath": params.get("projectPath", ""),
            "filePath": file_path,
            "applyCode": code_edit,   # ← 关键：插件用此渲染 diff
            "requestId": request_id,
            "sessionId": session_id,
            "extra": params.get("extra", ""),
            "sessionType": params.get("sessionType", ""),
            "mode": params.get("mode", ""),
        }
        await self._send_response(msg_id, result)

        # 发送 apply/finish 通知（部分插件版本依赖此通知刷新 diff）
        await self._send_client_request("chat/codeChange/apply/finish", result)
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
    _METHOD_MAP.update({
        # ── chat/ 方法（ChatService.java @JsonSegment("chat")） ──
        "chat/systemEvent": _handle_stub,
        "chat/getStage": _handle_stub,
        "chat/replyRequest": _handle_stub,
        "chat/like": _handle_stub,
        "chat/codeChange/apply": _handle_code_change_apply,     # 完整实现（ChatCodeChangeApplyResult + apply/finish）
        "image/upload": _handle_image_upload,                     # 双响应模式（校验→响应→异步通知）
        "chat/stopSession": _handle_stub,
        "chat/receive/notice": _handle_stub,
        "chat/quota/doNotRemindAgain": _handle_stub,
        "chat/listAllSessions": _handle_chat_list_all_sessions,  # ide_lsp4j 持久化会话列表
        "chat/getSessionById": _handle_chat_get_session_by_id,     # 从 DB 恢复聊天记录
        "chat/deleteSessionById": _handle_chat_delete_session_by_id,  # 删除会话
        "chat/clearAllSessions": _handle_chat_clear_all_sessions,      # 清空会话
        "chat/deleteChatById": _handle_chat_delete_chat_by_id,         # 删除单条消息
        # ── config/ 方法（ConfigService.java） ──
        "config/getEndpoint": _handle_config_get_endpoint,       # 返回 GlobalEndpointConfig
        "config/updateEndpoint": _handle_config_update_endpoint, # 更新端点配置
        # ── commitMsg/ 方法（CommitMessageService.java） ──
        "commitMsg/generate": _handle_commit_msg_generate,       # 生成 commit message（流式）
        # ── tool/ 方法（ToolCallService.java + ToolService.java） ──
        "tool/call/results": _handle_tool_call_results,        # ToolCallService.listToolCallInfo（MVP 空列表）
        # ── 任务规划工具（ToolTypeEnum.java:56-60） ──
        "tool/invoke": _handle_tool_invoke,                    # 工具调用入口（add_tasks/todo_write/search_replace 在此处理）
        # ── agents/ 方法（TestAgentService.java） ──
        "agents/testAgent/stepProcessConfirm": _handle_step_process_confirm,
        # ── textDocument/ 方法（TextDocumentService.java — inline edit，P2-3） ──
        "textDocument/completion": _handle_completion,        # 标准 LSP 补全（返回空列表，避免 -32601）
        "textDocument/preCompletion": _handle_pre_completion,  # IDE 补全预请求（返回 Void）
        "textDocument/inlineEdit": _handle_inline_edit,        # 行内编辑建议（返回 InlineEditResult）
        "textDocument/editPredict": _handle_edit_predict,      # 编辑预测（返回 Void）
        # ── LanguageServer.java 直接定义的 @JsonRequest 方法 ──
        "config/getGlobal": _handle_stub,                     # 全局配置查询
        "config/queryModels": _handle_config_query_models,    # 模型查询（占位列表）
        "ping": _handle_ping,                                 # 心跳 ping
        "ide/update": _handle_stub,                           # IDE 状态更新
        "dataPolicy/query": _handle_stub,                     # 数据政策查询
        "dataPolicy/sign": _handle_stub,                      # 同意数据政策
        "dataPolicy/cancel": _handle_stub,                    # 拒绝数据政策
        "auth/profile/getUrl": _handle_stub,                  # 获取用户资料 URL
        "auth/profile/update": _handle_stub,                  # 更新用户资料
        "extension/query": _handle_stub,                      # 查询自定义命令
        "extension/contextProvider/loadComboBoxItems": _handle_stub,  # 上下文下拉项加载
        "codebase/recommendation": _handle_stub,              # 代码库推荐
        "kb/list": _handle_stub,                              # 知识库列表
        "model/queryClasses": _handle_model_query_classes,     # 查询模型类别
        "model/getByokConfig": _handle_stub,                  # BYOK 配置查询
        "model/checkByokConfig": _handle_stub,                # BYOK 配置校验
        "user/plan": _handle_stub,                            # 用户计划查询
        "webview/command/list": _handle_stub,                 # WebView 命令列表
        # ── @JsonDelegate 服务: AuthService（6 个方法） ──
        "auth/login": _handle_stub,                           # 登录
        "auth/status": _handle_auth_status,                   # Clawith-only 登录态
        "auth/logout": _handle_stub,                          # 登出
        "auth/grantInfos": _handle_stub,                      # 授权信息（@Deprecated）
        "auth/grantInfosWrap": _handle_stub,                  # 授权信息（新版）
        "auth/switchAccount": _handle_stub,                   # 切换账号
        # ── @JsonDelegate 服务: LoginService（1 个方法） ──
        "login/generateUrl": _handle_stub,                    # 生成登录 URL
        # ── @JsonDelegate 服务: FeedbackService（1 个方法） ──
        "feedback/submit": _handle_stub,                      # 提交反馈
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
        "session/getCurrent": _handle_session_get_current,    # 获取当前会话
        # ── @JsonDelegate 服务: SystemService（1 个方法） ──
        "system/reportDiagnosisLog": _handle_stub,            # 上报诊断日志
        # ── @JsonDelegate 服务: SnippetService（2 个方法） ──
        "snippet/search": _handle_stub,                       # 代码片段搜索
        "snippet/report": _handle_stub,                       # 代码片段上报
    })


# ──────────────────────────────────────────────
# 模块级工具调用入口（供 tool_hooks.py 调用）
# ──────────────────────────────────────────────

async def invoke_lsp4j_tool(
    tool_name: str, arguments: dict, agent_id: uuid.UUID, user_id: uuid.UUID
) -> str:
    """通过 LSP4J WebSocket 调用 IDE 端工具。

    由 tool_hooks.py 中的 _lsp4j_aware_execute_tool 调用。
    通过 _active_routers 映射表查找对应 agent 的路由器实例。

    Args:
        tool_name: 插件原生工具名称（read_file, save_file 等）
        arguments: 工具参数字典
        agent_id: 智能体 UUID
        user_id: 用户 UUID

    Returns:
        工具执行结果字符串
    """
    # ── 特殊工具处理（纯 UI 工具，不需要发送到 IDE） ──
    if tool_name in ("add_tasks", "todo_write"):
        # 任务规划工具：直接返回成功响应，插件 AddTasksToolDetailPanel 会自动从工具结果中解析 TaskResponseItem 格式 JSON
        # LLM 会在回复中输出任务列表，插件自动渲染为任务树 UI
        logger.info("[LSP4J-TOOL] 纯 UI 工具: tool={} 返回成功响应", tool_name)
        import json
        # 返回空的成功响应，让插件知道工具调用成功，实际任务内容由 LLM 在 chat/answer 中输出
        return json.dumps({
            "success": True,
            "message": f"{tool_name} 工具调用成功，任务内容由 LLM 在回复中输出",
            "tool_name": tool_name,
        }, ensure_ascii=False)
    
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
                logger.info("[LSP4J-TOOL] 子 Agent 回退: sub_agent={} → primary_agent={} tool={}",
                            agent_id, rk_aid, tool_name)
                router_instance = rk_instance
                break
    if router_instance is None:
        active_keys = [key for key, _ in await list_active_routers()]
        logger.warning("[LSP4J-TOOL] 路由器未找到: agent_key={} active_keys={}",
                        agent_key, active_keys)
        return f"[错误] LSP4J 连接不可用（user_id={user_id}, agent_id={agent_id}）"

    logger.info("[LSP4J-TOOL] 调用 IDE 工具: tool={} agent_key={}", tool_name, agent_key)
    return await router_instance.invoke_tool_on_ide(tool_name, arguments)


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

            # 添加消息
            # ★ 显式设置 created_at，确保用户消息时间戳早于助手消息
            # 避免同一事务中两条消息的 server_default 时间戳相同导致排序错乱
            from datetime import timedelta
            if user_text:
                db.add(ChatMessage(
                    agent_id=agent_id,
                    user_id=user_id,
                    role="user",
                    content=user_text,
                    conversation_id=str(sid_uuid),
                    created_at=now - timedelta(seconds=1),  # 用户消息时间戳早 1 秒
                ))

            if reply_text:
                db.add(ChatMessage(
                    agent_id=agent_id,
                    user_id=user_id,
                    role="assistant",
                    content=reply_text,
                    conversation_id=str(sid_uuid),
                    thinking=thinking_text,  # 构造时传入，非事后更新
                    created_at=now,  # 助手消息时间戳
                ))

            await db.commit()
            # 持久化成功
            logger.debug("[LSP4J] persist success: session_id={} user_len={} reply_len={}", session_id, len(user_text), len(reply_text))

            # 通知 Clawith 前端 WebSocket 刷新 Web UI
            try:
                sid_normalized = str(sid_uuid)
                await ws_module.manager.send_to_session(
                    str(agent_id),
                    sid_normalized,
                    {"type": "done", "role": "assistant", "content": reply_text},
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


async def _load_lsp4j_history_from_db(
    session_id: str, agent_id: uuid.UUID, user_id: uuid.UUID
) -> list[dict]:
    """从数据库加载 LSP4J 对话历史。

    验证 session_id UUID + 所有权后返回历史消息列表。

    Args:
        session_id: 会话 ID（UUID 格式字符串）
        agent_id: 智能体 UUID
        user_id: 用户 UUID

    Returns:
        历史消息列表 [{"role": "user"/"assistant", "content": "..."}]
    """
    try:
        sid_uuid = uuid.UUID(session_id)
    except ValueError:
        return []

    async with async_session() as db:
        sr = await db.execute(select(ChatSession).where(ChatSession.id == sid_uuid))
        sess = sr.scalar_one_or_none()
        if not sess or sess.user_id != user_id or sess.agent_id != agent_id:
            if sess:
                logger.warning(
                    "LSP4J hydrate denied: session=%s user=%s agent=%s",
                    session_id, user_id, agent_id,
                )
            return []

        mr = await db.execute(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == str(sid_uuid))
            .where(ChatMessage.agent_id == agent_id)
            .where(ChatMessage.user_id == user_id)
            .where(ChatMessage.role.in_(("user", "assistant")))
            .order_by(ChatMessage.created_at.asc())
        )
        rows = mr.scalars().all()
        # 历史加载成功
        logger.debug("[LSP4J] history loaded from DB: session_id={} count={}", session_id, len(rows))
        return [{"role": m.role, "content": m.content} for m in rows]
