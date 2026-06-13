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

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.core.logging_config import get_trace_id
from app.models.agent import Agent as AgentModel
from app.models.org import OrgMember
from app.plugins.clawith_acp.acp_session import AcpSessionManager
from app.plugins.clawith_acp.tool_bridge import current_acp_handler, is_agent_internal_path
from app.plugins.clawith_acp.tool_hooks import install_acp_tool_hooks
from app.services.agent_context import build_agent_context

ACP_PROTOCOL_VERSION = 1
LLM_TIMEOUT_SECONDS = int(os.getenv("ACP_LLM_TIMEOUT_SECONDS", "600"))

# ── 背压控制：读写分离阈值 ──────────────────────────────────────
# 读操作(VFS/索引/搜索)响应快(&lt;100ms), 允许更高并发
# 写操作(write/build/reformat)需 EDT+WriteAction, 保持保守阈值
_READ_BACKPRESSURE_THRESHOLD = 15
_WRITE_BACKPRESSURE_THRESHOLD = 5
_READ_METHODS = frozenset({
    "fs/read_text_file", "fs/list_directory", "fs/find_file",
    "fs/search_text", "fs/find_class", "fs/find_symbol",
    "fs/find_references", "fs/find_definition", "fs/find_implementations",
    "fs/find_super_methods", "fs/file_structure", "fs/get_documentation",
    "fs/call_hierarchy", "fs/type_hierarchy", "fs/diagnostics",
    "ide/active_file", "ide/index_status",
})

_CHUNK_HARD_FLUSH = 240   # 降低硬上限, 配合时间驱动 flush 避免长时间无输出
_CHUNK_SOFT_FLUSH = 120   # 降低软上限, 短文本也能在合理边界发送
_CHUNK_IDLE_MIN = 10      # 降低 idle 最小阈值, 覆盖"完成。"等极短中文回复
# 渐进式 idle flush: 短回复 (&lt;40字) 用 150ms 避免卡顿, 长回复用 500ms 等待更多内容
_CHUNK_IDLE_SEC_SHORT = 0.15
_CHUNK_IDLE_SEC_LONG = 0.50
# 参考 gptme PR #1586: 100ms 时间驱动 flush 确保流式输出及时到达客户端,
# 不依赖内容边界(句号/换段)即可触发。150ms 平衡延迟与网络开销。
_PERIODIC_FLUSH_SEC = 0.15
# 中英文句末标点均触发句子边界 flush
_SENTENCE_END_RE = re.compile(r'[.!?。！？…~]["\'」』）\)]*\s*$')
_SPLIT_NL_RE = re.compile(r"(?<=\n)")

# ── LLM 输出脱敏 ──────────────────────────────────────────────

# Install ACP tool hooks (idempotent)
install_acp_tool_hooks()
# 工具名 → ACP ToolKind 映射，供 _push_tool_call 填充 kind 字段
_kind_map = {
    "read_file": "read", "write_file": "edit", "edit_file": "edit",
    "delete_file": "delete", "execute_command": "execute", "bash": "execute",
    "find_class": "search", "find_symbol": "search", "index_status": "read",
    "find_references": "search", "find_definition": "search",
    "find_implementations": "search", "find_super_methods": "search",
    "call_hierarchy": "search", "type_hierarchy": "search",
    "diagnostics": "read",
    "refactor_rename": "edit", "move_file": "edit",
    "reformat_code": "edit", "optimize_imports": "edit",
    "safe_delete": "delete", "convert_java_to_kotlin": "edit",
    "sync_files": "edit",
    "active_file": "read", "open_file": "edit",
    "file_structure": "read",
    "find_file": "search",
    "search_text": "search",
    "list_directory": "read",
    "list_files": "read",
    "build_project": "edit",
    "get_documentation": "read", "apply_quickfix": "edit",
    "git_status": "read", "git_diff": "read", "git_stage": "edit", "git_commit": "edit",
}

# 工具名 → 中文显示名，供 on_tool_call 填充 title 字段
_TOOL_CN_NAME = {
    "read_file": "读取文件", "write_file": "写入文件", "edit_file": "编辑文件",
    "delete_file": "删除文件", "execute_command": "执行命令", "bash": "终端",
    "find_class": "搜索类", "find_symbol": "搜索符号", "index_status": "索引进度",
    "find_references": "查找引用", "find_definition": "查找定义",
    "find_implementations": "查找实现", "find_super_methods": "查找父方法",
    "call_hierarchy": "调用层次", "type_hierarchy": "类型层次",
    "diagnostics": "诊断", "refactor_rename": "重命名", "move_file": "移动文件",
    "reformat_code": "格式化", "optimize_imports": "优化导入",
    "safe_delete": "安全删除", "convert_java_to_kotlin": "Java→Kotlin",
    "sync_files": "同步文件", "active_file": "活动文件", "open_file": "打开文件",
    "file_structure": "文件结构", "find_file": "查找文件", "search_text": "文本搜索",
    "list_directory": "列出目录", "list_files": "列出文件",
    "build_project": "构建项目", "get_documentation": "查看文档",
    "apply_quickfix": "应用修复",
    "git_status": "Git状态", "git_diff": "Git差异", "git_stage": "Git暂存", "git_commit": "Git提交",
}
class AcpHandler:
    """ACP JSON-RPC 2.0 路由 + Agent 管理。"""

    def __init__(self, websocket, user_id: str):
        self.ws = websocket
        self.user_id = user_id
        # 短连接 ID，便于 docker logs 对齐 IDE gen 与后端 handler
        self.conn_id = uuid.uuid4().hex[:8]
        self._tenant_id: uuid.UUID | None = None  # 惰性加载, 通过 _resolve_tenant_id() 获取
        self.session_id: str | None = None
        self.agent_id: str | None = None
        self.agent_name: str = "ACP Agent"
        self.role_description: str = ""
        self.session_mgr = AcpSessionManager()
        self._active_tasks: dict[str, asyncio.Task] = {}
        self._pending_tools: dict[str, asyncio.Future] = {}
        self._pending_requests: dict[str, tuple[asyncio.Future, float]] = {}
        self._cancel_event: asyncio.Event | None = None
        self._closing = False  # 关闭保护: 标记 WebSocket 正在关闭, 拒绝新 dispatch
        self._close_lock = asyncio.Lock()  # 防止并发 close()/cleanup()
        self._cwd: str = ""  # IDE project root
        # 当前 prompt 的性能计数，供 [ACP-PERF] prompt_done / first_chunk 使用
        self._current_prompt_perf: dict | None = None
        self._chunk_buffer = ""
        self._chunk_idle_task: asyncio.Task | None = None
        # 参考 gptme PR #1586: 时间驱动周期性 flush, 确保 LLM 流式输出期间
        # 不依赖内容边界即可每 150ms 发送一次文本到插件端
        self._periodic_flush_task: asyncio.Task | None = None
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
        try:
            async for raw in self.ws.iter_text():
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

                logger.debug(f"[ACP conn={self.conn_id}] method={method} id={msg_id}")

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
                    elif method == "_clawith/tool_result":
                        result = await self._handle_tool_result(params)
                    elif method == "session/set_mode":
                        result = await self._handle_set_mode(params)
                    else:
                        await self._send_error(msg_id, -32601, f"Method not found: {method}")
                        continue

                    if msg_id is not None:
                        await self._send_result(msg_id, result)
                except Exception as e:
                    logger.error(f"[ACP] {method} 失败", exc_info=True)
                    await self._send_error(msg_id, -32603, "Internal error")
        finally:
            current_acp_handler.reset(token)

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

            await self._push_to_ide(json.dumps({
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": self.session_id,
                    "update": {"traceId": get_trace_id(), "type": "prompt_done"}
                }
            }))

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
        client_agent_id = (params.get("_meta") or {}).get("agentId")
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
        trace_id = str(uuid.uuid4())[:8]
        # 将模块级 logger 替换为绑定 trace_id 的本地副本，本作用域内所有日志自动带 trace_id
        _log = logger.bind(trace_id=trace_id)  # type: ignore[assignment]
        from app.services.llm.caller import call_llm_with_failover

        self._current_prompt_perf = {
            "start": time.perf_counter(),
            "pushed_chunks": 0,
            "first_chunk_ms": None,
            "reply_chars": 0,
        }
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
            args = data.get("args") or {}
            if status == "running":
                _tool_call_starts[call_id] = time.perf_counter()
                elapsed_ms = 0
            else:
                t0 = _tool_call_starts.pop(call_id, time.perf_counter())
                elapsed_ms = int((time.perf_counter() - t0) * 1000)
            path_hint = args.get("path") or args.get("command") or ""
            # 去掉公共前缀 cd <cwd> && , 保留命令的区分部分。
            # 不再硬截断 — 插件端 ToolTimelineRow 无长度限制, 截断导致不同命令在 UI 上无法区分。
            if path_hint and self._cwd and isinstance(path_hint, str):
                cwd_prefix = f"cd {self._cwd} && "
                if path_hint.startswith(cwd_prefix):
                    path_hint = path_hint[len(cwd_prefix):]
            cn_name = _TOOL_CN_NAME.get(tool_name, tool_name)
            title = cn_name if not path_hint else f"{cn_name}({path_hint})"
            _log.info(
                f"[ACP-PERF] tool_notify tool={tool_name} call_id={call_id} status={status} elapsed_ms={elapsed_ms}"
            )
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

        async def _do_llm():
            full_reply = ""
            try:
                # Load primary model from agent config
                primary_model = None
                fallback_model = None
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

                history: list[dict] = []
                if self.session_id and self.user_id:
                    history = await self.session_mgr.load_history_for_llm(
                        self.session_id, self.user_id
                    )

                # 注入 agent context（soul.md + memory.md + skills + MCP 配置）
                ctx_llm_messages = list(history)
                if self.agent_id:
                    try:
                        agent_ctx = await build_agent_context(
                            agent_id=uuid.UUID(self.agent_id),
                            agent_name=self.agent_name,
                            role_description=self.role_description,
                        )
                        if agent_ctx and agent_ctx[0]:
                            ctx_prompt = agent_ctx[0] + "\n\n" + agent_ctx[1]
                            ctx_llm_messages.insert(0, {"role": "system", "content": ctx_prompt})
                            _log.info(
                                f"[ACP-CTX] build_agent_context 注入完成 "
                                f"static={len(agent_ctx[0])} dynamic={len(agent_ctx[1])} "
                                f"agent={self.agent_id}"
                            )
                    except Exception as e:
                        _log.warning(f"[ACP-CTX] build_agent_context 失败 (非阻塞): {e}")
                llm_messages = ctx_llm_messages + [
                    {"role": "user", "content": user_text_for_llm}
                ]
                _log.info(
                    f"[ACP-CTX] prompt history={len(history)} "
                    f"total={len(llm_messages)} session={self.session_id}"
                )
                _log.info(
                    f"[ACP-PERF] round_start conn={self.conn_id} session={self.session_id} "
                    f"primary_model={getattr(primary_model, 'model', None)} "
                    f"fallback_model={getattr(fallback_model, 'model', None)}"
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

                await self._push_thinking("第 1 轮：规划中…")

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
                    cancel_event=self._cancel_event,
                    on_chunk=push_chunk,
                    on_thinking=self._push_thinking,
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

        try:
            result = await asyncio.wait_for(_do_llm(), timeout=LLM_TIMEOUT_SECONDS)
            _log.info(f"[ACP] prompt 完成: {len(result)} chars")
            # 诊断日志: 打印最终回复前 120 字符, 用于确认答案内容正确抵达
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
        except asyncio.TimeoutError:
            _log.error(f"[ACP] LLM 超时 ({LLM_TIMEOUT_SECONDS}s)")
            # 超时前清空缓冲区, 确保已流式输出的部分内容不丢失
            await self._flush_chunk_buffer()
            await self._push_chunk("\n\n*错误: AI 响应超时*")
        except asyncio.CancelledError:
            # 取消前清空缓冲区
            await self._flush_chunk_buffer()
            await self._push_chunk("\n\n*[已取消]*")
            raise
        finally:
            self._active_tasks.pop(prompt_id, None)
            self._cancel_event = None

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
        raw = self._chunk_template.replace("__SID__", self.session_id or "").replace(
            "__TEXT__", json.dumps(text, ensure_ascii=False)[1:-1]
        )
        try:
            # asyncio.shield 防止取消中断发送导致 chunk 永久丢失 (buffer 已清空)
            # WebSocket 断开时 send_text 会抛 ConnectionClosedError, 捕获防止重入竞态
            await asyncio.shield(self.ws.send_text(raw))
        except Exception:
            logger.warning("[ACP-FLUSH] 发送失败 conn={} session={} len={}", self.conn_id, self.session_id, len(text))
        preview = text[:60].replace("\n", "\\n")
        logger.info(
            f"[ACP-FLUSH] conn={self.conn_id} session={self.session_id} "
            f"len={len(text)} preview={preview!r}"
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

    async def _push_thinking(self, text: str):
        # 轮次状态（A2）推送时打一条 hint，便于与 [LSP4J-PERF] Round 交叉验证
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
        for pid, task in list(self._active_tasks.items()):
            if not task.done():
                task.cancel()
        logger.info("[ACP] cancel")
        return {"cancelled": True}
    async def _handle_session_close(self, params: dict) -> dict:
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
        """ACP 扩展: 列出可用 Agent 列表 (智能体选择)。"""
        async with async_session() as db:
            result = await db.execute(
                select(AgentModel)
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
        self._cancel_idle_task()
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
        self._pending_tools.clear()
        self._pending_requests.clear()
        logger.info("[ACP] resources cleaned")

    # ── JSON-RPC 序列化 ───────────────────────────────────────

    async def _send_result(self, msg_id, result):
        raw = json.dumps({
            "jsonrpc": "2.0", "id": msg_id, "result": result
        }, ensure_ascii=False, default=str)
        logger.debug(f"[ACP-RAW-OUT] {raw}")
        await asyncio.shield(self.ws.send_text(raw))

    async def _send_error(self, msg_id, code: int, message: str):
        # P1-2: error_ref 随机码，用户报 ref=abc12345 → grep 秒级定位
        error_ref = str(uuid.uuid4())[:8]
        raw = json.dumps({
            "jsonrpc": "2.0", "id": msg_id,
            "error": {"code": code, "message": f"{message} (ref: {error_ref})"}
        }, ensure_ascii=False)
        # 关闭中不报 ERROR — "after websocket.close" 是预期的清理行为
        if self._closing:
            logger.warning(f"[ACP] _send_error (closing) code={code} ref={error_ref} msg={message}")
        else:
            logger.error(f"[ACP] _send_error code={code} ref={error_ref} msg={message}")
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

    async def send_request(self, method: str, params: dict, timeout: float = 30.0):
        """Send ACP JSON-RPC request and wait for response.

        Used for agent->client tool call proxy (e.g. fs/read_text_file).
        
        Note: the future is NOT removed from _pending_requests on timeout,
        so a late-arriving response can still be consumed by a subsequent
        retry or cleanup. asyncio.wait_for does NOT cancel a Future, so
        the future stays valid for later resolution."""
        # 惰性清理: 条目数 > 50 或最早条目 > 120s 时触发清理
        if len(self._pending_requests) > 50:
            await self.cleanup_stale_requests()
        elif self._pending_requests:
            oldest = min(ts for _, (_, ts) in self._pending_requests.items())
            if time.monotonic() - oldest > 120:
                await self.cleanup_stale_requests()
        # 背压控制: pending 积压时拒绝新请求, 防止 IDE 端无响应时队列无限增长
        # 读写分离: 读操作 (VFS/索引) 允许更高并发, 写操作保持保守
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
        elif pending_count >= 3:
            oldest_age = min(time.monotonic() - ts for _, (_, ts) in self._pending_requests.items())
            logger.warning(
                f"[ACP-QUEUE] pending 积压: count={pending_count} "
                f"method={method} oldest_age={oldest_age:.0f}s"
            )
        req_id = str(uuid.uuid4())
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_requests[req_id] = (future, time.monotonic())
        raw = json.dumps({
            "type": "com.agentclientprotocol.rpc.JsonRpcRequest",
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }, ensure_ascii=False, default=str)
        logger.debug(f"[ACP-RAW-OUT] {raw}")
        t0 = time.perf_counter()
        logger.info(
            f"[ACP-PERF] send_request START session={self.session_id} method={method} "
            f"timeout={timeout}s req_id={req_id}"
        )
        try:
            await asyncio.shield(self.ws.send_text(raw))
            result = await asyncio.wait_for(future, timeout=timeout)
            elapsed = time.perf_counter() - t0
            logger.info(
                f"[ACP-PERF] send_request DONE session={self.session_id} method={method} "
                f"elapsed={elapsed:.3f}s req_id={req_id}"
            )
            return result
        except asyncio.TimeoutError:
            elapsed = time.perf_counter() - t0
            logger.warning(
                f"[ACP-PERF] send_request TIMEOUT session={self.session_id} method={method} "
                f"elapsed={elapsed:.3f}s timeout={timeout}s req_id={req_id} "
                f"pending={len(self._pending_requests)} (future kept for late response)"
            )
            raise TimeoutError(f"ACP request {method} timed out after {timeout}s")

    def _build_project_context(self) -> str:
        """Build IDE project context string for prompt injection."""
        cwd = getattr(self, "_cwd", "")
        if not cwd:
            return ""

        return (
            f"## IDE Project Environment\n"
            f"Working directory: {cwd}\n"
            f"\n"
            f"## Rules\n"
            f"- 回答简洁直接。如果用户的问题不需要读取项目文件(如问候/身份确认), 只回复不扫描项目。\n"
            f"- 需要读文件时用 read_file, 不要用 cat/head/tail。\n"
            f"- build/test/git 用 execute_command。\n"
            f"- 代码搜索优先使用 IDE 索引工具 (find_class/find_symbol/search_text/"
            f"find_references/find_definition/find_file/find_implementations/list_files 等)，"
            f"比 grep -r 快 100-1000 倍且支持语义匹配 (多态继承、接口实现等)。"
            f"grep -r 仅作为 IDE 索引不可用时的降级方案。\n"
            f"- Agent 内部文件(memory/, skills/ 前缀)不在 IDE, 由后端本地读取。\n"
        )


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
