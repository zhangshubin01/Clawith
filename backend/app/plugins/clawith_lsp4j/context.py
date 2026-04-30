"""LSP4J 插件上下文变量定义。

将 ContextVar 集中在独立模块中，避免 router.py 与 tool_hooks.py 之间的循环导入。
所有可变默认值使用 default=None，在 router.py 中按需 set 为 {} / [] 等。
"""

import asyncio
from contextvars import ContextVar
from typing import Any


# 当前活跃的 LSP4J WebSocket 连接（用于 tool_hooks 判断 LSP4J 通道是否活跃）
current_lsp4j_ws: ContextVar[Any | None] = ContextVar("current_lsp4j_ws", default=None)

# 当前 LSP4J 连接的 pending tool Futures（toolCallId → asyncio.Future）
current_lsp4j_pending_tools: ContextVar[dict] = ContextVar(
    "current_lsp4j_pending_tools", default=None
)

# 当前 LSP4J 连接的消息历史（用于 call_llm 前回填）
current_lsp4j_message_history: ContextVar[list] = ContextVar(
    "current_lsp4j_message_history", default=None
)

# 当前 LSP4J 连接的 session_id（由 chat/ask 请求传入）
current_lsp4j_session_id: ContextVar[str | None] = ContextVar(
    "current_lsp4j_session_id", default=None
)

# 当前 LSP4J 连接的 user_id（认证后设置）
current_lsp4j_user_id: ContextVar[Any | None] = ContextVar(
    "current_lsp4j_user_id", default=None
)

# 当前 LSP4J 连接的 agent_id（认证后设置）
current_lsp4j_agent_id: ContextVar[Any | None] = ContextVar(
    "current_lsp4j_agent_id", default=None
)

# 活跃路由器映射表：(user_id_hex, agent_id_hex) → JSONRPCRouter 实例
# 使用复合键防止不同用户连接同一 agent 时互相覆盖，避免跨用户工具调用泄漏
_active_routers: dict[tuple[str, str], Any] = {}
_active_routers_lock = asyncio.Lock()


async def register_active_router(agent_key: tuple[str, str], router: Any) -> None:
    """线程安全注册活跃路由器。"""
    async with _active_routers_lock:
        _active_routers[agent_key] = router


async def unregister_active_router(agent_key: tuple[str, str] | None) -> None:
    """线程安全移除活跃路由器。"""
    if agent_key is None:
        return
    async with _active_routers_lock:
        _active_routers.pop(agent_key, None)


async def get_active_router(agent_key: tuple[str, str]) -> Any | None:
    """线程安全获取路由器。"""
    async with _active_routers_lock:
        return _active_routers.get(agent_key)


async def list_active_routers() -> list[tuple[tuple[str, str], Any]]:
    """返回活跃路由器快照，避免迭代期并发修改。"""
    async with _active_routers_lock:
        return list(_active_routers.items())
