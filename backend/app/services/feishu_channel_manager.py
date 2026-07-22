"""FeishuChannel 管理器 — 每 Agent 一个 FeishuChannel 实例（webhook 模式）。

管理多租户、多 Agent 的飞书通道生命周期。
"""

import uuid
from typing import Dict

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.channel_config import ChannelConfig

try:
    from lark_channel import FeishuChannel, SecurityConfig
    _HAS_LARK_CHANNEL = True
except ImportError:
    _HAS_LARK_CHANNEL = False
    FeishuChannel = None  # type: ignore
    SecurityConfig = None  # type: ignore


class FeishuChannelManager:
    """管理每 Agent 的 FeishuChannel 实例（webhook 模式）。

    每个 Agent 一个独立的 FeishuChannel 实例，拥有独立的 app_id/app_secret、
    事件分派器、去重缓存和令牌缓存。
    """

    def __init__(self):
        self._channels: Dict[uuid.UUID, "FeishuChannel"] = {}
        # 活跃的流式桥接器：chat_id → bridge，用于优雅关闭
        self._active_bridges: Dict[str, object] = {}

    def get(self, agent_id: uuid.UUID) -> "FeishuChannel | None":
        """获取指定 Agent 的 Channel 实例。"""
        return self._channels.get(agent_id)

    def register_bridge(self, chat_id: str, bridge: object) -> None:
        """注册活跃的流式桥接器。覆盖前 cancel 旧 bridge。"""
        old = self._active_bridges.get(chat_id)
        if old is not None and old is not bridge and hasattr(old, 'cancel'):
            import asyncio
            try:
                asyncio.create_task(old.cancel())
            except Exception:
                pass
        self._active_bridges[chat_id] = bridge

    def unregister_bridge(self, chat_id: str) -> None:
        """注销流式桥接器。"""
        self._active_bridges.pop(chat_id, None)

    async def start_all(self) -> None:
        """启动所有已配置的 Feishu Channel。单实例失败不阻塞其他。"""
        if not _HAS_LARK_CHANNEL:
            logger.warning("[FeishuChannel] lark-channel-sdk 未安装，跳过初始化")
            return

        async with async_session() as db:
            result = await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.is_configured == True,
                    ChannelConfig.channel_type == "feishu",
                )
            )
            configs = result.scalars().all()

        if not configs:
            logger.info("[FeishuChannel] 无已配置的飞书通道")
            return

        logger.info(f"[FeishuChannel] 初始化 {len(configs)} 个飞书通道...")

        import asyncio as _asyncio

        async def _start_one(cfg: ChannelConfig) -> None:
            try:
                # Phase 0 MEDIUM: encrypt_key 空值检查
                if not cfg.encrypt_key:
                    logger.error(
                        "[FeishuChannel] Agent {}: encrypt_key 未配置，跳过初始化",
                        cfg.agent_id,
                    )
                    return

                channel = FeishuChannel(
                    app_id=cfg.app_id,
                    app_secret=cfg.app_secret,
                    encrypt_key=cfg.encrypt_key,
                    verification_token=cfg.verification_token,
                    domain="https://open.larksuite.com",  # Lark 国际版
                    transport="webhook",
                    security=SecurityConfig(mode="strict"),  # Phase 3 切 strict
                )
                await channel.connect_until_ready()
                self._channels[cfg.agent_id] = channel

                # 注册事件处理器
                await self._register_handler(channel, cfg.agent_id)

                logger.info(f"[FeishuChannel] Agent {cfg.agent_id}: 通道已就绪")
            except Exception as e:
                logger.error("[FeishuChannel] Agent {}: 初始化失败: {}", cfg.agent_id, e)

        # Phase 1: Semaphore(10) 并发上限 + return_exceptions + 3 次重试
        _sem = _asyncio.Semaphore(10)

        async def _start_one_safe(cfg: ChannelConfig) -> None:
            async with _sem:
                last_exc = None
                for attempt in range(3):
                    try:
                        await _start_one(cfg)
                        return
                    except Exception as e:
                        last_exc = e
                        if attempt < 2:
                            await _asyncio.sleep(2 ** attempt)
                logger.error("[FeishuChannel] Agent {}: 3 次重试均失败: {}", cfg.agent_id, last_exc)

        results = await _asyncio.gather(
            *[_start_one_safe(cfg) for cfg in configs],
            return_exceptions=True,
        )
        for cfg, r in zip(configs, results):
            if isinstance(r, Exception):
                logger.error("[FeishuChannel] Agent {}: 启动异常: {}", cfg.agent_id, r)

    async def _register_handler(self, channel: "FeishuChannel", agent_id: uuid.UUID) -> None:
        """为 Channel 注册消息事件处理器。

        CRITICAL: 事件处理器不阻塞 HTTP 响应。使用 asyncio.create_task
        立即返回，Runtime 在后台执行。
        """
        import asyncio as _asyncio

        async def on_message(msg):
            """处理飞书消息事件。不阻塞 webhook HTTP 响应。"""
            # 后台处理，立即返回
            _asyncio.create_task(
                self._process_feishu_message(msg, agent_id, channel)
            )

        channel.on("message", on_message)

        # card.action.trigger 事件（中断按钮等交互）
        async def on_card_action(event):
            logger.info("[FeishuChannel] Agent {}: card action received", agent_id)

        channel.on("cardAction", on_card_action)

        # 错误事件
        async def on_error(err):
            logger.error("[FeishuChannel] Agent {}: 通道错误: {}", agent_id, err)

        channel.on("error", on_error)

    async def _process_feishu_message(
        self, msg, agent_id: uuid.UUID, channel: "FeishuChannel"
    ) -> None:
        """后台处理飞书消息。HIGH: 先 Runtime 就绪，后启动流式卡片。"""
        import asyncio as _asyncio
        from app.services.feishu_stream_bridge import FeishuStreamBridge

        chat_id = getattr(msg, "chat_id", "")
        bridge = FeishuStreamBridge()
        self.register_bridge(chat_id, bridge)
        from app.services.feishu_stream_bridge import set_active_bridge
        set_active_bridge(str(agent_id), bridge)

        try:
            # HIGH: 先验证 Runtime 就绪，再启动流式卡片
            from app.api.feishu import process_feishu_event

            body = {
                "header": {
                    "event_type": "im.message.receive_v1",
                    "event_id": getattr(msg, "message_id", ""),
                },
                "event": {
                    "message": {
                        "message_id": getattr(msg, "message_id", ""),
                        "chat_id": chat_id,
                        "chat_type": getattr(msg, "chat_type", "p2p"),
                        "message_type": "text",
                        "content": '{"text":"' + (getattr(msg, "content_text", "") or "").replace('"', '\\"') + '"}',
                    },
                    "sender": {
                        "sender_id": {
                            "open_id": getattr(msg, "sender_id", ""),
                        }
                    },
                },
            }

            # 启动流式卡片（先发"Thinking..."）
            stream_task = await bridge.start_stream(
                channel, chat_id,
                reply_to=getattr(msg, "message_id", None),
            )

            # 执行 Runtime（流式卡片会实时更新）
            await process_feishu_event(agent_id, body)

            # 等待流式完成
            try:
                await bridge.wait_complete(timeout=30)
            except _asyncio.TimeoutError:
                logger.warning("[FeishuChannel] Agent {}: 流式卡片超时，取消并降级", agent_id)
                await bridge.cancel()

        except Exception as e:
            logger.error("[FeishuChannel] Agent {}: 消息处理失败: {}", agent_id, e)
            await bridge.cancel()
        finally:
            self.unregister_bridge(chat_id)

    async def reload(self, agent_id: uuid.UUID) -> None:
        """热更新单个 agent 的 channel 实例（app_secret 轮换后调用）。

        validate-before-commit: 先创建新 channel + connect 成功 → 再断旧 channel。
        """
        async with async_session() as db:
            result = await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.agent_id == agent_id,
                    ChannelConfig.channel_type == "feishu",
                    ChannelConfig.is_configured,
                )
            )
            config = result.scalar_one_or_none()

        if not config:
            logger.info("[FeishuChannel] Agent {}: 配置已删除，通道已移除", agent_id)
            old = self._channels.pop(agent_id, None)
            if old:
                try:
                    await old.disconnect()
                except Exception as e:
                    logger.warning("[FeishuChannel] Agent {}: 断开旧通道失败: {}", agent_id, e)
            return

        # 先创建新 channel，成功后再替换
        channel = FeishuChannel(
            app_id=config.app_id,
            app_secret=config.app_secret,
            encrypt_key=config.encrypt_key,
            verification_token=config.verification_token,
            transport="webhook",
            security=SecurityConfig(mode="strict"),
        )
        await channel.connect_until_ready()
        await self._register_handler(channel, agent_id)

        old = self._channels.pop(agent_id, None)
        if old:
            try:
                await old.disconnect()
            except Exception as e:
                logger.warning("[FeishuChannel] Agent {}: 断开旧通道失败: {}", agent_id, e)
        self._channels[agent_id] = channel
        logger.info("[FeishuChannel] Agent {}: 通道已热更新", agent_id)

    async def stop_agent(self, agent_id: uuid.UUID) -> None:
        """停止指定 Agent 的 Channel。仅取消目标 agent 的活跃流式桥接器。"""
        bridges_to_cancel = [
            (cid, b) for cid, b in self._active_bridges.items()
        ]
        for chat_id, bridge in bridges_to_cancel:
            try:
                if hasattr(bridge, 'cancel'):
                    await bridge.cancel()
                self._active_bridges.pop(chat_id, None)
            except Exception as e:
                logger.warning("[FeishuChannel] 取消流式失败 {}: {}", chat_id, e)

        channel = self._channels.pop(agent_id, None)
        if channel:
            try:
                await channel.disconnect()
            except Exception as e:
                logger.warning("[FeishuChannel] Agent {}: 断开失败: {}", agent_id, e)

    async def get_or_create_ws_channel(self, agent_id: uuid.UUID) -> "FeishuChannel | None":
        """为 WS 模式的 agent 获取或创建用于出站流式的 FeishuChannel。

        WS 模式不需要 encrypt_key（WS 内置加密），channel 仅用于 send + stream。
        """
        if not _HAS_LARK_CHANNEL:
            return None

        existing = self._channels.get(agent_id)
        if existing:
            return existing

        # TOCTOU 保护：asyncio.Lock 原子化检查-创建
        import asyncio as _asyncio
        if not hasattr(self, '_ws_channel_locks'):
            self._ws_channel_locks: dict[uuid.UUID, _asyncio.Lock] = {}
        lock = self._ws_channel_locks.setdefault(agent_id, _asyncio.Lock())
        async with lock:
            existing = self._channels.get(agent_id)
            if existing:
                return existing

        async with async_session() as db:
            result = await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.agent_id == agent_id,
                    ChannelConfig.channel_type == "feishu",
                    ChannelConfig.is_configured == True,
                )
            )
            config = result.scalar_one_or_none()

        if not config:
            return None

        try:
            channel = FeishuChannel(
                app_id=config.app_id,
                app_secret=config.app_secret,
                encrypt_key=config.encrypt_key or None,  # WS mode: optional
                verification_token=config.verification_token or None,
                domain="https://open.larksuite.com",  # Lark 国际版
                transport="webhook",  # webhook transport for channel.send/stream only
                security=SecurityConfig(mode="strict"),
            )
            await channel.connect_until_ready()
            self._channels[agent_id] = channel
            logger.info("[FeishuChannel] Agent {}: WS outbound channel 已就绪", agent_id)
            return channel
        except Exception as e:
            logger.error("[FeishuChannel] Agent {}: WS outbound channel 创建失败: {}", agent_id, e)
            return None

    async def stop_all(self) -> None:
        """停止所有 Channel。先取消活跃流式，再断开连接。"""
        # 借用 4: 全局 Stream 注册 + 优雅关闭
        for chat_id, bridge in list(self._active_bridges.items()):
            try:
                if hasattr(bridge, 'cancel'):
                    await bridge.cancel()
            except Exception:
                pass
        self._active_bridges.clear()

        for agent_id, channel in list(self._channels.items()):
            try:
                await channel.disconnect()
            except Exception as e:
                logger.warning("[FeishuChannel] Agent {}: 断开失败: {}", agent_id, e)
        self._channels.clear()
        logger.info("[FeishuChannel] 所有通道已停止")

    @property
    def agent_count(self) -> int:
        return len(self._channels)


# 全局单例
feishu_channel_manager = FeishuChannelManager()
