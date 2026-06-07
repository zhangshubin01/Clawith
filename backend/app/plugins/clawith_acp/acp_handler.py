"""ACP JSON-RPC 2.0 协议处理 — Agent 路由到 call_llm_with_failover。

直接对接 Clawith 已有 LLM 调用链:
  call_llm_with_failover (caller.py:691) + build_agent_context (agent_context.py:243)
"""

import asyncio
import json
import os
import re
import time
import uuid

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.agent import Agent as AgentModel
from app.plugins.clawith_acp.acp_session import AcpSessionManager
from app.plugins.clawith_acp.tool_bridge import current_acp_handler
from app.plugins.clawith_acp.tool_hooks import install_acp_tool_hooks

ACP_PROTOCOL_VERSION = 1
LLM_TIMEOUT_SECONDS = int(os.getenv("ACP_LLM_TIMEOUT_SECONDS", "600"))

_CHUNK_HARD_FLUSH = 480
_CHUNK_SOFT_FLUSH = 220
_CHUNK_IDLE_MIN = 80
_CHUNK_IDLE_SEC = 0.75
_SENTENCE_END_RE = re.compile(r'[.!?][)"\'`]*\s$')
_SPLIT_NL_RE = re.compile(r"(?<=\n)")

# Install ACP tool hooks (idempotent)
install_acp_tool_hooks()
# 工具名 → ACP ToolKind 映射，供 _push_tool_call 填充 kind 字段
_kind_map = {
    "read_file": "read", "write_file": "edit", "edit_file": "edit",
    "delete_file": "delete", "execute_command": "execute", "bash": "execute",
}
class AcpHandler:
    """ACP JSON-RPC 2.0 路由 + Agent 管理。"""

    def __init__(self, websocket, user_id: str):
        self.ws = websocket
        self.user_id = user_id
        # 短连接 ID，便于 docker logs 对齐 IDE gen 与后端 handler
        self.conn_id = uuid.uuid4().hex[:8]
        self.session_id: str | None = None
        self.agent_id: str | None = None
        self.agent_name: str = "ACP Agent"
        self.role_description: str = ""
        self.session_mgr = AcpSessionManager()
        self._active_tasks: dict[str, asyncio.Task] = {}
        self._pending_tools: dict[str, asyncio.Future] = {}
        self._pending_requests: dict[str, tuple[asyncio.Future, float]] = {}
        self._cancel_event: asyncio.Event | None = None
        self._cwd: str = ""  # IDE project root
        # 当前 prompt 的性能计数，供 [ACP-PERF] prompt_done / first_chunk 使用
        self._current_prompt_perf: dict | None = None
        self._tool_call_starts: dict[str, float] = {}
        self._chunk_buffer = ""
        self._chunk_idle_task: asyncio.Task | None = None
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
                logger.debug(f"[ACP-RAW-IN] {raw}")
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
                    logger.error(f"[ACP] {method} 失败: {e}")
                    await self._send_error(msg_id, -32603, str(e))
        finally:
            current_acp_handler.reset(token)

    def _has_in_flight_prompt(self) -> bool:
        """同连接是否已有在途 prompt（含派发 task 与 LLM task）。"""
        return any(not task.done() for task in self._active_tasks.values())

    async def _dispatch_prompt(self, params: dict, msg_id, dispatch_key: str | None = None):
        """在独立 task 中执行 prompt，结束后回写 JSON-RPC result。

        必须从 run() 读循环外调用，否则 LLM 工具 send_request 无法读入 IDE 响应（R1 死锁）。
        """
        logger.info(f"[ACP] prompt 派发开始: session={self.session_id} id={msg_id}")
        try:
            result = await self._handle_prompt(params, msg_id)
            if msg_id is not None and result is not None:
                await self._send_result(msg_id, result)
        except Exception as e:
            logger.error(f"[ACP] dispatch_prompt 失败: session={self.session_id} id={msg_id} err={e}")
            if msg_id is not None:
                await self._send_error(msg_id, -32603, str(e))
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
                "fs": {"readTextFile": True, "writeTextFile": True},
                "terminal": True,
            },
            "agentInfo": {"name": "clawith-acp-agent", "version": "0.1.0"},
        }

    async def _handle_session_new(self, params: dict) -> dict:
        cwd = params.get("cwd", "")

        # Accept agentId from client _meta
        agent = None
        client_agent_id = (params.get("_meta") or {}).get("agentId")
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

        task = asyncio.create_task(
            self._run_prompt_with_timeout(prompt_id, user_text_raw, user_text_for_llm)
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
        from app.services.llm.caller import call_llm_with_failover

        self._current_prompt_perf = {
            "start": time.perf_counter(),
            "pushed_chunks": 0,
            "first_chunk_ms": None,
            "reply_chars": 0,
        }
        self._tool_call_starts.clear()
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
                self._tool_call_starts[call_id] = time.perf_counter()
                elapsed_ms = 0
            else:
                t0 = self._tool_call_starts.pop(call_id, time.perf_counter())
                elapsed_ms = int((time.perf_counter() - t0) * 1000)
            path_hint = args.get("path") or args.get("command") or ""
            title = tool_name if not path_hint else f"{tool_name}({str(path_hint)[:80]})"
            logger.info(
                f"[ACP-PERF] tool_notify conn={self.conn_id} session={self.session_id} "
                f"tool={tool_name} call_id={call_id} status={status} elapsed_ms={elapsed_ms}"
            )
            acp_status = (
                "in_progress"
                if status == "running"
                else "completed"
                if status == "done"
                else status
            )
            kind = _kind_map.get(tool_name, "other")
            locations = []
            if path := args.get("path"):
                locations.append({"path": str(path)})
            elif cmd := args.get("command"):
                locations.append({"path": f"$ {cmd}"})
            try:
                await self._push_tool_call(call_id, title, acp_status, kind=kind, locations=locations)
            except Exception as push_err:
                logger.warning(f"[ACP-PERF] tool_notify push failed: {push_err}")

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

                history: list[dict] = []
                if self.session_id and self.user_id:
                    history = await self.session_mgr.load_history_for_llm(
                        self.session_id, self.user_id
                    )
                llm_messages = list(history) + [
                    {"role": "user", "content": user_text_for_llm}
                ]
                logger.info(
                    f"[ACP-CTX] prompt history={len(history)} "
                    f"total={len(llm_messages)} session={self.session_id}"
                )
                logger.info(
                    f"[ACP-PERF] round_start conn={self.conn_id} session={self.session_id}"
                )

                await self._push_thinking("第 1 轮：规划中…")

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
                if perf := self._current_prompt_perf:
                    perf["reply_chars"] = len(full_reply)
                if full_reply and (self._current_prompt_perf or {}).get("pushed_chunks", 0) == 0:
                    logger.warning(f"[ACP] prompt 未收到流式 chunk，使用最终结果兜底推送: {len(full_reply)} chars")
                    await self._push_chunk(full_reply)
            except asyncio.CancelledError:
                logger.info(f"[ACP] LLM 调用取消: {prompt_id}")
                raise
            except Exception as e:
                logger.error(f"[ACP] LLM 调用失败: {e}")
                await self._push_chunk(f"\n\n*错误: {e}*")
            return full_reply

        try:
            result = await asyncio.wait_for(_do_llm(), timeout=LLM_TIMEOUT_SECONDS)
            logger.info(f"[ACP] prompt 完成: {len(result)} chars")
            if result and self.session_id and self.agent_id:
                await self.session_mgr.persist_turn(
                    session_id=self.session_id,
                    user_id=self.user_id,
                    agent_id=self.agent_id,
                    user_text=user_text_raw,
                    assistant_text=result,
                )
        except asyncio.TimeoutError:
            logger.error(f"[ACP] LLM 超时 ({LLM_TIMEOUT_SECONDS}s)")
            await self._push_chunk("\n\n*错误: AI 响应超时*")
        except asyncio.CancelledError:
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
        self._chunk_idle_task = asyncio.ensure_future(self._idle_flush())

    async def _idle_flush(self):
        await asyncio.sleep(_CHUNK_IDLE_SEC)
        if self._chunk_buffer:
            await self._do_flush()

    async def _do_flush(self):
        self._cancel_idle_task()
        text = self._chunk_buffer
        self._chunk_buffer = ""
        raw = self._chunk_template.replace("__SID__", self.session_id or "").replace(
            "__TEXT__", json.dumps(text, ensure_ascii=False)[1:-1]
        )
        await self.ws.send_text(raw)

    def _cancel_idle_task(self):
        if self._chunk_idle_task and not self._chunk_idle_task.done():
            self._chunk_idle_task.cancel()
            self._chunk_idle_task = None

    async def _flush_chunk_buffer(self):
        self._cancel_idle_task()
        if self._chunk_buffer:
            text = self._chunk_buffer
            self._chunk_buffer = ""
            raw = self._chunk_template.replace("__SID__", self.session_id or "").replace(
                "__TEXT__", json.dumps(text, ensure_ascii=False)[1:-1]
            )
            await self.ws.send_text(raw)

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

    async def _find_agent(self):
        """查找最近更新的 Agent (无 user_id 过滤, 取租户内活跃 Agent)。"""
        async with async_session() as db:
            result = await db.execute(
                select(AgentModel)
                .where(AgentModel.status == "idle")
                .order_by(AgentModel.updated_at.desc())
                .limit(1)
            )
            agent = result.scalar_one_or_none()
            if not agent:
                # 备选: 任意活跃 Agent
                result = await db.execute(
                    select(AgentModel)
                    .order_by(AgentModel.updated_at.desc())
                    .limit(1)
                )
                agent = result.scalar_one_or_none()
            return agent

    async def _find_agent_by_id(self, agent_id: str):
        """Find agent by UUID."""
        async with async_session() as db:
            try:
                from uuid import UUID
                aid = UUID(agent_id)
                result = await db.execute(
                    select(AgentModel).where(AgentModel.id == aid)
                )
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
        self._cancel_idle_task()
        for task in self._active_tasks.values():
            task.cancel()
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
        await self.ws.send_text(raw)

    async def _send_error(self, msg_id, code: int, message: str):
        raw = json.dumps({
            "jsonrpc": "2.0", "id": msg_id,
            "error": {"code": code, "message": message}
        }, ensure_ascii=False)
        logger.debug(f"[ACP-RAW-OUT] {raw}")
        await self.ws.send_text(raw)

    async def _send_notification(self, method: str, params: dict):
        raw = json.dumps({
            "jsonrpc": "2.0", "method": method, "params": params
        }, ensure_ascii=False, default=str)
        logger.debug(f"[ACP-RAW-OUT] {raw}")
        await self.ws.send_text(raw)

    async def _send_json(self, data: dict):
        raw = json.dumps(data, ensure_ascii=False, default=str)
        logger.debug(f"[ACP-RAW-OUT] {raw}")
        await self.ws.send_text(raw)

    async def send_request(self, method: str, params: dict, timeout: float = 30.0):
        """Send ACP JSON-RPC request and wait for response.

        Used for agent->client tool call proxy (e.g. fs/read_text_file).
        
        Note: the future is NOT removed from _pending_requests on timeout,
        so a late-arriving response can still be consumed by a subsequent 
        retry or cleanup. asyncio.wait_for does NOT cancel a Future, so 
        the future stays valid for later resolution."""
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
            await self.ws.send_text(raw)
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
            f"## IDE Project Environment (Important)\n"
            f"You are working inside the user's IDE. Active project: {cwd}\n"
            f"\n"
            f"## File Access\n"
            f"- Use read_file to read project files (absolute or relative paths).\n"
            f"- Example: read_file(path=\"{cwd}/build.gradle.kts\")\n"
            f"- Project files DO exist. Read them directly without confirming.\n"
            f"- Use execute_command for builds, tests, and git operations.\n"
            f"- Use execute_command with grep -r to search code.\n"
            f"\n"
            f"## Notes\n"
            f"- Agent config files (memory/, skills/ prefix) are local, not IDE.\n"
            f"- On first interaction, read build.gradle.kts to understand the project.\n"
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
