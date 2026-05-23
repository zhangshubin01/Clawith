"""LSP4J WebSocket 端点 + 认证。

提供 WebSocket 端点供通义灵码 IDE 插件连接。
URL 格式：ws://{host}/api/plugins/clawith-lsp4j/ws?agent_id={}&token={}

连接流程：
1. token 认证（verify_api_key_or_token）
2. agent_id 解析（_resolve_agent_override）
3. WebSocket accept + ContextVar 设置
4. 消息循环（LSP Base Protocol 解析 → JSONRPCRouter 路由）
5. finally 清理（ContextVar 重置 + _active_routers 移除 + Futures resolve）
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException, Query
from loguru import logger
from sqlalchemy import select

from app.core.rate_limit import check_ip_rate_limit
from app.core.security import verify_api_key_or_token
from app.core.permissions import check_agent_access
from app.database import async_session
from app.models.agent import Agent as AgentModel
from app.models.llm import LLMModel
from app.models.user import User
from app.services.connection_manager import manager

from .context import (
    current_lsp4j_ws,
    current_lsp4j_pending_tools,
    current_lsp4j_message_history,
    current_lsp4j_session_id,
    current_lsp4j_user_id,
    current_lsp4j_agent_id,
    register_active_router,
    unregister_active_router,
)
from .jsonrpc_router import JSONRPCRouter

router = APIRouter(tags=["lsp4j"])


async def _resolve_agent_override(
    override: str, user_id: uuid.UUID
) -> tuple[AgentModel, LLMModel] | None:
    """查找 agent by UUID 或 name，返回 (agent, model) 或 None。

    复用 ACP 的 _resolve_agent_override 逻辑（ACP router.py:912-936），
    但在 LSP4J 模块内独立定义，避免跨插件导入内部函数。
    """
    try:
        async with async_session() as db:
            agent = None
            try:
                aid = uuid.UUID(override)
                ar = await db.execute(select(AgentModel).where(AgentModel.id == aid))
                agent = ar.scalar_one_or_none()
            except ValueError:
                pass
            if agent is None:
                ar = await db.execute(select(AgentModel).where(AgentModel.name == override))
                agent = ar.scalar_one_or_none()
            # 兜底：IDE 可能以 "clawith" 等通用名连接，DB 中无此名称时回退到用户创建的最新 agent
            if agent is None:
                ar = await db.execute(
                    select(AgentModel)
                    .where(AgentModel.creator_id == user_id)
                    .order_by(AgentModel.created_at.desc())
                    .limit(1)
                )
                agent = ar.scalar_one_or_none()
                if agent is not None:
                    logger.info(
                        "[LSP4J-LIFE] agent_override {!r} not found, fallback to user's agent: id={} name={}",
                        override, agent.id, agent.name,
                    )
            if agent is None:
                logger.warning("[LSP4J-LIFE] agent_override {!r} not found and user has no agents", override)
                return None

            # 权限校验：复用 check_agent_access，检查用户是否有权访问该 agent
            ur = await db.execute(select(User).where(User.id == user_id))
            user_obj = ur.scalar_one_or_none()
            if user_obj is None:
                logger.warning("[LSP4J-LIFE] user_id={} not found in DB", user_id)
                return None
            try:
                await check_agent_access(db, user_obj, agent.id)
            except HTTPException as e:
                logger.warning("[LSP4J-LIFE] user_id={} 无权访问 agent_id={}: {}", user_id, agent.id, e.detail)
                return None

            mr = await db.execute(select(LLMModel).where(LLMModel.id == agent.primary_model_id))
            model = mr.scalar_one_or_none()
            if model is None:
                logger.warning("[LSP4J-LIFE] agent_override %r has no model", override)
                return None
            return agent, model
    except Exception as e:
        logger.error("[LSP4J-LIFE] DB error during agent resolution: {} (override={})", e, override)
        return None


@router.websocket("/ws")
async def lsp4j_websocket_endpoint(
    websocket: WebSocket,
    agent_id: str = Query(...),
    token: str = Query(None),
):
    """LSP4J WebSocket 端点。

    URL 参数：
    - agent_id: 智能体 UUID 或名称
    - token: JWT token

    认证失败关闭码：
    - 4001: token 无效或缺失
    - 4002: agent 未找到
    """
    # 0. IP 粒度速率限制：防止恶意客户端高频创建 WebSocket 连接消耗资源
    client_ip = websocket.client.host if websocket.client else "0.0.0.0"
    try:
        await check_ip_rate_limit(client_ip, "ws_connect_lsp4j")
    except HTTPException as e:
        # 429 时 WebSocket 尚未 accept，直接返回 HTTP 429
        logger.warning("[LSP4J-LIFE] WS rate limited: ip={}", client_ip)
        await websocket.close(code=4001, reason=str(e.detail))
        return

    # 先 accept 再认证（LSP4J 框架要求）
    logger.info("[LSP4J-LIFE] WS accepting connection from {}:{}", websocket.client.host if websocket.client else "unknown", websocket.client.port if websocket.client else "unknown")
    await websocket.accept()
    logger.info("[LSP4J-LIFE] WS connection accepted")

    # 1. token 认证：优先从 URL query parameter 读取，其次从 Authorization header
    if not token:
        auth_header = websocket.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            logger.info("[LSP4J-LIFE] WS token from Authorization header")
        else:
            logger.warning("[LSP4J-LIFE] WS auth failed: missing token (neither query param nor Authorization header)")
            await websocket.close(code=4001, reason="Missing token")
            return
    logger.info("[LSP4J-LIFE] WS authenticating (token length={})", len(token) if token else 0)

    try:
        user_id = await verify_api_key_or_token(token)
        logger.info("[LSP4J-LIFE] WS auth success: user_id={}", user_id)
    except HTTPException as e:
        msg = e.detail if isinstance(e.detail, str) else "Unauthorized"
        # token_expired 使用 debug 级别，避免客户端重连时日志风暴
        if msg == "token_expired":
            logger.warning("[LSP4J-LIFE] WS auth failed: token_expired (client should refresh JWT)")
        else:
            logger.warning("[LSP4J-LIFE] WS auth failed: {}", msg)
        code = 4003 if msg == "token_expired" else 4001
        await websocket.close(code=code, reason=msg)
        return
    except Exception:
        logger.exception("[LSP4J-LIFE] WebSocket auth error")
        await websocket.close(code=4001, reason="Unauthorized")
        return

    # 2. agent 解析
    resolved = await _resolve_agent_override(agent_id, user_id)
    if resolved is None:
        await websocket.close(code=4002, reason="Agent not found")
        return

    agent_obj, model_obj = resolved
    # F01 修复：注册 LSP4J 连接到 ConnectionManager，防止 WebUI 空闲被清理
    manager.register_lsp4j_connection(str(agent_obj.id))

    logger.info(
        "[LSP4J-LIFE] WS connected agent_id={} agent_name={} user_id={} model_id={}",
        agent_obj.id, agent_obj.name, user_id, model_obj.id,
    )

    # 3. 设置 ContextVar
    current_lsp4j_ws.set(websocket)
    current_lsp4j_user_id.set(user_id)
    current_lsp4j_agent_id.set(agent_obj.id)
    current_lsp4j_pending_tools.set({})
    current_lsp4j_message_history.set([])
    current_lsp4j_session_id.set(None)

    # 创建路由器实例
    jsonrpc = JSONRPCRouter(websocket, user_id, agent_obj, model_obj)

    # 注册到 _active_routers（供 tool_hooks 查找）
    # 使用 (user_id, agent_id) 复合键，防止不同用户连接同一 agent 时互相覆盖
    agent_key = (str(user_id), str(agent_obj.id))
    await register_active_router(agent_key, jsonrpc)
    jsonrpc._agent_key = agent_key  # 保存到实例变量，cleanup 时使用

    try:
        # 4. 消息循环 + WebSocket 保活机制
        # ★ 保活任务：每30秒发送一次 LSP ping，防止网络中间件（nginx/云负载均衡）断开空闲连接
        async def keep_alive():
            """
            WebSocket 保活协程
            - 每30秒发送一次 LSP ping 消息
            - 保持连接活跃，避免被网络中间件判定为空闲而断开
            - 使用 LSP 标准 ping 方法，插件端会自动响应
            """
            try:
                while True:
                    await asyncio.sleep(30)  # 30秒保活间隔
                    # 检查连接状态
                    if websocket.client_state.name == 'CONNECTED':
                        try:
                            await websocket.send_ping()
                            logger.debug("[LSP4J-KEEPALIVE] Sent keep-alive ping to agent_id={}", agent_obj.id)
                        except Exception:
                            logger.debug("[LSP4J-KEEPALIVE] Ping failed, connection may be dead, agent_id={}", agent_obj.id)
                            break
            except asyncio.CancelledError:
                logger.info("[LSP4J-KEEPALIVE] Keep-alive task cancelled for agent_id={}", agent_obj.id)
            except Exception as e:
                logger.warning("[LSP4J-KEEPALIVE] Keep-alive error for agent_id={}: {}", agent_obj.id, e)

        # 启动保活任务
        keepalive_task = asyncio.create_task(keep_alive())
        logger.info("[LSP4J-KEEPALIVE] Keep-alive task started, interval=30s, agent_id={}", agent_obj.id)

        # ★ 使用 create_task 并发处理每条消息，避免串行等待导致死锁：
        #    chat/ask 的 call_llm 会 await tool_future，而 tool/invokeResult
        #    需要消息循环读取才能 resolve Future。如果串行 await route()，
        #    消息循环被 chat/ask 阻塞，tool/invokeResult 无法被处理 → 死锁。
        _ws_tasks: set[asyncio.Task] = set()
        _ws_task_times: dict[asyncio.Task, float] = {}

        async def _cleanup_ws_tasks():
            """定期清理 _ws_tasks，防止 CancelledError 导致 done_callback 不触发时 task 泄漏。

            每 120s 扫描一次：
            - 已完成的 task 直接移除
            - 运行超过 600s 的 task 强制 cancel 并移除
            """
            try:
                while True:
                    await asyncio.sleep(120)
                    now = time.monotonic()
                    stale: list[asyncio.Task] = []
                    for t in list(_ws_tasks):
                        if t.done():
                            stale.append(t)
                        elif now - _ws_task_times.get(t, now) > 600:
                            logger.warning(
                                "[LSP4J-TASK-CLEANUP] Force-cancelling task older than 600s: {}",
                                t.get_name() if hasattr(t, 'get_name') else repr(t),
                            )
                            t.cancel()
                            stale.append(t)
                    for t in stale:
                        _ws_tasks.discard(t)
                        _ws_task_times.pop(t, None)
                    if stale:
                        logger.info(
                            "[LSP4J-TASK-CLEANUP] Cleaned up {} stale tasks, {} remaining",
                            len(stale), len(_ws_tasks),
                        )
            except asyncio.CancelledError:
                logger.debug("[LSP4J-TASK-CLEANUP] Cleanup task cancelled for agent_id={}", agent_obj.id)

        cleanup_task = asyncio.create_task(_cleanup_ws_tasks())

        while True:
            raw_data = await websocket.receive_text()
            t = asyncio.create_task(jsonrpc.route(raw_data))
            _ws_tasks.add(t)
            _ws_task_times[t] = time.monotonic()
            t.add_done_callback(_ws_tasks.discard)
            t.add_done_callback(lambda done_t: _ws_task_times.pop(done_t, None))
    except WebSocketDisconnect:
        logger.info("[LSP4J-LIFE] WS disconnected agent_id={}", agent_obj.id)
        # 通知 Chat WS：LSP4J 工具连接已断开，IDE 工具将不可用
        _sid = getattr(jsonrpc, "_session_id", None)
        _agent_id = str(agent_obj.id)
        if _sid:
            try:
                await manager.send_to_session(
                    _agent_id,
                    str(_sid),
                    {
                        "type": "lsp4j_disconnected",
                        "message": "IDE 工具连接已断开，工具调用将不可用。请重新连接 IDE 插件。",
                    },
                )
                logger.info("[LSP4J-LIFE] 已通知 Chat WS: session={} agent={}", _sid, _agent_id)
            except Exception as _e:
                logger.debug("[LSP4J-LIFE] Chat WS 通知失败（可能 Chat WS 已断开）: {}", _e)
        # 会话结束生命周期事件
        _msg_count = len(getattr(jsonrpc, "_message_history", []) or [])
        logger.info(
            "[LSP4J-LIFE] session_end: session={} agent={} msgs={} end_type=disconnect",
            _sid,
            _agent_id,
            _msg_count,
        )
    except Exception as e:
        logger.error("[LSP4J-LIFE] WS error: {}", e)
    finally:
        # 清理保活任务
        if 'keepalive_task' in locals():
            keepalive_task.cancel()
            try:
                await keepalive_task
            except asyncio.CancelledError:
                pass
            logger.info("[LSP4J-KEEPALIVE] Keep-alive task cleaned up for agent_id={}", agent_obj.id)

        # 清理 _ws_tasks 定期清理任务
        if 'cleanup_task' in locals():
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
            logger.info("[LSP4J-TASK-CLEANUP] Cleanup task stopped for agent_id={}", agent_obj.id)
        # 强制清空残留任务
        if '_ws_tasks' in locals() and _ws_tasks:
            pending_count = len(_ws_tasks)
            for t in list(_ws_tasks):
                if not t.done():
                    t.cancel()
            logger.info("[LSP4J-TASK-CLEANUP] Cancelled {} pending tasks on disconnect", pending_count)

        # F01 修复：注销 LSP4J 连接
        manager.unregister_lsp4j_connection(str(agent_obj.id))
        # 5. 清理（按顺序）
        # 5.1 重置 ContextVar
        current_lsp4j_ws.set(None)
        current_lsp4j_user_id.set(None)
        current_lsp4j_agent_id.set(None)
        current_lsp4j_pending_tools.set({})
        current_lsp4j_message_history.set([])
        current_lsp4j_session_id.set(None)

        # 5.2 resolve 所有 pending Futures（防止协程挂起，先于 unregister 避免竞态）
        await jsonrpc.cleanup()

        # 5.3 从 _active_routers 移除（使用实例变量中的复合键）
        await unregister_active_router(getattr(jsonrpc, "_agent_key", None))

        logger.info("[LSP4J-LIFE] WS cleanup done agent_id={}", agent_obj.id)
