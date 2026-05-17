"""WebSocket 连接管理器 — 管理每个 Agent 的 WebSocket 连接，包含心跳和空闲清理。

从 app.api.websocket 提取，独立文件便于复用。
"""

import asyncio
import time
from dataclasses import dataclass

from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger

MAX_CONNECTIONS = 500

# #103 修复：模型配置 TTL 缓存，避免每轮消息都查询 DB
_MODEL_CONFIG_CACHE_TTL = 60.0  # 60 秒缓存
_model_config_cache: dict[str, tuple[float, dict | None]] = {}

# #134 修复：断开连接日志防抖（同一用户在 100ms 内多次断开只记录一条汇总日志）
_DISCONNECT_DEBOUNCE_WINDOW = 0.1
_disconnect_debounce: dict[str, tuple[float, int]] = {}  # user_id → (last_ts, count)


@dataclass
class _ConnectionInfo:
    """WebSocket 连接信息（#98 修复：类型安全的数据类替代裸元组）。

    字段说明：
    - ws: WebSocket 实例
    - session_id: 会话 ID（UUID 字符串）
    - user_id: 用户 ID（UUID 字符串）
    - connected_at: 连接建立时间（monotonic 秒）
    - last_activity: 最后真实消息活动时间（#104 修复：不与 ping 混淆）
    - last_ping_ts: 最后 ping 成功响应时间（#104 修复）
    - ping_count: 连续仅 ping 无消息的次数（#104+#136 修复：超过 5 次主动断连，约300秒空闲）
    """

    ws: "WebSocket"
    session_id: str = ""
    user_id: str = ""
    connected_at: float = 0.0
    last_activity: float = 0.0
    last_ping_ts: float = 0.0
    ping_count: int = 0


class ConnectionManager:
    """管理每个 Agent 的 WebSocket 连接，包含心跳和空闲清理。

    #98 修复：用 _ConnectionInfo dataclass 替代裸元组，类型安全且可读。
    #108 修复：同一 session 多连接去重。
    #104 修复：区分 ping 和真实活动，半死连接主动断连。
    """

    def __init__(self):
        self.active_connections: dict[str, dict[str, _ConnectionInfo]] = {}
        self._cleanup_task: asyncio.Task | None = None
        self._cleanup_started: bool = False

    @property
    def total_connections(self) -> int:
        return sum(len(conns) for conns in self.active_connections.values())

    async def connect(self, agent_id: str, websocket: WebSocket, session_id: str = None, user_id: str | None = None):
        if self.total_connections >= MAX_CONNECTIONS:
            await websocket.close(code=1013, reason="Server at capacity")
            logger.warning("[WS] 连接被拒绝：已达最大连接数 ({})", MAX_CONNECTIONS)
            return
        # accept 由调用方（websocket_chat）完成，ConnectionManager 仅负责连接追踪
        _now = time.monotonic()
        conn_key = f"{session_id}@{user_id}" if session_id else str(id(websocket))
        if agent_id not in self.active_connections:
            self.active_connections[agent_id] = {}
        # #108 修复：同一 session 已有连接时替换旧连接
        if conn_key in self.active_connections[agent_id]:
            logger.info("[WS] 同 session 替换旧连接: agent={} session={} user={}", agent_id, session_id, user_id)
        self.active_connections[agent_id][conn_key] = _ConnectionInfo(
            ws=websocket, session_id=session_id or "", user_id=user_id or "",
            connected_at=_now, last_activity=_now,
        )
        if not self._cleanup_started:
            self._cleanup_started = True
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def shutdown(self):
        """取消后台清理循环，优雅关闭。"""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

    def disconnect(self, agent_id: str, websocket: WebSocket):
        if agent_id in self.active_connections:
            for key, info in list(self.active_connections[agent_id].items()):
                if info.ws == websocket:
                    del self.active_connections[agent_id][key]
                    break
            if not self.active_connections[agent_id]:
                del self.active_connections[agent_id]

    def touch(self, agent_id: str, websocket: WebSocket):
        """更新连接的最后活动时间。#98 修复：通过 info 引用 O(1) 更新。"""
        if agent_id in self.active_connections:
            for info in self.active_connections[agent_id].values():
                if info.ws == websocket:
                    info.last_activity = time.monotonic()
                    info.ping_count = 0  # #104 修复：真正活动时重置 ping 计数
                    return

    async def _cleanup_loop(self, max_idle_seconds: float = 300.0, interval: float = 60.0):
        """后台任务：每 60 秒清理空闲连接并发送心跳 ping（#65, #66）。

        #104 修复：区分 ping 成功和真实消息活动；
        连续仅 ping 无消息超过 3 次则主动断连。
        """
        while True:
            await asyncio.sleep(interval)
            _now = time.monotonic()
            stale_agents = []
            for agent_id, conns in list(self.active_connections.items()):
                stale_keys = []
                for key, info in conns.items():
                    if _now - info.last_activity > max_idle_seconds:
                        try:
                            await info.ws.send_json({"type": "ping"})
                            info.last_ping_ts = _now
                            info.ping_count += 1
                            if info.ping_count > 5:
                                logger.info(
                                    "[WS] 半死连接清理: agent={} session={} user={} ping_count={}",
                                    agent_id, info.session_id, info.user_id, info.ping_count,
                                )
                                stale_keys.append(key)
                        except Exception as e:
                            logger.warning(
                                "[WS] 清理死连接: agent={} session={} error={}: {}",
                                agent_id, info.session_id, type(e).__name__, str(e),
                            )
                            stale_keys.append(key)
                for key in stale_keys:
                    del conns[key]
                if not conns:
                    stale_agents.append(agent_id)
            for aid in stale_agents:
                del self.active_connections[aid]
            if stale_agents:
                logger.info("[WS] 清理完成：移除 {} 个无活跃连接的 agent", len(stale_agents))

    async def send_message(self, agent_id: str, message: dict):
        """向 Agent 的所有连接广播消息。

        #133 修复：区分连接已关闭（正常断连）和真正的发送错误，
        避免静默吞掉有用的异常信息。
        """
        if agent_id in self.active_connections:
            for info in self.active_connections[agent_id].values():
                try:
                    await info.ws.send_json(message)
                except WebSocketDisconnect:
                    # 客户端已断开连接，属于正常事件，debug 级别记录即可
                    logger.debug(
                        "[WS] send_message 跳过已断开连接: agent={} session={} user={}",
                        agent_id, info.session_id, info.user_id,
                    )
                except Exception:
                    # 其他异常（如网络错误、序列化失败）需要记录以便排查
                    logger.warning(
                        "[WS] send_message 发送失败: agent={} session={} user={} msg_type={}",
                        agent_id, info.session_id, info.user_id, message.get("type", "unknown"),
                    )

    async def send_to_session(self, agent_id: str, session_id: str, message: dict):
        """发送消息到指定 session。#108 修复：仅发第一条匹配连接，避免重复投递。

        #133 修复：区分连接已关闭和真正的发送错误。
        """
        if agent_id in self.active_connections:
            for info in self.active_connections[agent_id].values():
                if info.session_id == session_id:
                    try:
                        await info.ws.send_json(message)
                    except WebSocketDisconnect:
                        # 客户端已断开连接，属于正常事件
                        logger.debug(
                            "[WS] send_to_session 跳过已断开连接: agent={} session={} user={}",
                            agent_id, session_id, info.user_id,
                        )
                    except Exception:
                        # 其他异常（如网络错误、序列化失败）需要记录以便排查
                        logger.warning(
                            "[WS] send_to_session 发送失败: agent={} session={} user={} msg_type={}",
                            agent_id, session_id, info.user_id, message.get("type", "unknown"),
                        )
                    return

    async def send_to_user(self, agent_id: str, user_id: str, message: dict):
        """向指定 user_id 的所有连接发送消息。

        #133 修复：区分连接已关闭和真正的发送错误。
        """
        if agent_id in self.active_connections:
            for info in self.active_connections[agent_id].values():
                if info.user_id == user_id:
                    try:
                        await info.ws.send_json(message)
                    except WebSocketDisconnect:
                        # 客户端已断开连接，属于正常事件
                        logger.debug(
                            "[WS] send_to_user 跳过已断开连接: agent={} session={} user={}",
                            agent_id, info.session_id, user_id,
                        )
                    except Exception:
                        # 其他异常（如网络错误、序列化失败）需要记录以便排查
                        logger.warning(
                            "[WS] send_to_user 发送失败: agent={} session={} user={} msg_type={}",
                            agent_id, info.session_id, user_id, message.get("type", "unknown"),
                        )

    def get_active_session_ids(self, agent_id: str) -> list[str]:
        if agent_id not in self.active_connections:
            return []
        return list(set(info.session_id for info in self.active_connections[agent_id].values() if info.session_id))

    def is_user_viewing_session(self, agent_id: str, session_id: str, user_id: str) -> bool:
        if agent_id not in self.active_connections:
            return False
        for info in self.active_connections[agent_id].values():
            if info.session_id == session_id and info.user_id == user_id:
                return True
        return False


# 全局单例
manager = ConnectionManager()
