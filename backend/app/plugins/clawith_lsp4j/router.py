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
import uuid
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException, Query
from loguru import logger
from sqlalchemy import select

from app.core.security import verify_api_key_or_token
from app.core.permissions import check_agent_access
from app.database import async_session
from app.models.agent import Agent as AgentModel
from app.models.llm import LLMModel
from app.models.user import User

from .context import (
    current_lsp4j_ws,
    current_lsp4j_pending_tools,
    current_lsp4j_message_history,
    current_lsp4j_session_id,
    current_lsp4j_user_id,
    current_lsp4j_agent_id,
    _active_routers,
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
        if agent is None:
            logger.warning("[LSP4J-LIFE] agent_override %r not found", override)
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


@router.websocket("/ws")
async def lsp4j_websocket_endpoint(
    websocket: WebSocket,
    agent_id: str = Query(...),
    token: str = Query(None),
):
    """LSP4J WebSocket 端点。

    URL 参数：
    - agent_id: 智能体 UUID 或名称
    - token: API Key（cw- 前缀）或 JWT

    认证失败关闭码：
    - 4001: token 无效或缺失
    - 4002: agent 未找到
    """
    # 先 accept 再认证（LSP4J 框架要求）
    logger.info("[LSP4J-LIFE] WS accepting connection from {}:{}", websocket.client.host if websocket.client else "unknown", websocket.client.port if websocket.client else "unknown")
    await websocket.accept()
    logger.info("[LSP4J-LIFE] WS connection accepted")

    # 1. token 认证
    logger.info("[LSP4J-LIFE] WS authenticating with token: {}...", token[:20] if token else "None")
    if not token:
        logger.warning("[LSP4J-LIFE] WS auth failed: missing token")
        await websocket.close(code=4001, reason="Missing token")
        return

    try:
        user_id = await verify_api_key_or_token(token)
        logger.info("[LSP4J-LIFE] WS auth success: user_id={}", user_id)
    except HTTPException as e:
        msg = e.detail if isinstance(e.detail, str) else "Unauthorized"
        logger.warning("[LSP4J-LIFE] WS auth failed: {}", msg)
        await websocket.close(code=4001, reason=msg)
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
    _active_routers[agent_key] = jsonrpc
    jsonrpc._agent_key = agent_key  # 保存到实例变量，cleanup 时使用

    try:
        # 4. 消息循环
        # ★ 使用 create_task 并发处理每条消息，避免串行等待导致死锁：
        #    chat/ask 的 call_llm 会 await tool_future，而 tool/invokeResult
        #    需要消息循环读取才能 resolve Future。如果串行 await route()，
        #    消息循环被 chat/ask 阻塞，tool/invokeResult 无法被处理 → 死锁。
        _ws_tasks: set[asyncio.Task] = set()
        while True:
            raw_data = await websocket.receive_text()
            t = asyncio.create_task(jsonrpc.route(raw_data))
            _ws_tasks.add(t)
            t.add_done_callback(_ws_tasks.discard)
    except WebSocketDisconnect:
        logger.info("[LSP4J-LIFE] WS disconnected agent_id={}", agent_obj.id)
    except Exception as e:
        logger.error("[LSP4J-LIFE] WS error: {}", e)
    finally:
        # 5. 清理（按顺序）
        # 5.1 重置 ContextVar
        current_lsp4j_ws.set(None)
        current_lsp4j_user_id.set(None)
        current_lsp4j_agent_id.set(None)
        current_lsp4j_pending_tools.set({})
        current_lsp4j_message_history.set([])
        current_lsp4j_session_id.set(None)

        # 5.2 从 _active_routers 移除（使用实例变量中的复合键）
        _active_routers.pop(getattr(jsonrpc, "_agent_key", None), None)

        # 5.3 resolve 所有 pending Futures（防止协程挂起）
        await jsonrpc.cleanup()

        logger.info("[LSP4J-LIFE] WS cleanup done agent_id={}", agent_obj.id)
