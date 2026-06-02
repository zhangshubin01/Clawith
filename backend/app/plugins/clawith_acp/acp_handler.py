"""ACP JSON-RPC 2.0 协议处理 — Agent 路由到 call_llm_with_failover。

直接对接 Clawith 已有 LLM 调用链:
  call_llm_with_failover (caller.py:691) + build_agent_context (agent_context.py:243)
"""

import asyncio
import json
import uuid

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.agent import Agent as AgentModel
from app.plugins.clawith_acp.acp_session import AcpSessionManager
from app.plugins.clawith_acp.tool_bridge import current_acp_handler

ACP_PROTOCOL_VERSION = 1
LLM_TIMEOUT_SECONDS = 600  # LLM 调用超时 (10 分钟)


class AcpHandler:
    """ACP JSON-RPC 2.0 路由 + Agent 管理。"""

    def __init__(self, websocket, user_id: str):
        self.ws = websocket
        self.user_id = user_id
        self.session_id: str | None = None
        self.agent_id: str | None = None
        self.agent_name: str = "ACP Agent"
        self.role_description: str = ""
        self.session_mgr = AcpSessionManager()
        self._active_tasks: dict[str, asyncio.Task] = {}
        self._pending_tools: dict[str, asyncio.Future] = {}
        self._cancel_event: asyncio.Event | None = None

    async def run(self):
        """消息循环: 接收 JSON-RPC → 分发 → 返回响应。"""
        token = current_acp_handler.set(self)
        try:
            async for raw in self.ws.iter_text():
                logger.warning(f"[ACP-RAW-IN] {raw}")
                try:
                    request = json.loads(raw)
                except json.JSONDecodeError:
                    await self._send_error(None, -32700, "Parse error")
                    continue

                method = request.get("method")
                msg_id = request.get("id")
                params = request.get("params", {})

                logger.debug(f"[ACP] method={method} id={msg_id}")

                try:
                    if method == "initialize":
                        result = await self._handle_initialize(params)
                    elif method == "session/new":
                        result = await self._handle_session_new(params)
                    elif method == "session/load":
                        result = await self._handle_session_load(params)
                    elif method == "session/prompt":
                        result = await self._handle_prompt(params, msg_id)
                    elif method == "$/cancelRequest":
                        await self._handle_cancel(params)
                        continue
                    elif method == "session/cancel":
                        result = await self._handle_cancel(params)
                    elif method == "session/close":
                        result = await self._handle_session_close(params)
                    elif method == "_clawith/list_agents":
                        result = await self._handle_list_agents(params)
                    elif method == "_clawith/tool_result":
                        await self._handle_tool_result(params)
                        continue
                    elif method == "session/set_mode":
                        result = await self._handle_set_mode(params)
                    elif method == "session/set_model":
                        result = await self._handle_set_model(params)
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
        agent = await self._find_agent()
        if not agent:
            raise ValueError("未找到可用 Agent")

        self.agent_id = str(agent.id)
        self.agent_name = agent.name if agent else "ACP Agent"
        self.role_description = agent.role_description if agent else ""

        session_id = await self.session_mgr.create(
            user_id=self.user_id,
            agent_id=self.agent_id,
            cwd=cwd,
        )
        self.session_id = session_id
        logger.info(f"[ACP] session/new: id={session_id} agent={self.agent_id}")
        return {"sessionId": session_id, "cwd": cwd}

    async def _handle_session_load(self, params: dict) -> dict:
        session_id = params.get("sessionId", "")
        result = await self.session_mgr.load(session_id, self.user_id)
        if not result:
            return {"error": "Session not found"}
        self.session_id = session_id
        self.agent_id = result["agent_id"]
        logger.info(f"[ACP] session/load: id={session_id}")
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

        user_input = _extract_user_text(messages)
        if not user_input:
            user_input = json.dumps(messages, ensure_ascii=False)

        prompt_id = str(uuid.uuid4())
        self._cancel_event = asyncio.Event()

        task = asyncio.create_task(
            self._run_prompt_with_timeout(prompt_id, user_input)
        )
        self._active_tasks[prompt_id] = task

        try:
            await task
            return {"stopReason": "end_turn"}
        except asyncio.CancelledError:
            logger.info(f"[ACP] prompt 取消: {prompt_id}")
            return {"stopReason": "cancelled"}

    async def _run_prompt_with_timeout(self, prompt_id: str, user_input: str):
        """LLM 调用 with 600s 超时 + cancel_event。"""
        from app.services.llm.caller import call_llm_with_failover

        pushed_chunks = 0

        async def push_chunk(chunk: str):
            nonlocal pushed_chunks
            pushed_chunks += 1
            await self._push_chunk(chunk)

        async def _do_llm():
            full_reply = ""
            try:
                result = await call_llm_with_failover(
                    primary_model=None,       # 使用 Agent 配置的默认模型
                    fallback_model=None,
                    messages=[{"role": "user", "content": user_input}],
                    agent_name=self.agent_name,
                    role_description=self.role_description,
                    agent_id=self.agent_id,
                    user_id=self.user_id,
                    session_id=self.session_id or "",
                    cancel_event=self._cancel_event,
                    on_chunk=lambda chunk: asyncio.ensure_future(
                        push_chunk(chunk)
                    ),
                    on_thinking=lambda thinking: asyncio.ensure_future(
                        self._push_thinking(thinking)
                    ),
                )
                full_reply = result or ""
                if full_reply and pushed_chunks == 0:
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
        await self._send_notification("session/update", {
            "sessionId": self.session_id,
            "update": {
                "agentMessageChunk": {
                    "content": {"type": "text", "text": text},
                }
            },
        })

    async def _push_thinking(self, text: str):
        await self._send_notification("session/update", {
            "sessionId": self.session_id,
            "update": {
                "agentThoughtChunk": {
                    "content": {"type": "text", "text": text},
                }
            },
        })

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

    async def _handle_tool_result(self, params: dict):
        tool_id = params.get("toolId", "")
        future = self._pending_tools.pop(tool_id, None)
        if future and not future.done():
            if params.get("success"):
                future.set_result(params.get("result", ""))
            else:
                future.set_exception(Exception(params.get("error", "工具执行失败")))
            logger.info(f"[ACP] tool_result: id={tool_id}")

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

    async def cleanup(self):
        for task in self._active_tasks.values():
            task.cancel()
        for future in self._pending_tools.values():
            if not future.done():
                future.cancel()
        self._active_tasks.clear()
        self._pending_tools.clear()
        logger.info("[ACP] 资源已清理")

    # ── JSON-RPC 序列化 ───────────────────────────────────────

    async def _send_result(self, msg_id, result):
        raw = json.dumps({
            "jsonrpc": "2.0", "id": msg_id, "result": result
        }, ensure_ascii=False, default=str)
        logger.warning(f"[ACP-RAW-OUT] {raw}")
        await self.ws.send_text(raw)

    async def _send_error(self, msg_id, code: int, message: str):
        raw = json.dumps({
            "jsonrpc": "2.0", "id": msg_id,
            "error": {"code": code, "message": message}
        }, ensure_ascii=False)
        logger.warning(f"[ACP-RAW-OUT] {raw}")
        await self.ws.send_text(raw)

    async def _send_notification(self, method: str, params: dict):
        raw = json.dumps({
            "jsonrpc": "2.0", "method": method, "params": params
        }, ensure_ascii=False, default=str)
        logger.warning(f"[ACP-RAW-OUT] {raw}")
        await self.ws.send_text(raw)

    async def _send_json(self, data: dict):
        raw = json.dumps(data, ensure_ascii=False, default=str)
        logger.warning(f"[ACP-RAW-OUT] {raw}")
        await self.ws.send_text(raw)


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
