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
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone as tz_utc
from typing import Any

from loguru import logger
from sqlalchemy import select

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
    _active_routers,
)
from .lsp_protocol import LSPBaseProtocolParser, ParseError

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

        # 连接关闭标记（防止断开后继续发送消息）
        self._closed: bool = False

        # 图片上传缓存: request_id → (image_url, base64_data, timestamp)
        self._image_cache: dict[str, tuple[str, str, float]] = {}
        self._image_cache_max_size: int = 10
        self._image_cache_ttl: float = 600.0  # 10 分钟
        self._image_cleanup_task: asyncio.Task | None = None

        # 已取消请求的 RPC ID 集合（防止超时后迟达响应干扰，OrderedDict 保证 FIFO）
        self._cancelled_requests: dict[int, None] = {}
        self._MAX_CANCELLED_REQUESTS_SIZE: int = 100

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

        # 3. 非核心方法通用处理
        if method in (
            "initialized",               # 生命周期通知，无需响应
            "textDocument/didOpen",       # 文档同步（通义灵码自动发送，忽略）
            "textDocument/didChange",
            "textDocument/didClose",
            "textDocument/didSave",
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

        # 从 rootUri 提取 projectPath（兼容 file:/// 和 file://localhost/ 格式）
        root_uri = params.get("rootUri", "")
        if root_uri:
            try:
                import urllib.parse
                import os
                parsed = urllib.parse.urlparse(root_uri)
                # file:///path 或 file://localhost/path
                path = parsed.path
                if path:
                    # 安全校验：禁止路径穿越
                    norm_path = os.path.normpath(path)
                    if ".." in norm_path.split(os.sep):
                        logger.warning("[LSP4J-LIFE] rootUri 含路径穿越，忽略: {}", root_uri)
                    else:
                        self._project_path = norm_path
                        logger.info("[LSP4J-LIFE] projectPath 从 rootUri 提取: {}", self._project_path)
            except Exception as e:
                logger.warning("[LSP4J-LIFE] rootUri 解析失败: {} error={}", root_uri, e)

        await self._send_response(msg_id, {
            "capabilities": {
                "textDocumentSync": {"openClose": True, "change": 1},
                "completionProvider": {"resolveProvider": False, "triggerCharacters": ["."]},
            },
            "serverInfo": {"name": "Clawith LSP4J", "version": "0.1.0"},
        })

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

            async def on_chunk(text: str) -> None:
                """流式文本回调 — 推送 chat/answer（ChatAnswerParams 格式）"""
                # 检查取消事件，若用户已发送 chat/stop 则中断流式输出
                if self._cancel_event and self._cancel_event.is_set():
                    # 用户取消流式输出
                    logger.info("[LSP4J] on_chunk: cancelled by chat/stop, chunks_sent={}", len(reply_parts))
                    raise asyncio.CancelledError("chat/stop requested")
                reply_parts.append(text)
                # 首次 chunk 用 info（可观测），后续用 debug（避免高频噪音）
                if len(reply_parts) == 1:
                    logger.info("[LSP4J] on_chunk: first_chunk text_len={} requestId={}", len(text), request_id)
                else:
                    logger.debug("[LSP4J] on_chunk: chunk_count={} text_len={}", len(reply_parts), len(text))
                # 仅在流式模式下推送 chat/answer（非流式模式在 finish 中一次性返回）
                if getattr(self, "_stream_mode", True):
                    await self._send_chat_answer(session_id, text, request_id)

            async def on_thinking(text: str) -> None:
                """推理过程回调 — 推送 chat/think（ChatThinkingParams 格式）+ 累积 thinking 文本"""
                nonlocal _thinking_started
                thinking_chunks.append(text)
                if not _thinking_started:
                    _thinking_started = True
                    await self._send_chat_think(session_id, "思考中...", "start", request_id)

            async def on_tool_call(data: dict) -> None:
                """工具调用回调 — 推送状态通知给 IDE + step callback"""
                status = data.get("status", "running")
                tool_name = data.get("name", "unknown")
                if status == "running":
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

    async def _handle_tool_call_approve(self, params: dict, msg_id: Any) -> None:
        """处理 tool/callApprove 请求。

        MVP 阶段自动批准所有工具调用权限。
        后续迭代可增加权限审批 UI。
        """
        # 工具审批日志
        logger.info("[LSP4J-TOOL] 工具审批: toolCallId={} name={} → 自动批准", params.get("toolCallId"), params.get("name"))
        # 返回空结果表示批准
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
            logger.warning("LSP4J: tool/invokeResult 缺少 toolCallId")
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

        # tool/invokeResult 是 @JsonRequest，需要返回响应
        await self._send_response(msg_id, {"status": "ok"})

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
        await self._send_notification("tool/call/sync", {
            "sessionId": session_id or "",
            "requestId": request_id,
            "projectPath": self._project_path,
            "toolCallId": tool_call_id,
            "toolCallStatus": tool_call_status,
            "parameters": parameters or {},
            "results": self._wrap_results(results),
            "errorCode": error_code,
            "errorMsg": error_msg,
        })

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
        tool_call_id = str(uuid.uuid4())
        request_id = self._current_request_id or str(uuid.uuid4())

        logger.info("[LSP4J-TOOL] invoke_tool_on_ide: tool={} callId={} requestId={} timeout={}",
                     tool_name, tool_call_id[:8], request_id[:8], timeout)

        # 发送 PENDING → RUNNING sync 通知
        await self._send_tool_call_sync(
            self._session_id, request_id, tool_call_id,
            "PENDING", tool_name=tool_name, parameters=arguments,
        )
        await self._send_tool_call_sync(
            self._session_id, request_id, tool_call_id,
            "RUNNING", tool_name=tool_name, parameters=arguments,
        )

        # 创建 Future 等待异步结果（通过 tool/invokeResult 回传）
        loop = asyncio.get_running_loop()
        tool_future: asyncio.Future = loop.create_future()
        self._pending_tools[tool_call_id] = tool_future
        logger.info("[LSP4J-TOOL] Future registered: toolCallId={} pending_count={} waiting for invokeResult",
                     tool_call_id[:8], len(self._pending_tools))

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
                "name": tool_name,         # 插件原生名称，不是 ide_ 前缀
                "parameters": arguments,   # 不是 arguments 字段名，是 parameters
                "async": True,             # 异步执行，结果通过 invokeResult 回传
            },
        })

        # 等待异步结果
        try:
            result = await asyncio.wait_for(tool_future, timeout=timeout)
            # 工具执行完成，发送 FINISHED sync 通知
            # results 字符串由 _wrap_results 自动包装为 [{"content": "..."}]
            await self._send_tool_call_sync(
                self._session_id, request_id, tool_call_id,
                "FINISHED", tool_name=tool_name, results=result[:500] if result else None,
            )
            return result
        except asyncio.TimeoutError:
            logger.warning("LSP4J: 工具调用超时 tool={} callId={}", tool_name, tool_call_id)
            # 记录已取消的 RPC ID，防止迟达 ack 响应干扰日志
            self._cancelled_requests[rpc_id] = None
            if len(self._cancelled_requests) > self._MAX_CANCELLED_REQUESTS_SIZE:
                self._cancelled_requests.pop(next(iter(self._cancelled_requests)))
            # 工具超时，发送 ERROR sync 通知
            await self._send_tool_call_sync(
                self._session_id, request_id, tool_call_id,
                "ERROR", tool_name=tool_name, error_msg=f"工具 {tool_name} 执行超时（{timeout}s）",
            )
            return f"[超时] 工具 {tool_name} 执行超时（{timeout}s）"
        finally:
            self._pending_tools.pop(tool_call_id, None)

    # ──────────────────────────────────────────
    # 消息发送方法（严格匹配插件协议字段）
    # ──────────────────────────────────────────

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
        # 构建 extra 字段（ChatAnswerParams.extra 类型为 Map<String, String>）
        extra: dict[str, str] = {}
        session_type = getattr(self, "_current_session_type", "")
        if session_type:
            extra["sessionType"] = session_type

        # chat/answer 发送追踪
        logger.debug("[LSP4J] chat/answer: requestId={} text_len={}", request_id, len(text))

        await self._send_notification("chat/answer", {
            "requestId": request_id,
            "sessionId": session_id or "",
            "text": text,
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
        await self._send_notification("chat/think", {
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
        await self._send_notification("chat/finish", {
            "requestId": request_id,
            "sessionId": session_id or "",
            "reason": reason,
            "statusCode": status_code,
            "fullAnswer": full_answer,
        })

    async def _send_session_title_update(
        self, session_id: str | None, title: str
    ) -> None:
        """发送 session/title/update 通知（SessionTitleRequest 格式）。

        参数：sessionId, sessionTitle
        """
        await self._send_notification("session/title/update", {
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
        await self._send_notification("chat/process_step_callback", {
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

        注意：chat/answer, chat/think, chat/finish 在插件中是 @JsonRequest，
        但由于 LSP4J 的请求-响应模式，我们发送带 id 的请求。
        如果插件不回响应会导致超时，所以这里改用通知方式避免阻塞。
        实际上 LSP4J Launcher 会自动处理未匹配的响应。
        """
        await self._send_message({
            "jsonrpc": "2.0",
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

        # 清理已取消请求记录
        self._cancelled_requests.clear()

    # ──────────────────────────────────────────
    # 存根与扩展方法
    # ──────────────────────────────────────────

    async def _handle_stub(self, params: dict, msg_id: Any) -> None:
        """通用存根处理器 — 返回空成功响应，避免 Method not found 错误。

        用于 chat/listAllSessions、chat/like 等暂不实现的方法。
        注意：这些方法在 ChatService.java 中定义为 @JsonRequest，必须返回响应，
        否则 LSP4J 框架会超时等待。
        """
        await self._send_response(msg_id, {})

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
        # 返回当前服务端地址（MVP：返回空字符串，插件会使用默认值）
        await self._send_response(msg_id, {"endpoint": ""})

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
                await self._send_notification("commitMsg/answer", {
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
            await self._send_notification("commitMsg/answer", {
                "requestId": request_id,
                "text": reply,
                "timestamp": int(time.time() * 1000),
            })

        # 6. 发送完成通知
        await self._send_notification("commitMsg/finish", {
            "requestId": request_id,
            "statusCode": 0,
            "reason": "",
        })

    async def _handle_tool_call_results(self, params: dict, msg_id: Any) -> None:
        """处理 tool/call/results 请求 — MVP 阶段返回空列表。

        参数格式（GetSessionToolCallRequest）：sessionId
        返回格式（ListToolCallInfoResponse）：toolCalls, sessionId, totalCount

        后续迭代可实现完整的工具调用历史查询。
        """
        session_id = params.get("sessionId", "")
        await self._send_response(msg_id, {
            "toolCalls": [],
            "sessionId": session_id,
            "totalCount": 0,
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
        await self._send_notification("chat/codeChange/apply/finish", result)
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
        "chat/listAllSessions": _handle_stub,                    # 插件打开聊天面板时调用
        "chat/getSessionById": _handle_stub,                     # 查看历史会话时调用
        "chat/deleteSessionById": _handle_stub,                  # 删除会话时调用
        "chat/clearAllSessions": _handle_stub,                   # 清空所有会话时调用
        "chat/deleteChatById": _handle_stub,                     # 删除单条消息时调用
        # ── config/ 方法（ConfigService.java） ──
        "config/getEndpoint": _handle_config_get_endpoint,       # 返回 GlobalEndpointConfig
        "config/updateEndpoint": _handle_config_update_endpoint, # 更新端点配置
        # ── commitMsg/ 方法（CommitMessageService.java） ──
        "commitMsg/generate": _handle_commit_msg_generate,       # 生成 commit message（流式）
        # ── tool/ 方法（ToolCallService.java + ToolService.java） ──
        "tool/call/results": _handle_tool_call_results,        # ToolCallService.listToolCallInfo（MVP 空列表）
        # ── agents/ 方法（TestAgentService.java） ──
        "agents/testAgent/stepProcessConfirm": _handle_step_process_confirm,
        # ── textDocument/ 方法（TextDocumentService.java — inline edit，P2-3） ──
        "textDocument/inlineEdit": _handle_stub,               # 旧版 NES 行内编辑建议
        "textDocument/editPredict": _handle_stub,              # 新版 NES 编辑预测
        # ── LanguageServer.java 直接定义的 @JsonRequest 方法 ──
        "config/getGlobal": _handle_stub,                     # 全局配置查询
        "config/queryModels": _handle_stub,                   # 模型查询
        "ping": _handle_stub,                                 # 心跳 ping
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
        "model/queryClasses": _handle_stub,                   # 查询模型类别
        "model/getByokConfig": _handle_stub,                  # BYOK 配置查询
        "model/checkByokConfig": _handle_stub,                # BYOK 配置校验
        "user/plan": _handle_stub,                            # 用户计划查询
        "webview/command/list": _handle_stub,                 # WebView 命令列表
        # ── @JsonDelegate 服务: AuthService（6 个方法） ──
        "auth/login": _handle_stub,                           # 登录
        "auth/status": _handle_stub,                          # 登录状态
        "auth/logout": _handle_stub,                          # 登出
        "auth/grantInfos": _handle_stub,                      # 授权信息（@Deprecated）
        "auth/grantInfosWrap": _handle_stub,                  # 授权信息（新版）
        "auth/switchAccount": _handle_stub,                   # 切换账号
        # ── @JsonDelegate 服务: LoginService（1 个方法） ──
        "login/generateUrl": _handle_stub,                    # 生成登录 URL
        # ── @JsonDelegate 服务: FeedbackService（1 个方法） ──
        "feedback/submit": _handle_stub,                      # 提交反馈
        # ── @JsonDelegate 服务: SnapshotService（2 个方法） ──
        "snapshot/listBySession": _handle_stub,               # 按会话列出快照
        "snapshot/operate": _handle_stub,                     # 快照操作
        # ── @JsonDelegate 服务: WorkingSpaceFileService（5 个方法） ──
        "workingSpaceFile/operate": _handle_stub,             # 工作区文件操作
        "workingSpaceFile/listBySnapshot": _handle_stub,      # 按快照列出文件
        "workingSpaceFile/getLastStableContent": _handle_stub, # 获取最后稳定内容
        "workingSpaceFile/getFullContent": _handle_stub,      # 获取完整内容
        "workingSpaceFile/updateContent": _handle_stub,       # 更新文件内容
        # ── @JsonDelegate 服务: SessionService（1 个方法） ──
        "session/getCurrent": _handle_stub,                   # 获取当前会话
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
    # 使用 (user_id, agent_id) 复合键查找路由器，确保不同用户的连接不会互相干扰
    agent_key = (str(user_id), str(agent_id))
    router_instance = _active_routers.get(agent_key)
    if router_instance is None:
        logger.warning("[LSP4J-TOOL] 路由器未找到: agent_key={} active_keys={}",
                        agent_key, list(_active_routers.keys()))
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
                    client_type="ide_plugin",
                    created_at=now,
                    last_message_at=now,
                )
                db.add(sess)
                # TODO(P2-4): ChatSession 还有 project_path / current_file / open_files 字段，
                # 待插件未来发送对应数据后可直接利用，当前不增加死代码
            else:
                sess.last_message_at = now

            # 添加消息
            if user_text:
                db.add(ChatMessage(
                    agent_id=agent_id,
                    user_id=user_id,
                    role="user",
                    content=user_text,
                    conversation_id=str(sid_uuid),
                ))

            if reply_text:
                db.add(ChatMessage(
                    agent_id=agent_id,
                    user_id=user_id,
                    role="assistant",
                    content=reply_text,
                    conversation_id=str(sid_uuid),
                    thinking=thinking_text,  # 构造时传入，非事后更新
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
