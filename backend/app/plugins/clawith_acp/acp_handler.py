"""ACP JSON-RPC 2.0 协议处理 — Agent 路由到 call_llm_with_failover。

直接对接 Clawith 已有 LLM 调用链:
  call_llm_with_failover (caller.py:691) + build_agent_context (agent_context.py:243)
"""

import asyncio
import contextvars
import json
import os
import re
import time
import uuid
from typing import Any

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.core.logging_config import get_trace_id
from app.core.permissions import build_visible_agents_query
from app.models.agent import Agent as AgentModel
from app.models.org import OrgMember
from app.models.user import User as UserModel
from app.plugins.clawith_acp.acp_session import AcpSessionManager
from app.plugins.clawith_acp.acp_document import (
    DOCUMENT_NOTIFICATION_METHODS,
    document_store,
    handle_document_notification,
)
from app.plugins.clawith_acp.acp_features import acp_feature_enabled
from app.plugins.clawith_acp.acp_nes import (
    handle_nes_accept_reject,
    handle_nes_close,
    handle_nes_start,
    handle_nes_suggest,
)
from app.plugins.clawith_acp.tool_bridge import current_acp_handler, is_agent_internal_path
from app.plugins.clawith_acp.tool_hooks import install_acp_tool_hooks
from app.plugins.clawith_acp.turn_budget import (
    BudgetExceededError,
    PERMISSION_GATED_METHODS,
    TurnBudget,
    get_turn_budget,
    set_turn_budget,
)

ACP_PROTOCOL_VERSION = 1
# 向后兼容：未设 ACP_COMPUTE_BUDGET_SECONDS 时 TurnBudget.from_env 读取此值
LLM_TIMEOUT_SECONDS = int(os.getenv("ACP_LLM_TIMEOUT_SECONDS", "600"))

# ── 背压控制：读写分离阈值 ──────────────────────────────────────
# 读操作(VFS/索引/搜索)响应快(&lt;100ms), 允许更高并发
# 写操作(write/build/reformat)需 EDT+WriteAction, 保持保守阈值
_READ_BACKPRESSURE_THRESHOLD = 15
_WRITE_BACKPRESSURE_THRESHOLD = 5
# 需 IDE 权限弹窗的 RPC — 超时须与 ACP_PERMISSION_TIMEOUT(120s) 对齐
_PERMISSION_GATED_METHODS = PERMISSION_GATED_METHODS
_READ_METHODS = frozenset({
    "fs/read_text_file", "fs/list_directory", "fs/find_file",
    "fs/search_text", "fs/find_class", "fs/find_symbol",
    "fs/find_references", "fs/find_definition", "fs/find_implementations",
    "fs/find_super_methods", "fs/file_structure", "fs/get_documentation",
    "fs/call_hierarchy", "fs/type_hierarchy", "fs/diagnostics",
    "ide/active_file", "ide/index_status",
})

_CHUNK_HARD_FLUSH = 240   # 硬上限: 缓冲区达 240 字立即发送
_CHUNK_SOFT_FLUSH = 120   # 软上限: 120 字 + 空白边界发送
_CHUNK_IDLE_MIN = 4       # idle 最小阈值: 极短回复也及时 flush
# 渐进式 idle flush: 短回复 50ms, 长回复 100ms — 流式流畅度核心参数
_CHUNK_IDLE_SEC_SHORT = 0.05
_CHUNK_IDLE_SEC_LONG = 0.10
# 时间驱动 periodic flush: 每 60ms 兜底发送 (~16fps), 不依赖内容边界
_PERIODIC_FLUSH_SEC = 0.06
# 中英文句末标点均触发句子边界 flush

_ACP_A2A_OBS_LOG = os.getenv(
    "DEBUG_SESSION_LOG_PATH",
    "/data/agents/.debug/debug-f3071f.log" if os.path.isdir("/data/agents")
    else "/tmp/clawith-debug.log",
)


def _acp_a2a_obs_log(hypothesis_id: str, location: str, message: str, **data) -> None:
    entry = {
        "sessionId": "f3071f",
        "timestamp": int(time.time() * 1000),
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data,
    }
    logger.info("[ACP-A2A-OBS] {} {} {}", hypothesis_id, message, data)
    try:
        os.makedirs(os.path.dirname(_ACP_A2A_OBS_LOG), exist_ok=True)
        with open(_ACP_A2A_OBS_LOG, "a", encoding="utf-8") as _f:
            _f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass

_SENTENCE_END_RE = re.compile(r'[.!?。！？…~]["\'」』）\)]*\s*$')
_SPLIT_NL_RE = re.compile(r"(?<=\n)")

# ── LLM 输出脱敏 ──────────────────────────────────────────────

# Install ACP tool hooks (idempotent)
install_acp_tool_hooks()
# 工具名 → ACP ToolKind 映射 + 中文显示名，统一在 acp_routes.py 管理
from app.plugins.clawith_acp.acp_routes import ACP_KIND_MAP, ACP_TOOL_CN_NAME as _TOOL_CN_NAME

_kind_map = ACP_KIND_MAP

class _ThinkingCoalescer:
    """ACP 专用：仅转发 phase hint，reasoning 正文不推 WebSocket。"""

    def __init__(self, handler: "AcpHandler"):
        self._handler = handler
        self._enabled = os.getenv("ACP_THINKING_COALESCE_ENABLED", "true").lower() == "true"
        self._phase_sent: set[str] = set()

    def on_new_prompt(self) -> None:
        self._phase_sent.clear()

    @staticmethod
    def _is_phase_hint(text: str) -> bool:
        prefix = text[:40]
        return "轮" in prefix or "round" in prefix.lower()

    async def handle(self, text: str) -> None:
        if not self._enabled or not text:
            return
        if self._is_phase_hint(text):
            key = text[:80]
            if key in self._phase_sent:
                logger.info("[ACP-THINK] suppressed duplicate phase={}", key[:40])
                return
            self._phase_sent.add(key)
            await self._handler._push_thinking(text)
            return
        logger.info("[ACP-THINK] suppressed reasoning len={}", len(text))


class AcpHandler:
    """ACP JSON-RPC 2.0 路由 + Agent 管理。"""

    def __init__(self, websocket, user_id: str):
        self.ws = websocket
        self.user_id = user_id
        # 短连接 ID，便于 docker logs 对齐 IDE gen 与后端 handler
        self.conn_id = uuid.uuid4().hex[:8]
        self._start_time = time.monotonic()  # 连接建立时间, 用于 disconnect 日志计算持续时间
        self._tenant_id: uuid.UUID | None = None  # 惰性加载, 通过 _resolve_tenant_id() 获取
        self.session_id: str | None = None
        self.agent_id: str | None = None
        self.agent_name: str = "ACP Agent"
        self.role_description: str = ""
        self.session_mgr = AcpSessionManager()
        self._active_tasks: dict[str, asyncio.Task] = {}
        self._terminal_events: dict[str, asyncio.Event] = {}
        self._pending_tools: dict[str, asyncio.Future] = {}
        self._pending_requests: dict[str, tuple[asyncio.Future, float]] = {}
        self._pending_lock = asyncio.Lock()
        self._cancel_event: asyncio.Event | None = None
        self._closing = False  # 关闭保护: 标记 WebSocket 正在关闭, 拒绝新 dispatch
        self._close_lock = asyncio.Lock()  # 防止并发 close()/cleanup()
        self._cwd: str = ""  # IDE project root
        # 当前 prompt 的性能计数，供 [ACP-PERF] prompt_done / first_chunk 使用
        self._current_prompt_perf: dict | None = None
        # 熔断器: per-instance 滑动窗口错误率熔断, IDE 异常时自动切断
        self._circuit = CircuitBreaker()
        # 礼貌延迟: 请求间最小间隔 (16ms = 1 EDT frame @ 60fps)
        self._last_send_ts: float = 0.0
        self._polite_lock = asyncio.Lock()
        self._chunk_buffer = ""
        self._chunk_idle_task: asyncio.Task | None = None
        # 参考 gptme PR #1586: 时间驱动周期性 flush, 确保 LLM 流式输出期间
        # 不依赖内容边界即可每 150ms 发送一次文本到插件端
        self._periodic_flush_task: asyncio.Task | None = None
        self._thinking_coalescer = _ThinkingCoalescer(self)
        self._chunk_template = json.dumps({
            "jsonrpc": "2.0", "method": "session/update",
            "params": {
                "sessionId": "__SID__",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "__TEXT__"},
                },
            },
        }, ensure_ascii=False)
    async def run(self):
        """消息循环: 接收 JSON-RPC → 分发 → 返回响应。"""
        token = current_acp_handler.set(self)
        client_ip = "?"
        try:
            if hasattr(self.ws, "client") and self.ws.client:
                client_ip = self.ws.client.host
            elif hasattr(self.ws, "scope"):
                client_ip = self.ws.scope.get("client", ("?", 0))[0]
        except Exception:
            pass
        logger.info("[ACP-CONN] connect conn_id={} user={} ip={}", self.conn_id, self.user_id[:8] if self.user_id else "?", client_ip)

        self._last_active = time.monotonic()
        watchdog = asyncio.create_task(self._keepalive_watchdog())
        try:
            async for raw in self.ws.iter_text():
                self._last_active = time.monotonic()
                if "session/new" in raw:
                    logger.info(f"[ACP-RAW-IN] session/new len={len(raw)}")
                try:
                    request = json.loads(raw)
                except json.JSONDecodeError:
                    await self._send_error(None, -32700, "Parse error")
                    continue

                method = request.get("method")
                msg_id = request.get("id")

                # JSON-RPC response (id + result/error, no method): dispatch to pending requests
                if method is None and msg_id is not None and ("result" in request or "error" in request):
                    entry = self._pending_requests.pop(msg_id, None)
                    future = entry[0] if entry else None
                    if future and not future.done():
                        if "error" in request:
                            err = request["error"]
                            future.set_exception(RuntimeError(
                                f"ACP error {err.get('code', -1)}: {err.get('message', 'unknown')}"
                            ))
                        else:
                            future.set_result(request.get("result"))
                    continue

                params = request.get("params", {})
                session_id_param = params.get("sessionId") or params.get("session_id")
                if session_id_param and not self.session_id:
                    self.session_id = str(session_id_param)

                logger.debug(f"[ACP conn={self.conn_id}] method={method} id={msg_id}")

                if msg_id is None and method:
                    # ACP SDK ClientSession.cancel() 发送 session/cancel 通知（无 JSON-RPC id）
                    if method in ("session/cancel", "$/cancelRequest"):
                        await self._handle_cancel(params)
                        continue
                    if acp_feature_enabled("document") and method in DOCUMENT_NOTIFICATION_METHODS:
                        handle_document_notification(self.session_id, method, params)
                        continue
                    if acp_feature_enabled("nes") and method in ("nes/accept", "nes/reject"):
                        handle_nes_accept_reject(self.session_id, method, params)
                        continue
                    logger.debug("[ACP] 忽略无 id 通知: method={}", method)
                    continue

                try:
                    if method == "initialize":
                        result = await self._handle_initialize(params)
                    elif method == "session/new":
                        result = await self._handle_session_new(params)
                    elif method == "session/load":
                        result = await self._handle_session_load(params)
                    elif method == "session/resume":
                        result = await self._handle_session_load(params)
                    elif method == "session/prompt":
                        # R1: 禁止在读循环内 await 长时 prompt，否则 send_request 无法读入 IDE 工具响应（死锁）
                        if not self.session_id:
                            await self._send_error(msg_id, -32002, "No active session")
                            continue
                        messages = params.get("messages")
                        if messages is None:
                            prompt = params.get("prompt", [])
                            messages = [{"role": "user", "content": prompt}] if prompt else []
                        if not messages:
                            await self._send_error(msg_id, -32602, "messages 不能为空")
                            continue
                        if self._has_in_flight_prompt():
                            logger.warning(f"[ACP] 拒绝并发 prompt: session={self.session_id}")
                            await self._send_error(msg_id, -32000, "Prompt already in progress")
                            continue
                        dispatch_key = (
                            f"dispatch-{msg_id}" if msg_id is not None else f"dispatch-{uuid.uuid4()}"
                        )
                        task = asyncio.create_task(
                            self._dispatch_prompt(params, msg_id, dispatch_key)
                        )
                        self._active_tasks[dispatch_key] = task
                        continue
                    elif method == "$/cancelRequest":
                        await self._handle_cancel(params)
                        continue
                    elif method == "session/cancel":
                        result = await self._handle_cancel(params)
                    elif method == "session/close":
                        result = await self._handle_session_close(params)
                    elif method == "_clawith/list_agents":
                        result = await self._handle_list_agents(params)
                    elif method == "_clawith/set_agent":
                        result = await self._handle_set_agent(params)
                    elif method == "_clawith/last_assistant":
                        result = await self._handle_last_assistant(params)
                    elif method == "_clawith/session_meta":
                        result = await self._handle_session_meta(params)
                    elif method == "_clawith/tool_result":
                        result = await self._handle_tool_result(params)
                    elif method == "session/set_mode":
                        result = await self._handle_set_mode(params)
                    elif method == "session/set_model":
                        result = await self._handle_set_model(params)
                    elif method == "providers/list":
                        result = await self._handle_providers_list(params)
                    elif method == "providers/set":
                        result = await self._handle_providers_set(params)
                    elif method == "logout":
                        result = await self._handle_logout(params)
                    elif method == "nes/start":
                        result = await handle_nes_start(self.session_id, params)
                    elif method == "nes/suggest":
                        result = await handle_nes_suggest(self.session_id, params)
                    elif method == "nes/close":
                        result = await handle_nes_close(self.session_id, params)
                    else:
                        await self._send_error(msg_id, -32601, f"Method not found: {method}")
                        continue

                    if msg_id is not None:
                        await self._send_result(msg_id, result)
                except Exception as e:
                    logger.error(f"[ACP] {method} 失败: {type(e).__name__}: {e}", exc_info=True)
                    await self._send_error(msg_id, -32603, "Internal error")
        finally:
            watchdog.cancel()
            current_acp_handler.reset(token)

    async def _keepalive_watchdog(self):
        """后台任务：每 30s 检查连接活性，120s 无消息主动关闭。"""
        while True:
            await asyncio.sleep(30)
            if time.monotonic() - self._last_active > 120:
                logger.warning("[ACP] keepalive timeout conn={}", self.conn_id)
                await self.ws.close(code=1001)
                break

    def _has_in_flight_prompt(self) -> bool:
        """同连接是否已有在途 prompt（含派发 task 与 LLM task）。"""
        return any(not task.done() for task in self._active_tasks.values())

    async def _dispatch_prompt(self, params: dict, msg_id, dispatch_key: str | None = None):
        """在独立 task 中执行 prompt，结束后回写 JSON-RPC result。

        必须从 run() 读循环外调用，否则 LLM 工具 send_request 无法读入 IDE 响应（R1 死锁）。
        """
        # WebSocket 关闭保护: 拒绝新 dispatch, 避免 "Unexpected ASGI message" 错误
        if self._closing:
            logger.warning(
                f"[ACP] dispatch_prompt 跳过 (正在关闭): "
                f"session={self.session_id} id={msg_id}"
            )
            if msg_id is not None:
                await self._send_error(msg_id, -32000, "WebSocket is closing")
            return
        logger.info(f"[ACP] prompt 派发开始: session={self.session_id} id={msg_id}")
        try:
            result = await self._handle_prompt(params, msg_id)
            if msg_id is not None and result is not None:
                await self._send_result(msg_id, result)
        except asyncio.CancelledError:
            logger.warning(
                f"[ACP] dispatch_prompt 取消: session={self.session_id} id={msg_id}"
            )
            if msg_id is not None:
                await self._send_error(msg_id, -32800, "Prompt cancelled")
        except TimeoutError:
            logger.error(
                f"[ACP] dispatch_prompt 超时: session={self.session_id} id={msg_id}"
            )
            if msg_id is not None:
                await self._send_error(msg_id, -32000, "LLM timeout")
        except Exception as e:
            logger.error(
                f"[ACP] dispatch_prompt 失败: session={self.session_id} id={msg_id} "
                f"exception={type(e).__name__}: {e}"
            )
            if msg_id is not None:
                await self._send_error(msg_id, -32603, f"Internal error: {type(e).__name__}")
        finally:
            if dispatch_key:
                self._active_tasks.pop(dispatch_key, None)
            perf = self._current_prompt_perf
            if perf is None:
                logger.warning(
                    f"[ACP-PERF] prompt_done conn={self.conn_id} session={self.session_id} "
                    f"total_ms=-1 pushed_chunks=0 first_chunk_ms=-1 reply_chars=0 perf_missing=true"
                )
            else:
                total_ms = (time.perf_counter() - perf["start"]) * 1000
                first_ms = perf.get("first_chunk_ms")
                first_ms_s = f"{first_ms:.0f}" if first_ms is not None else "-1"
                logger.info(
                    f"[ACP-PERF] prompt_done conn={self.conn_id} session={self.session_id} "
                    f"total_ms={total_ms:.0f} pushed_chunks={perf.get('pushed_chunks', 0)} "
                    f"first_chunk_ms={first_ms_s} reply_chars={perf.get('reply_chars', 0)} perf_missing=false"
                )
                self._current_prompt_perf = None
            await self._flush_chunk_buffer()
            logger.info(f"[ACP] prompt 派发结束: session={self.session_id} id={msg_id}")

    # ── ACP 方法实现 ──────────────────────────────────────────

    async def _handle_initialize(self, params: dict) -> dict:
        logger.info("[ACP] initialize")
        return {
            "protocolVersion": ACP_PROTOCOL_VERSION,
            "capabilities": {
                "prompt": {"text": True},
                "fs": {
                    "readTextFile": True, "writeTextFile": True,
                    "findFile": True, "searchText": True,
                    "findClass": True, "findSymbol": True,
                    "findReferences": True, "findDefinition": True,
                    "findImplementations": True, "findSuperMethods": True,
                    "callHierarchy": True, "typeHierarchy": True,
                    "diagnostics": True, "fileStructure": True,
                    "safeDelete": True, "refactorRename": True,
                    "moveFile": True, "reformatCode": True,
                    "optimizeImports": True, "convertJavaToKotlin": True,
                    "listDirectory": True,
                },
                "ide": {
                    "indexStatus": True, "syncFiles": True,
                    "activeFile": True, "openFile": True,
                },
                "git": {"status": True, "diff": True, "stage": True, "commit": True},
                "terminal": True,
            },
            # ACP 协议规范字段名为 implementation, 非 agentInfo。
            # 插件端 Client.initialize() 通过 ServerInfo.implementation 读取此字段。
            "implementation": {"name": "clawith-acp-agent", "version": "0.1.0"},
        }

    async def _handle_session_new(self, params: dict) -> dict:
        cwd = params.get("cwd", "")

        # Accept agentId from client _meta
        agent = None
        client_agent_id = (params.get("_meta") or {}).get("agentId")
        # 诊断日志: 查看 session/new 的实际参数, 确认 _meta 是否正确到达
        logger.info(f"[ACP] session/new params keys={list(params.keys())} _meta={params.get('_meta')!r}")
        if client_agent_id:
            agent = await self._find_agent_by_id(client_agent_id)
            if agent:
                logger.info(f"[ACP] session/new: using client agent={client_agent_id}")

        if not agent and self.agent_id:
            # 用户已通过 _clawith/set_agent 选择了智能体（Phase 2），
            # 优先使用选定的智能体而非 _find_agent() 的默认排序
            agent = await self._find_agent_by_id(self.agent_id)
            if agent:
                logger.info(f"[ACP] session/new: using previously set agent={self.agent_id} ({agent.name})")
        if not agent:
            agent = await self._find_agent()
        if not agent:
            raise ValueError("No available agent")

        self.agent_id = str(agent.id)
        self.agent_name = agent.name if agent else "ACP Agent"
        self.role_description = agent.role_description if agent else ""
        self._cwd = cwd

        session_id = await self.session_mgr.create(
            user_id=self.user_id,
            agent_id=self.agent_id,
            tenant_id=str(agent.tenant_id),
            cwd=cwd,
        )
        self.session_id = session_id
        logger.info(f"[ACP] session/new: id={session_id} agent={self.agent_id} ({self.agent_name})")
        return {"sessionId": session_id, "cwd": cwd}

    async def _handle_session_load(self, params: dict) -> dict:
        session_id = params.get("sessionId", "")
        result = await self.session_mgr.load(session_id, self.user_id)
        if not result:
            return {"error": "Session not found"}
        self.session_id = session_id
        self._cwd = result.get("cwd", "")

        # Accept agentId from client _meta (user may have switched agent)
        meta = params.get("_meta")
        client_agent_id = (meta or {}).get("agentId") if isinstance(meta, dict) else None
        if client_agent_id:
            new_agent = await self._find_agent_by_id(client_agent_id)
            if new_agent:
                self.agent_id = str(new_agent.id)
                self.agent_name = new_agent.name
                self.role_description = new_agent.role_description or ""
                await self.session_mgr.update_agent(session_id, self.agent_id)
                logger.info(f"[ACP] session/load: agent switched to {self.agent_id} ({self.agent_name})")
            else:
                self.agent_id = result["agent_id"]
        else:
            self.agent_id = result["agent_id"]

        logger.info(f"[ACP] session/load: id={session_id} agent={self.agent_id}")
        return {
            "sessionId": session_id,
            "cwd": result.get("cwd", ""),
            "history": result.get("history", []),
            "historyHasTools": result.get("historyHasTools", False),
            "tool_call_count": result.get("tool_call_count", 0),
        }

    async def _handle_prompt(self, params: dict, msg_id=None):
        """处理 prompt — LLM 调用 + 流式推送, 带 600s 超时。"""
        if not self.session_id:
            await self._send_error(msg_id, -32002, "No active session")
            return None

        messages = params.get("messages")
        if messages is None:
            prompt = params.get("prompt", [])
            messages = [{"role": "user", "content": prompt}] if prompt else []
        if not messages:
            await self._send_error(msg_id, -32602, "messages 不能为空")
            return None

        user_text_raw = _extract_user_text(messages)
        if not user_text_raw:
            user_text_raw = json.dumps(messages, ensure_ascii=False)

        if acp_feature_enabled("document") and self.session_id:
            meta = params.get("_meta") or {}
            if isinstance(meta, dict) and meta.get("documents"):
                docs = meta["documents"]
                open_n = len(docs.get("openUris") or []) if isinstance(docs, dict) else 0
                focused = docs.get("focusedUri") if isinstance(docs, dict) else None
                logger.info(
                    "[ACP-DOC] prompt meta snapshot session={} open={} focused={}",
                    self.session_id,
                    open_n,
                    focused,
                )
                document_store.apply_snapshot(self.session_id, meta["documents"])

        # 项目上下文只注入当前轮 LLM 输入，不写入 DB（避免历史膨胀）
        user_text_for_llm = user_text_raw
        project_ctx = self._build_project_context()
        if project_ctx:
            user_text_for_llm = project_ctx + "\n\n" + user_text_raw

        prompt_id = str(uuid.uuid4())
        self._cancel_event = asyncio.Event()

        # P1-11: 拷贝当前 contextvars 上下文，确保 create_task 创建的协程能继承
        # ContextVar（如 current_acp_handler），避免 asyncio.wait_for 超时取消路径破坏继承链
        ctx = contextvars.copy_context()
        task = asyncio.create_task(
            self._run_prompt_with_timeout(prompt_id, user_text_raw, user_text_for_llm),
            context=ctx,
        )
        self._active_tasks[prompt_id] = task

        try:
            await task
            return {"stopReason": "end_turn"}
        except asyncio.CancelledError:
            logger.info(f"[ACP] prompt 取消: {prompt_id}")
            return {"stopReason": "cancelled"}

    async def _run_prompt_with_timeout(
        self, prompt_id: str, user_text_raw: str, user_text_for_llm: str
    ):
        """LLM 调用 with 600s 超时 + cancel_event；加载/持久化多轮对话历史。"""
        # P1-12: 生成 trace_id 用于全链路日志追踪，便于跨模块定位单次 prompt 请求
        trace_id = str(uuid.uuid4())[:12]
        # 将模块级 logger 替换为绑定 trace_id 的本地副本，本作用域内所有日志自动带 trace_id
        _log = logger.bind(trace_id=trace_id)  # type: ignore[assignment]
        from app.services.llm.caller import call_llm_with_failover

        self._streamed_reply_parts: list[str] = []
        self._current_prompt_perf = {
            "start": time.perf_counter(),
            "pushed_chunks": 0,
            "first_chunk_ms": None,
            "reply_chars": 0,
        }
        self._turn_persisted_by_grace = False
        budget = TurnBudget.from_env()
        set_turn_budget(budget)
        _running_tool_call_ids: set[str] = set()
        # 每轮独立的 tool_call 计时: 并行执行时 call_id 隔离, 防跨 round 竞态
        _tool_call_starts: dict[str, float] = {}
        logger.info(
            f"[ACP-PERF] prompt_start conn={self.conn_id} session={self.session_id} "
            f"prompt_id={prompt_id} user_chars={len(user_text_raw)} llm_chars={len(user_text_for_llm)}"
        )

        async def push_chunk(chunk: str):
            # finish 正文常一次性较大：按行拆分推送，避免 IDE 长时间无更新后整段弹出
            if len(chunk) > 120 and "\n" in chunk:
                import re
                for part in _SPLIT_NL_RE.split(chunk):
                    if not part:
                        continue
                    await self._push_chunk(part)
            else:
                await self._push_chunk(chunk)

        async def on_tool_call(data: dict):
            """工具进度回调：写 agentbay 可检索日志并推送 ACP tool_call 通知。"""
            tool_name = data.get("name", "")
            call_id = data.get("call_id", "") or uuid.uuid4().hex[:12]
            status = data.get("status", "")
            if status == "running" and call_id:
                _running_tool_call_ids.add(call_id)
            elif status == "done" and call_id:
                _running_tool_call_ids.discard(call_id)
            args = data.get("args") or {}
            path_hint = args.get("path") or args.get("command") or ""
            if path_hint and self._cwd and isinstance(path_hint, str):
                cwd_prefix = f"cd {self._cwd} && "
                if path_hint.startswith(cwd_prefix):
                    path_hint = path_hint[len(cwd_prefix):]
            cn_name = _TOOL_CN_NAME.get(tool_name, tool_name)
            title = cn_name if not path_hint else f"{cn_name}({path_hint})"
            if status == "running":
                _tool_call_starts[call_id] = time.perf_counter()
                elapsed_ms = 0
            else:
                t0 = _tool_call_starts.pop(call_id, time.perf_counter())
                elapsed_ms = int((time.perf_counter() - t0) * 1000)
            _log.info(
                f"[ACP-PERF] tool_notify tool={tool_name} call_id={call_id} status={status} elapsed_ms={elapsed_ms}"
            )
            # 跨 prompt 记忆：与 WS/caller 默认路径一致，将 tool 结果写入 ChatMessage(tool_call)
            if status == "done":
                _b = get_turn_budget()
                if _b is not None:
                    _b.record_tool_completed()
            if status == "done" and self.session_id and self.agent_id and self.user_id:
                try:
                    from app.services.chat_session_service import save_tool_call_log

                    await save_tool_call_log(
                        agent_id=uuid.UUID(self.agent_id),
                        user_id=uuid.UUID(self.user_id),
                        conversation_id=self.session_id,
                        tool_name=tool_name,
                        arguments=args,
                        result=data.get("result"),
                        status="done",
                        tool_call_id=call_id,
                        reasoning_content=data.get("reasoning_content"),
                    )
                except Exception as persist_err:
                    _log.warning(f"[ACP] tool_call persist 失败 tool={tool_name}: {persist_err}")
            if tool_name == "send_message_to_agent":
                _acp_a2a_obs_log("H4", "acp_handler.py:on_tool_call", "a2a_tool_notify",
                                 status=status, elapsed_ms=elapsed_ms, call_id=call_id,
                                 target=args.get("agent_name", ""), msg_type=args.get("msg_type", ""),
                                 acp_session=self.session_id)
            acp_status = (
                "in_progress"
                if status == "running"
                else "completed"
                if status == "done"
                else status
            )
            kind = _kind_map.get(tool_name, "other")
            # P1-4: 标题映射验证日志
            _log.debug(
                f"[ACP] tool_notify title tool={tool_name} "
                f"kind={kind} cn_title={title}"
            )
            locations = []
            if path := args.get("path"):
                path_str = str(path)
                # 后端记忆/技能等内部文件不在 IDE 时间线展示（仍本地执行）
                if tool_name in ("read_file", "write_file", "edit_file", "delete_file") \
                        and os.path.isabs(path_str) \
                        and is_agent_internal_path(path_str):
                    _log.info(
                        f"[ACP-PERF] tool_notify skipped internal "
                        f"tool={tool_name} path={path_str}"
                    )
                    return
                locations.append({"path": path_str})
            elif cmd := args.get("command"):
                locations.append({"path": f"$ {cmd}"})
            try:
                await self._push_tool_call(call_id, title, acp_status, kind=kind, locations=locations)
                # P0-2: 通知正常推送确认
                _log.debug(
                    f"[ACP-PERF] tool_notify sent "
                    f"tool={tool_name} path={args.get('path', '') or args.get('command', '')} kind={kind}"
                )
            except Exception as push_err:
                _log.warning(f"[ACP-PERF] tool_notify push failed: {push_err}")

        self._thinking_coalescer.on_new_prompt()

        async def _do_llm():
            full_reply = ""
            try:
                # Load primary model from agent config
                primary_model = None
                fallback_model = None
                agent_row = None
                if self.agent_id:
                    from app.models.agent import Agent as _AgentModel
                    from app.models.llm import LLMModel as _LLMModel
                    from sqlalchemy.orm import joinedload
                    from app.database import async_session as _async_session
                    _log.info(f"[ACP-MODEL] 开始加载模型: agent_id={self.agent_id}")
                    async with _async_session() as db:
                        agent_result = await db.execute(
                            select(_AgentModel)
                            .options(
                                joinedload(_AgentModel.primary_model),
                                joinedload(_AgentModel.fallback_model),
                            )
                            .where(_AgentModel.id == self.agent_id)
                        )
                        agent_row = agent_result.unique().scalar_one_or_none()
                        if agent_row:
                            primary_model = agent_row.primary_model if agent_row.primary_model and agent_row.primary_model.enabled else None
                            fallback_model = agent_row.fallback_model if agent_row.fallback_model and agent_row.fallback_model.enabled else None
                            _log.info(
                                f"[ACP-MODEL] 模型加载完成: "
                                f"primary={getattr(primary_model, 'model', None)} "
                                f"fallback={getattr(fallback_model, 'model', None)} "
                                f"agent_row_found=True"
                            )
                        else:
                            _log.warning(f"[ACP-MODEL] agent_id={self.agent_id} DB 查询无结果!")

                from app.services.llm.context_compressor import _est_tokens_str, _get_ctx_guard_max_window

                _model_for_ctx = getattr(primary_model, "model", None) or getattr(fallback_model, "model", "") or ""
                _ctx_window = _get_ctx_guard_max_window(_model_for_ctx)

                history: list[dict] = []
                if self.session_id and self.user_id:
                    history = await self.session_mgr.load_history_for_llm(
                        self.session_id, self.user_id, ctx_window=_ctx_window,
                    )

                # hydrate（acp_session.load_history_for_llm）已完成 F2 token 预算 + Layer0 压缩；
                # token 级 in-loop 压缩由 caller.call_llm 内 CTX-GUARD 负责。

                # 历史消息直接传递给 call_llm_with_failover。
                # build_agent_context (soul.md + memory.md + skills + MCP 配置)
                # 由 caller.call_llm 统一注入到 system prompt, 避免 ACP 路径双重注入
                # (acp_handler 注入一次 + caller 再注入一次 → 浪费 ~2-4K tokens/次)。
                llm_messages = list(history) + [
                    {"role": "user", "content": user_text_for_llm}
                ]
                _tool_text = "".join(
                    m.get("content") or ""
                    for m in history
                    if m.get("role") == "tool" and isinstance(m.get("content"), str)
                )
                _log.info(
                    "[ACP-CTX] path=acp history={} total={} chars={} tool_tokens={} session={}",
                    len(history),
                    len(llm_messages),
                    sum(len(m.get("content") or "") for m in llm_messages if isinstance(m.get("content"), str)),
                    _est_tokens_str(_tool_text, _model_for_ctx),
                    self.session_id,
                )

                # 模型未配置时提前返回友好错误, 避免 call_llm_with_failover 瞬间返回
                # "⚠️ 未配置 LLM 模型" 被当作正常回复 → 客户端不理解 → 断开重连循环
                if primary_model is None and fallback_model is None:
                    err_msg = (
                        f"⚠️ Agent「{self.agent_name}」未配置 LLM 模型。"
                        "请在管理后台 agent 设置中指定 primary_model。"
                    )
                    await self._push_chunk(err_msg)
                    return err_msg

                await self._thinking_coalescer.handle("第 1 轮：规划中…")

                _llm_t0 = time.perf_counter()
                _log.info(
                    f"[ACP-LLM] call_llm_with_failover START session={self.session_id} "
                    f"primary={getattr(primary_model, 'model', 'None')} "
                    f"agent={self.agent_name}"
                )
                result = await call_llm_with_failover(
                    primary_model=primary_model,
                    fallback_model=fallback_model,
                    messages=llm_messages,
                    agent_name=self.agent_name,
                    role_description=self.role_description,
                    agent_id=self.agent_id,
                    user_id=self.user_id,
                    session_id=self.session_id or "",
                    on_chunk=push_chunk,
                    on_thinking=self._thinking_for_acp,
                    on_tool_call=on_tool_call,
                )
                full_reply = result or ""
                _log.info(
                    f"[ACP-LLM] call_llm_with_failover DONE session={self.session_id} "
                    f"elapsed={time.perf_counter() - _llm_t0:.3f}s "
                    f"reply_len={len(full_reply)}"
                )
                if perf := self._current_prompt_perf:
                    perf["reply_chars"] = len(full_reply)
                if full_reply and (self._current_prompt_perf or {}).get("pushed_chunks", 0) == 0:
                    _log.warning(f"[ACP] prompt 未收到流式 chunk，使用最终结果兜底推送: {len(full_reply)} chars")
                    await self._push_chunk(full_reply)
            except BudgetExceededError as budget_exc:
                _log.warning(
                    "[ACP-BUDGET] {} stats={}",
                    budget_exc.reason,
                    budget_exc.stats,
                )
                await self._terminate_turn_gracefully(
                    budget_exc.reason,
                    budget_exc.stats,
                    user_text_raw,
                    running_tool_ids=list(_running_tool_call_ids),
                )
                return full_reply
            except asyncio.CancelledError:
                _log.info(f"[ACP] LLM 调用取消: {prompt_id}")
                # 取消前清空缓冲区中的残余文本, 确保已流式输出的内容不丢失
                await self._flush_chunk_buffer()
                raise
            except Exception as e:
                _log.error(f"[ACP] LLM 调用失败: {e}")
                # 错误内容先发送缓冲区中已有文本, 再追加错误提示
                await self._flush_chunk_buffer()
                await self._push_chunk(f"\n\n*错误: {e}*")
            return full_reply

        result = ""
        already_persisted = False
        try:
            wf_remaining = budget.remaining_workflow()
            result = await asyncio.wait_for(_do_llm(), timeout=max(0.1, wf_remaining))
            _log.info(f"[ACP] prompt 完成: {len(result)} chars")
            if result:
                preview = result[:120].replace("\n", "\\n")
                _log.info(f"[ACP-ANSWER] session={self.session_id} len={len(result)} preview={preview!r}")
            if result and self.session_id and self.agent_id:
                await self.session_mgr.persist_turn(
                    session_id=self.session_id,
                    user_id=self.user_id,
                    agent_id=self.agent_id,
                    user_text=user_text_raw,
                    assistant_text=result,
                )
                already_persisted = True
        except BudgetExceededError as budget_exc:
            await self._terminate_turn_gracefully(
                budget_exc.reason,
                budget_exc.stats,
                user_text_raw,
                running_tool_ids=list(_running_tool_call_ids),
            )
        except asyncio.TimeoutError:
            stats = budget.to_audit_dict()
            _log.error(
                "[ACP-BUDGET] workflow_exceeded workflow_s={} stats={}",
                budget.workflow_seconds,
                stats,
            )
            await self._terminate_turn_gracefully(
                "workflow_exceeded",
                stats,
                user_text_raw,
                running_tool_ids=list(_running_tool_call_ids),
            )
        except asyncio.CancelledError:
            # 取消前清空缓冲区
            await self._flush_chunk_buffer()
            await self._push_chunk("\n\n*[已取消]*")
            raise
        finally:
            perf = self._current_prompt_perf or {}
            chunks = int(perf.get("pushed_chunks", 0))
            reply_len = len(result or "")
            streamed = "".join(getattr(self, "_streamed_reply_parts", None) or []).strip()
            # B3: 深 loop 多 chunk 但最终 reply 极短 → 用 streamed 兜底 UI/DB
            if chunks > 50 and reply_len < 50 and streamed:
                await self._flush_chunk_buffer()
                _log.warning(
                    "[ACP-UX] short_reply_fallback chunks={} reply={} streamed={}",
                    chunks,
                    reply_len,
                    len(streamed),
                )
                try:
                    await self._push_chunk(streamed)
                except Exception as push_exc:
                    _log.warning("[ACP-UX] short_reply_fallback push failed: {}", push_exc)
            if (
                not already_persisted
                and not getattr(self, "_turn_persisted_by_grace", False)
                and not (result or "").strip()
            ):
                await self._persist_streamed_reply_fallback(user_text_raw)
            elif not already_persisted and chunks > 50 and reply_len < 50 and streamed:
                await self._persist_streamed_reply_fallback(user_text_raw)
            self._streamed_reply_parts = []
            self._active_tasks.pop(prompt_id, None)
            self._cancel_event = None
            set_turn_budget(None)
            self._turn_persisted_by_grace = False

    async def _terminate_turn_gracefully(
        self,
        reason: str,
        stats: dict,
        user_text: str,
        *,
        running_tool_ids: list[str] | None = None,
    ) -> None:
        """超时优雅收尾：保留已流式内容 + 部分结果说明 + IDE 清扫通知。"""
        await self._flush_chunk_buffer()
        tools = int(stats.get("tools_completed", 0))
        streamed = "".join(getattr(self, "_streamed_reply_parts", None) or [])
        chars = len(streamed)
        if reason == "compute_exceeded":
            label = "计算预算超时"
        elif reason == "workflow_exceeded":
            label = "任务总时长超时"
        else:
            label = "任务超时"
        notice = (
            f"\n\n*{label}（已完成 {tools} 个工具调用，已输出 {chars} 字）。"
            f"可继续对话让智能体收尾。*"
        )
        try:
            await self._push_chunk(notice)
        except Exception as push_exc:
            logger.warning("[ACP-BUDGET] partial notice push failed: {}", push_exc)
        await self._persist_streamed_reply_fallback(user_text)
        self._turn_persisted_by_grace = True
        logger.warning(
            "[ACP-BUDGET] terminate reason={} stats={} running_tools={}",
            reason,
            stats,
            running_tool_ids or [],
        )
        if running_tool_ids:
            try:
                await self._send_notification("_clawith/turn_timeout", {
                    "sessionId": self.session_id,
                    "reason": reason,
                    "runningToolCallIds": running_tool_ids,
                    "stats": stats,
                })
            except Exception as notify_exc:
                logger.warning("[ACP-BUDGET] turn_timeout notify failed: {}", notify_exc)


    async def _push_chunk(self, text: str):
        """追加流式文本到缓冲区，按边界触发 flush。

        四级 flusher 规则（对标 OpenClaw）：
          1. 硬边界 ≥480 字 — 无论内容，立即发送
          2. 段落边界 \\n\\n — 自然段落，立即发送
          3. 句子边界 .!? 结尾 — 完整句子，立即发送
          4. 软边界 ≥220 字 + 空白结尾 — 词边界，发送
          5. 空闲 750ms + ≥80 字 — 最后一段不卡死
        """
        # P1: LLM 输出脱敏 — 推送前 sanitize 敏感信息
        
        perf = self._current_prompt_perf
        if perf is not None:
            perf["pushed_chunks"] = perf.get("pushed_chunks", 0) + 1
            if perf.get("first_chunk_ms") is None:
                elapsed_ms = (time.perf_counter() - perf["start"]) * 1000
                perf["first_chunk_ms"] = elapsed_ms
                logger.info(
                    f"[ACP-PERF] first_chunk conn={self.conn_id} session={self.session_id} "
                    f"ms_since_prompt={elapsed_ms:.0f} len={len(text)}"
                )
        self._chunk_buffer += text
        # 确保时间驱动 flush 在运行中 — 无论内容边界如何, 每 150ms 至少发送一次
        self._ensure_periodic_flush()
        if self._should_flush():
            await self._do_flush()
        else:
            self._schedule_idle_flush()

    def _should_flush(self) -> bool:
        buf = self._chunk_buffer
        if len(buf) >= _CHUNK_HARD_FLUSH:
            return True
        if buf.endswith("\n\n"):
            return True
        if _SENTENCE_END_RE.search(buf):
            return True
        if len(buf) >= _CHUNK_SOFT_FLUSH and buf[-1].isspace():
            return True
        return False

    def _schedule_idle_flush(self):
        if self._chunk_idle_task and not self._chunk_idle_task.done():
            return
        if len(self._chunk_buffer) < _CHUNK_IDLE_MIN:
            return
        # 渐进式延迟: 缓冲区越小, flush 越快 (短回复不卡)
        delay = _CHUNK_IDLE_SEC_SHORT if len(self._chunk_buffer) < 40 else _CHUNK_IDLE_SEC_LONG
        self._chunk_idle_task = asyncio.create_task(self._idle_flush(delay))

    async def _idle_flush(self, delay: float = 0.50):
        await asyncio.sleep(delay)
        if self._chunk_buffer:
            await self._do_flush()

    async def _do_flush(self):
        self._cancel_idle_task()
        self._cancel_periodic_task()
        text = self._chunk_buffer
        self._chunk_buffer = ""  # 必须在 await 前清空, 确保新 chunk 不被重复发送
        if not text:
            return
        parts = getattr(self, "_streamed_reply_parts", None)
        if isinstance(parts, list):
            parts.append(text)
        raw = self._chunk_template.replace("__SID__", self.session_id or "").replace(
            "__TEXT__", json.dumps(text, ensure_ascii=False)[1:-1]
        )
        try:
            # asyncio.shield 防止取消中断发送导致 chunk 永久丢失 (buffer 已清空)
            # WebSocket 断开时 send_text 会抛 ConnectionClosedError, 捕获防止重入竞态
            await asyncio.shield(self.ws.send_text(raw))
        except Exception as flush_exc:
            logger.warning(
                "[ACP-FLUSH] 发送失败 conn={} session={} len={} err={}",
                self.conn_id,
                self.session_id,
                len(text),
                flush_exc,
            )
            await self._persist_streamed_reply_fallback()
            try:
                await self._push_recovery_notice("连接中断，已将会话内容写入历史")
            except Exception:
                pass
        preview = text[:60].replace("\n", "\\n")
        logger.debug(
            "[ACP-FLUSH] conn={} session={} len={} preview={!r}",
            self.conn_id, self.session_id, len(text), preview,
        )

    def _cancel_idle_task(self):
        if self._chunk_idle_task and not self._chunk_idle_task.done():
            self._chunk_idle_task.cancel()
            self._chunk_idle_task = None

    # ── 时间驱动周期性 flush (参考 gptme PR #1586: 每 100ms 定时 flush) ──

    def _ensure_periodic_flush(self):
        """确保周期性 flush 在运行中 — 时间驱动保底, 不依赖内容边界。

        参考 gptme PR #1586: on_token 回调中每 100ms 定时 flush。
        本实现取 150ms, 平衡延迟与 WebSocket 消息频率。
        """
        if self._periodic_flush_task and not self._periodic_flush_task.done():
            return
        self._periodic_flush_task = asyncio.ensure_future(self._periodic_flush())

    async def _periodic_flush(self):
        """每隔 _PERIODIC_FLUSH_SEC 秒检查并发送缓冲区内容。"""
        await asyncio.sleep(_PERIODIC_FLUSH_SEC)
        if self._chunk_buffer:
            await self._do_flush()

    def _cancel_periodic_task(self):
        if self._periodic_flush_task and not self._periodic_flush_task.done():
            self._periodic_flush_task.cancel()
            self._periodic_flush_task = None

    async def _flush_chunk_buffer(self):
        self._cancel_idle_task()
        self._cancel_periodic_task()
        if self._chunk_buffer:
            text = self._chunk_buffer
            self._chunk_buffer = ""  # 必须在 await 前清空
            raw = self._chunk_template.replace("__SID__", self.session_id or "").replace(
                "__TEXT__", json.dumps(text, ensure_ascii=False)[1:-1]
            )
            try:
                # asyncio.shield: _flush_chunk_buffer 在取消/超时路径被调用 (CancelledError catch),
                # 此处无 shield 会导致 chunk 已清空但 send 被取消 → 永久丢失
                await asyncio.shield(self.ws.send_text(raw))
            except Exception:
                pass  # WS 已断开, 静默丢弃

    async def _push_recovery_notice(self, notice: str) -> None:
        """断连时推送可见摘要，避免 IDE 时间线完全空白。"""
        if not notice:
            return
        await self._send_notification("session/update", {
            "sessionId": self.session_id,
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": f"\n\n*{notice}*"},
            },
        })

    async def _persist_streamed_reply_fallback(self, user_text: str = "") -> None:
        parts = getattr(self, "_streamed_reply_parts", None) or []
        text = "".join(parts).strip()
        if not text or not self.session_id or not self.agent_id or not self.user_id:
            return
        try:
            await self.session_mgr.persist_turn(session_id=self.session_id, user_id=self.user_id, agent_id=self.agent_id, user_text=user_text, assistant_text=text)
            logger.info("[ACP] persist_streamed_fallback session={} chars={}", self.session_id, len(text))
        except Exception as exc:
            logger.warning("[ACP] persist_streamed_fallback failed session={} err={}", self.session_id, exc)

    async def _thinking_for_acp(self, text: str) -> None:
        await self._thinking_coalescer.handle(text)

    async def _push_thinking(self, text: str):
        # 轮次状态（A2）推送时打一条 hint，便于与 [ACP-PERF] Round 交叉验证
        if text and ("轮" in text[:40] or "round" in text[:40].lower()):
            logger.info(
                f"[ACP-PERF] round_hint conn={self.conn_id} session={self.session_id} "
                f"text={text[:120]!r}"
            )
        await self._send_notification("session/update", {
            "sessionId": self.session_id,
            "update": {
                "sessionUpdate": "agent_thought_chunk",
                "content": {"type": "text", "text": text},
            },
        })

    async def _push_tool_call(self, tool_call_id: str, title: str,
                              status: str = "in_progress", kind: str | None = None,
                              locations: list | None = None):
        """推送工具调用状态更新 (ACP session/update notification)。
        
        使用 ACP SDK 原生字段: kind (read/edit/execute/...), locations (文件路径链接)。
        status 对齐 ToolCallStatus 枚举: in_progress / completed / failed。
        """
        await self._send_notification("session/update", {
            "sessionId": self.session_id,
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": tool_call_id,
                "title": title,
                "status": status,
                "kind": kind,
                "locations": locations or [],
            },
        })
        logger.info(
            f"[ACP-NOTIFY] tool_call_update conn={self.conn_id} session={self.session_id} "
            f"tool={tool_call_id} kind={kind} status={status} locations={len(locations or [])}"
        )
    async def _handle_cancel(self, params: dict) -> dict:
        if self._cancel_event:
            self._cancel_event.set()
        # 联动 terminal 流式 poll_loop，使其优雅 kill 而非仅靠 task.cancel()
        for ev in getattr(self, "_terminal_events", {}).values():
            ev.set()
        tasks = [t for t in self._active_tasks.values() if not t.done()]
        for task in tasks:
            task.cancel()
        logger.info(
            "[ACP] cancel session={} tasks={} terminal_events={}",
            self.session_id,
            len(tasks),
            len(getattr(self, "_terminal_events", {})),
        )
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=30.0)
            except TimeoutError:
                logger.warning("[ACP] cancel 等待 task 结束超时 session={}", self.session_id)
        # 清除已结束 task，避免 _has_in_flight_prompt 误判阻塞下一 prompt
        for key in [k for k, t in list(self._active_tasks.items()) if t.done()]:
            self._active_tasks.pop(key, None)
        logger.info(
            "[ACP] cancel 完成 session={} remaining_tasks={}",
            self.session_id,
            sum(1 for t in self._active_tasks.values() if not t.done()),
        )
        return {"cancelled": True}
    async def _handle_session_close(self, params: dict) -> dict:
        if self.session_id and acp_feature_enabled("document"):
            document_store.clear_session(self.session_id)
        logger.info(f"[ACP] session/close: {self.session_id}")
        return {"closed": True}

    async def _handle_set_agent(self, params: dict) -> dict:
        """Handle client agent selection — must return JSON-RPC response."""
        agent_id = params.get("agentId", "")
        if not agent_id:
            return {"ok": False, "error": "agentId is required"}
        agent = await self._find_agent_by_id(agent_id)
        if agent:
            self.agent_id = str(agent.id)
            self.agent_name = agent.name
            self.role_description = agent.role_description or ""
            if self.session_id:
                await self.session_mgr.update_agent(self.session_id, self.agent_id)
            logger.info(f"[ACP] set_agent: session={self.session_id} agent={self.agent_id} ({self.agent_name})")
            return {"ok": True}
        logger.warning(f"[ACP] set_agent: agent not found: {agent_id}")
        return {"ok": False, "error": f"Agent not found: {agent_id}"}

    async def _handle_last_assistant(self, params: dict) -> dict:
        """断连恢复：返回会话最后一条 assistant 正文，供 IDE 补渲染。"""
        session_id = str(params.get("sessionId") or self.session_id or "")
        if not session_id:
            return {"text": ""}
        from app.models.audit import ChatMessage

        try:
            async with async_session() as db:
                hr = await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.conversation_id == session_id)
                    .where(ChatMessage.role == "assistant")
                    .order_by(ChatMessage.created_at.desc())
                    .limit(1)
                )
                msg = hr.scalar_one_or_none()
            text = (msg.content if msg else "") or ""
            logger.info(
                "[ACP-UX] last_assistant session={} chars={}",
                session_id[:8],
                len(text),
            )
            return {"text": text}
        except Exception as exc:
            logger.warning("[ACP-UX] last_assistant failed session={} err={}", session_id[:8], exc)
            return {"text": ""}

    async def _handle_session_meta(self, params: dict) -> dict:
        """E0：返回会话是否含历史 tool 行（旧 session 无 tool_call 需提示新开）。"""
        session_id = str(params.get("sessionId") or self.session_id or "")
        if not session_id or not self.user_id:
            return {"historyHasTools": False, "tool_call_count": 0}
        loaded = await self.session_mgr.load(session_id, self.user_id)
        if not loaded:
            return {"historyHasTools": False, "tool_call_count": 0}
        return {
            "historyHasTools": bool(loaded.get("historyHasTools")),
            "tool_call_count": int(loaded.get("tool_call_count") or 0),
        }

    async def _handle_tool_result(self, params: dict) -> dict:
        """Handle client tool result notification — must return JSON-RPC response."""
        tool_id = params.get("toolId", "")
        future = self._pending_tools.pop(tool_id, None)
        if future and not future.done():
            if params.get("success"):
                future.set_result(params)
            else:
                future.set_exception(Exception(params.get("error", "工具执行失败")))
        logger.info(f"[ACP] tool_result: id={tool_id}")
        return {"received": True}

    async def _handle_set_mode(self, params: dict) -> dict:
        return {"mode": params.get("mode", "auto")}

    async def _handle_set_model(self, params: dict) -> dict:
        return {"model": params.get("model", "")}

    async def _handle_list_agents(self, params: dict) -> dict:
        """ACP 扩展: 列出可用 Agent 列表 (智能体选择)。

        与 REST API ide_plugin.py 的 list_agents_for_ide 对齐权限模型:
        通过 build_visible_agents_query 按 tenant_id + user 权限过滤,
        确保 ACP 通道不会跨租户泄露 agent 列表。
        """
        async with async_session() as db:
            # 加载 User 对象以进行权限过滤 (build_visible_agents_query 需要 User 实例)
            user_r = await db.execute(select(UserModel).where(UserModel.id == self.user_id))
            user = user_r.scalar_one_or_none()
            if not user:
                logger.warning("[ACP] list_agents: user not found for user_id=%s", self.user_id)
                return {"agents": []}
            # 复用 REST API 的可见性过滤规则,
            # 仅追加 status 过滤(排除已删除/禁用的 agent)
            result = await db.execute(
                build_visible_agents_query(user)
                .where(AgentModel.status.in_(["idle", "running"]))
                .order_by(AgentModel.updated_at.desc())
                .limit(20)
            )
            agents = result.scalars().all()
            return {"agents": [
                {"id": str(a.id), "name": a.name,
                 "description": a.role_description or "",
                 "is_default": i == 0}
                for i, a in enumerate(agents)
            ]}

    async def _handle_providers_list(self, params: dict) -> dict:
        if not acp_feature_enabled("providers"):
            return {"providers": []}
        data = await self._handle_list_agents(params)
        providers = [
            {"id": a.get("id", ""), "name": a.get("name", ""), "description": a.get("description", "")}
            for a in data.get("agents", [])
        ]
        logger.info("[ACP] providers/list count={}", len(providers))
        return {"providers": providers}

    async def _handle_providers_set(self, params: dict) -> dict:
        if not acp_feature_enabled("providers"):
            return {"ok": False, "error": "providers feature disabled"}
        agent_id = params.get("providerId") or params.get("agentId") or params.get("id") or ""
        return await self._handle_set_agent({"agentId": agent_id})

    async def _handle_logout(self, params: dict) -> dict:
        self.agent_id = None
        self.agent_name = "ACP Agent"
        logger.info("[ACP] logout session={}", self.session_id)
        return {"ok": True}

    # ── 辅助方法 ──────────────────────────────────────────────

    async def _resolve_tenant_id(self) -> uuid.UUID | None:
        """通过 self.user_id 查询 OrgMember 获取 tenant_id (惰性加载)。"""
        if self._tenant_id is not None:
            return self._tenant_id
        async with async_session() as db:
            try:
                uid = uuid.UUID(self.user_id)
                result = await db.execute(
                    select(OrgMember.tenant_id)
                    .where(OrgMember.user_id == uid, OrgMember.status == "active")
                    .limit(1)
                )
                self._tenant_id = result.scalar_one_or_none()
            except ValueError:
                logger.warning(f"[ACP] 无效的 user_id, 无法解析 tenant_id: {self.user_id}")
                self._tenant_id = None
        return self._tenant_id

    async def _find_agent(self):
        """查找当前租户内最近更新的 Agent。"""
        tenant_id = await self._resolve_tenant_id()
        async with async_session() as db:
            # 优先找当前租户下 idle 状态的 Agent
            base = select(AgentModel).order_by(AgentModel.updated_at.desc()).limit(1)
            if tenant_id:
                base = base.where(AgentModel.tenant_id == tenant_id)
            result = await db.execute(base.where(AgentModel.status == "idle"))
            agent = result.scalar_one_or_none()
            if not agent:
                # 备选: 当前租户下任意状态的 Agent
                result = await db.execute(base)
                agent = result.scalar_one_or_none()
            return agent

    async def _find_agent_by_id(self, agent_id: str):
        """Find agent by UUID (带 tenant_id 过滤)。"""
        tenant_id = await self._resolve_tenant_id()
        async with async_session() as db:
            try:
                aid = uuid.UUID(agent_id)
                query = select(AgentModel).where(AgentModel.id == aid)
                if tenant_id:
                    query = query.where(AgentModel.tenant_id == tenant_id)
                result = await db.execute(query)
                return result.scalar_one_or_none()
            except ValueError:
                logger.warning(f"[ACP] invalid agentId: {agent_id}")
                return None

    async def cleanup_stale_requests(self):
        now = time.monotonic()
        stale = [rid for rid, (_, ts) in self._pending_requests.items() if now - ts > 300]
        for rid in stale:
            entry = self._pending_requests.pop(rid, None)
            if entry and not entry[0].done():
                entry[0].cancel()
        if stale:
            logger.warning(f"[ACP] stale requests cleaned: {len(stale)}")

    async def cleanup(self):
        """安全清理 — 标记关闭状态, 取消并等待活跃任务完成。"""
        async with self._close_lock:
            if self._closing:
                return
            self._closing = True
        # 清理前记录活跃资源数量，便于排查残留泄漏
        logger.info("[ACP] cleanup: active_tasks={} pending_tools={} pending_requests={}",
            len(self._active_tasks), len(self._pending_tools), len(self._pending_requests))
        self._cancel_idle_task()
        for ev in getattr(self, "_terminal_events", {}).values():
            ev.set()
        # 取消所有活跃任务 (dispatch + terminal streaming)
        for task in self._active_tasks.values():
            if not task.done():
                task.cancel()
        # 等待任务完成 (最多 5s), 避免 WebSocket 关闭时仍有进行中的 dispatch
        if self._active_tasks:
            await asyncio.gather(
                *self._active_tasks.values(), return_exceptions=True
            )
        for future in self._pending_tools.values():
            if not future.done():
                future.cancel()
        for entry in self._pending_requests.values():
            if not entry[0].done():
                entry[0].cancel()
        self._active_tasks.clear()
        getattr(self, "_terminal_events", {}).clear()
        self._pending_tools.clear()
        self._pending_requests.clear()
        duration = time.monotonic() - self._start_time
        logger.info(
            "[ACP-CONN] disconnect conn_id={} session={} user={} duration={:.0f}s",
            self.conn_id, self.session_id or "?", self.user_id[:8] if self.user_id else "?", duration,
        )

    # ── JSON-RPC 序列化 ───────────────────────────────────────

    async def _send_result(self, msg_id, result):
        raw = json.dumps({
            "jsonrpc": "2.0", "id": msg_id, "result": result
        }, ensure_ascii=False, default=str)
        logger.debug(f"[ACP-RAW-OUT] {raw}")
        try:
            await asyncio.shield(self.ws.send_text(raw))
        except Exception as exc:
            # cancel 后客户端可能仍连着也可能已断开，不向 ERROR 升级
            logger.warning(
                "[ACP] _send_result 丢弃 conn={} session={} err={}",
                self.conn_id,
                self.session_id,
                exc,
            )
            await self._persist_streamed_reply_fallback()

    async def _send_error(self, msg_id, code: int, message: str):
        # P1-2: error_ref 随机码，用户报 ref=abc12345 → grep 秒级定位
        error_ref = str(uuid.uuid4())[:8]
        raw = json.dumps({
            "jsonrpc": "2.0", "id": msg_id,
            "error": {"code": code, "message": f"{message} (ref: {error_ref})"}
        }, ensure_ascii=False)
        # 关闭中不报 ERROR — "after websocket.close" 是预期的清理行为
        if self._closing:
            logger.warning("[ACP] _send_error (closing) conn={} session={} code={} ref={} msg={}",
                self.conn_id, self.session_id, code, error_ref, message)
        else:
            logger.error("[ACP] _send_error conn={} session={} code={} ref={} msg={}",
                self.conn_id, self.session_id, code, error_ref, message)
        try:
            await asyncio.shield(self.ws.send_text(raw))
        except Exception:
            pass  # WS 已关闭 (cleanup() → ws.close()), 静默丢弃

    async def _send_notification(self, method: str, params: dict):
        raw = json.dumps({
            "jsonrpc": "2.0", "method": method, "params": params
        }, ensure_ascii=False, default=str)
        logger.debug(f"[ACP-RAW-OUT] {raw}")
        await asyncio.shield(self.ws.send_text(raw))

    async def _send_json(self, data: dict):
        raw = json.dumps(data, ensure_ascii=False, default=str)
        logger.debug(f"[ACP-RAW-OUT] {raw}")
        await asyncio.shield(self.ws.send_text(raw))


    async def _notify_abort_permissions(self, reason: str) -> None:
        """通知 IDE 中止挂起的权限弹窗，防止后端已超时后用户点击仍执行删除。"""
        if not self.session_id:
            return
        payload = {
            "jsonrpc": "2.0",
            "method": "_clawith/abort_permissions",
            "params": {"sessionId": self.session_id, "reason": reason},
        }
        try:
            await asyncio.shield(self.ws.send_text(json.dumps(payload, ensure_ascii=False)))
            logger.info(
                "[ACP-PERF] abort_permissions notified session={} reason={}",
                self.session_id,
                reason,
            )
        except Exception as exc:
            logger.warning(
                "[ACP-PERF] abort_permissions notify failed session={} reason={} err={}",
                self.session_id,
                reason,
                exc,
            )

    async def _wait_pending_future(self, future: asyncio.Future, *, timeout: float) -> Any:
        """等待 IDE RPC 响应；每秒检查 cancel_event，避免权限等待阻塞取消。"""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            if self._cancel_event and self._cancel_event.is_set():
                raise asyncio.CancelledError("ACP send_request cancelled")
            try:
                return await asyncio.wait_for(asyncio.shield(future), timeout=min(1.0, remaining))
            except asyncio.TimeoutError:
                continue


    async def send_request(self, method: str, params: dict, timeout: float = 30.0):
        """Send ACP JSON-RPC request and wait for response.

        Used for agent->client tool call proxy (e.g. fs/read_text_file).

        Note: the future is NOT removed from _pending_requests on timeout,
        so a late-arriving response can still be consumed by a subsequent
        retry or cleanup. asyncio.wait_for does NOT cancel a Future, so
        the future stays valid for later resolution."""
        # 熔断器门控: IDE 端持续异常时拒绝发送, 防止错误扩散
        if os.getenv("ACP_CIRCUIT_ENABLED", "true").lower() != "false":
            if not self._circuit.allow():
                raise RuntimeError(
                    "ACP circuit breaker OPEN — IDE 端持续异常, 请求已被熔断。"
                    "请等待恢复或检查 IDE 状态。"
                )
        # 惰性清理: 条目数 > 50 或最早条目 > 120s 时触发清理
        if len(self._pending_requests) > 50:
            await self.cleanup_stale_requests()
        elif self._pending_requests:
            oldest = min(ts for _, (_, ts) in self._pending_requests.items())
            if time.monotonic() - oldest > 120:
                await self.cleanup_stale_requests()
        req_id = str(uuid.uuid4())
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        async with self._pending_lock:
            # 背压控制: 原子 check-then-act，避免 gather 并发 TOCTOU
            pending_count = len(self._pending_requests)
            is_read = method in _READ_METHODS
            threshold = _READ_BACKPRESSURE_THRESHOLD if is_read else _WRITE_BACKPRESSURE_THRESHOLD
            if pending_count >= threshold:
                logger.error(
                    f"[ACP-BACKPRESSURE] 拒绝 {method}: pending={pending_count} >= {threshold} "
                    f"(type={'read' if is_read else 'write'}). "
                    f"IDE 端可能 EDT 阻塞或无响应, 请检查 IDE 状态。"
                )
                raise RuntimeError(
                    f"ACP backpressure: {pending_count} pending requests. "
                    f"IDE 端可能忙或无响应。请稍后重试。"
                )
            elif pending_count >= 5:
                oldest_age = min(time.monotonic() - ts for _, (_, ts) in self._pending_requests.items())
                logger.warning(
                    f"[ACP-QUEUE] pending 积压: count={pending_count} "
                    f"method={method} oldest_age={oldest_age:.1f}s "
                    f"total_pending={len(self._pending_requests)}"
                )
            self._pending_requests[req_id] = (future, time.monotonic())
        # 端到端追踪: 将 trace_id 和 req_id 注入 ACP params, IDE 插件侧可解析关联
        # trace_id 由 _do_llm 中 get_trace_id() 生成, 用于跨请求聚合
        _trace_id = get_trace_id()
        _params = dict(params)
        _params["_trace_id"] = _trace_id
        _params["_req_id"] = req_id
        raw = json.dumps({
            "type": "com.agentclientprotocol.rpc.JsonRpcRequest",
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": _params,
        }, ensure_ascii=False, default=str)
        logger.debug(f"[ACP-RAW-OUT] {raw}")
        t0 = time.perf_counter()
        logger.debug(
            "[ACP-PERF] send_request START session={} method={} timeout={}s req_id={}",
            self.session_id, method, timeout, req_id,
        )
        try:
            await asyncio.shield(self.ws.send_text(raw))
            result = await self._wait_pending_future(future, timeout=timeout)
            elapsed = time.perf_counter() - t0
            logger.debug(
                "[ACP-PERF] send_request DONE session={} method={} elapsed={:.3f}s req_id={}",
                self.session_id, method, elapsed, req_id,
            )
            # 熔断器: 记录成功
            if os.getenv("ACP_CIRCUIT_ENABLED", "true").lower() != "false":
                self._circuit.success()
            return result
        except asyncio.TimeoutError:
            elapsed = time.perf_counter() - t0
            # 超时后丢弃 pending future，避免 late response 在用户已点「允许」后误执行
            self._pending_requests.pop(req_id, None)
            logger.warning(
                f"[ACP-PERF] send_request TIMEOUT session={self.session_id} method={method} "
                f"elapsed={elapsed:.3f}s timeout={timeout}s req_id={req_id} "
                f"pending={len(self._pending_requests)}"
            )
            if method in _PERMISSION_GATED_METHODS:
                asyncio.create_task(self._notify_abort_permissions("permission_timeout"))
            # 熔断器: 记录超时失败
            if os.getenv("ACP_CIRCUIT_ENABLED", "true").lower() != "false":
                self._circuit.failure()
            raise TimeoutError(f"ACP request {method} timed out after {timeout}s")

    def _build_project_context(self) -> str:
        """Build IDE project context string for prompt injection."""
        cwd = getattr(self, "_cwd", "")
        parts: list[str] = []
        if cwd:
            parts.append(
                f"## IDE Project Environment\n"
                f"Working directory: {cwd}\n"
                f"\n"
                f"## Rules\n"
                f"- 回答简洁直接。如果用户的问题不需要读取项目文件(如问候/身份确认), 只回复不扫描项目。\n"
                f"- 需要读文件时用 read_file, 不要用 cat/head/tail。\n"
                f"- 所有项目文件路径必须相对 Working directory; 不要把包名、类名猜成目录。\n"
                f"- 删除、移动、重命名前, 必须先用 find_file/list_files 确认真实路径。\n"
                f"- build/test/git 用 execute_command; 禁止在命令中 cd 到绝对路径, 直接在项目根执行相对命令。\n"
                f"🔍 **代码搜索强制规则**: 搜索代码时必须用 IDE 索引工具 (find_symbol/search_text/find_class/"
                f"find_references/find_definition/find_file/find_implementations/list_files)，"
                f"比 grep -r 快 100-1000 倍且支持语义匹配。**严禁用 execute_command + grep 替代 IDE 搜索**。\n"
                f"📍 **查找引用/实现优先**: 找到符号后，必须用 find_references（查所有引用）或 find_implementations（查所有实现），"
                f"一次性获取所有相关位置，不要逐文件搜索。\n"
                f"⚠️ 注意: index_status 返回错误不代表其他 IDE 工具不可用，各工具独立运作。\n"
                f"- Agent 内部文件(memory/, skills/ 前缀)不在 IDE, 由后端本地读取。\n"
                f"- 🚀 并行执行: 多个独立的 read_file/search/list/find 操作必须在一次函数调用批次中并行调用, "
                f"不要逐个串行。例如: 需要读 3 个文件时, 一次 tool_calls 中同时调用 3 个 read_file。\n"
            )
        if acp_feature_enabled("document") and self.session_id:
            doc_ctx = document_store.format_for_prompt(self.session_id)
            if doc_ctx:
                parts.append(doc_ctx)
        return "\n\n".join(parts) if parts else ""


def _extract_user_text(messages: list) -> str:
    """从 ACP prompt messages 提取用户文本。"""
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
                return "".join(parts) or None
            elif isinstance(content, str):
                return content
    return ""


# ── 滑动窗口错误率熔断器 ──────────────────────────────────────
# 参考 Hystrix CircuitBreaker 模式 + aioresilience。
# per-AcpHandler 实例级（非模块级单例），保证多智能体独立熔断。


# 熔断器可调参数（环境变量注入，支持热调整无需重新部署）
_CIRCUIT_WINDOW_S: float = float(os.getenv("ACP_CIRCUIT_WINDOW_S", "60"))
_CIRCUIT_ERROR_RATE: float = float(os.getenv("ACP_CIRCUIT_ERROR_RATE", "0.5"))
_CIRCUIT_RECOVERY_S: float = float(os.getenv("ACP_CIRCUIT_RECOVERY_S", "30"))
# 最小样本数门槛: 窗口内样本不足时不计算错误率, 防止单次超时就熔断
_CIRCUIT_MIN_SAMPLES: int = int(os.getenv("ACP_CIRCUIT_MIN_SAMPLES", "3"))


class CircuitBreaker:
    """per-AcpHandler 滑动窗口错误率熔断器。

    状态机:
      CLOSED → (error_rate > 50%) → OPEN
      OPEN   → (recovery_time 后)  → HALF_OPEN (放行 1 个探测请求)
      HALF_OPEN → 探测成功         → CLOSED
      HALF_OPEN → 探测失败         → OPEN

    关键设计:
      - 单探针 (half_open 仅放行 1 个请求), 避免多探针预算耗尽死锁
      - _prune() 自动清理滑动窗口外条目, 防止内存泄漏
      - per-instance (非模块级), 每个 AcpHandler 独立熔断
      - 状态变化 WARNING 级别日志, 携带前后状态和错误率
      - 所有参数通过环境变量可配, ACP_CIRCUIT_ENABLED=false 关闭
    """

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"

    def __init__(
        self,
        window_s: float = _CIRCUIT_WINDOW_S,
        error_rate_threshold: float = _CIRCUIT_ERROR_RATE,
        recovery_s: float = _CIRCUIT_RECOVERY_S,
    ) -> None:
        self.state: str = self.CLOSED
        self._window_s = window_s
        self._error_rate_threshold = error_rate_threshold
        self._recovery = recovery_s
        self._half_open_probe_sent = False  # 单探针: 仅放行 1 个探测请求
        self._results: list[tuple[float, bool]] = []  # [(timestamp, success)]
        self.last_fail = 0.0

    def _prune(self) -> None:
        """清理滑动窗口外的旧条目, 防止内存泄漏。每次 success/failure 自动调用。"""
        now = time.monotonic()
        cutoff = now - self._window_s
        self._results = [r for r in self._results if r[0] > cutoff]

    def _error_rate(self) -> float:
        """计算滑动窗口内的错误率 (0.0 ~ 1.0)。

        最小样本数门槛: 窗口内样本不足 _CIRCUIT_MIN_SAMPLES 时不计算错误率,
        防止单次超时就触发熔断 (单次超时 = 1/1 = 100% > 50% 阈值 → 误熔断)。
        """
        now = time.monotonic()
        cutoff = now - self._window_s
        recent = [ok for ts, ok in self._results if ts > cutoff]
        if len(recent) < _CIRCUIT_MIN_SAMPLES:
            return 0.0
        return 1.0 - sum(recent) / len(recent)

    def allow(self) -> bool:
        """判断是否允许发送请求。

        CLOSED → 直接放行
        OPEN   → 检查是否到恢复时间, 到了则转换到 HALF_OPEN 并放行 1 个探测
        HALF_OPEN → 仅放行 1 个探测请求
        """
        if self.state == self.CLOSED:
            return True

        if self.state == self.OPEN:
            age = time.monotonic() - self.last_fail
            if age > self._recovery:
                err_rate = self._error_rate()
                old_state = self.state
                self.state = self.HALF_OPEN
                self._half_open_probe_sent = True
                logger.warning(
                    "[ACP-CIRCUIT] state={}→{} error_rate={:.0%} "
                    "last_fail={:.0f}s_ago — 发送探测请求",
                    old_state, self.state, err_rate, age,
                )
                return True
            return False

        if self.state == self.HALF_OPEN:
            # 单探针: 仅放行 1 个探测请求
            if self._half_open_probe_sent:
                return False
            self._half_open_probe_sent = True
            return True

        return False

    def success(self) -> None:
        """记录一次成功请求。HALF_OPEN 状态下成功则恢复 CLOSED。"""
        self._results.append((time.monotonic(), True))
        self._prune()

        if self.state == self.HALF_OPEN:
            old_state = self.state
            self.state = self.CLOSED
            self._half_open_probe_sent = False
            logger.warning(
                "[ACP-CIRCUIT] state={}→{} — 探测成功, 熔断器关闭",
                old_state, self.state,
            )

    def failure(self) -> None:
        """记录一次失败请求。CLOSED 下超阈值 → OPEN; HALF_OPEN 下失败 → 重新 OPEN。"""
        self._results.append((time.monotonic(), False))
        self._prune()

        self.last_fail = time.monotonic()

        if self.state == self.HALF_OPEN:
            old_state = self.state
            self.state = self.OPEN
            self._half_open_probe_sent = False
            logger.warning(
                "[ACP-CIRCUIT] state={}→{} — 探测失败, 熔断器重新打开",
                old_state, self.state,
            )
            return

        if self.state == self.CLOSED:
            err_rate = self._error_rate()
            if err_rate >= self._error_rate_threshold:
                old_state = self.state
                self.state = self.OPEN
                logger.warning(
                    "[ACP-CIRCUIT] state={}→{} error_rate={:.0%} "
                    "last_fail=now — 错误率超阈值, 触发熔断",
                    old_state, self.state, err_rate,
                )
