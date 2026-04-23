"""Feishu WebSocket Long Connection Manager."""

import asyncio
import json
from typing import Any, Dict
import uuid

from loguru import logger
try:
    import lark_oapi as lark
    import lark_oapi.ws as ws
    _HAS_LARK = True
except ImportError:
    lark = None  # type: ignore
    ws = None    # type: ignore
    _HAS_LARK = False

if _HAS_LARK:
    try:
        import websockets as _websockets
        # Keep a reference to the original connect so we can restore it if needed.
        _orig_websockets_connect = _websockets.connect
        _PROXY_PATCH_AVAILABLE = True
    except ImportError:
        _PROXY_PATCH_AVAILABLE = False
else:
    _PROXY_PATCH_AVAILABLE = False


def _make_no_proxy_connect(orig_connect):
    """Return a drop-in replacement for websockets.connect that forces proxy=None.

    This is intentionally NOT applied at module import time to avoid polluting
    the global websockets namespace for other modules in the process.  Instead
    it is applied as a scoped context manager around lark-oapi's _connect() call.
    """
    import contextlib

    class _NoProxyConnect:
        """Wraps websockets.connect to inject proxy=None, preventing macOS
        system-proxy interference with long-lived SSE / WebSocket connections."""

        def __init__(self, *args, **kwargs):
            kwargs.setdefault("proxy", None)
            self._coro = orig_connect(*args, **kwargs)
            self._ws = None

        def __await__(self):
            return self._coro.__await__()

        async def __aenter__(self):
            self._ws = await self._coro
            return self._ws

        async def __aexit__(self, *exc):
            if self._ws:
                await self._ws.close()

    @contextlib.asynccontextmanager
    async def _scoped_no_proxy():
        """Context manager that temporarily replaces websockets.connect for
        the duration of the lark-oapi connection handshake only."""
        if not _PROXY_PATCH_AVAILABLE:
            yield
            return
        old = _websockets.connect
        _websockets.connect = _NoProxyConnect
        logger.debug("[Feishu WS] Scoped websockets proxy bypass: active")
        try:
            yield
        finally:
            _websockets.connect = old
            logger.debug("[Feishu WS] Scoped websockets proxy bypass: restored")

    return _scoped_no_proxy

from app.database import async_session
from app.models.channel_config import ChannelConfig
from sqlalchemy import select


if not _HAS_LARK:
    logger.warning(
        "[Feishu WS] lark-oapi package not installed. "
        "Feishu WebSocket features will be disabled. "
        "Install with: pip install lark-oapi"
    )


class FeishuWSManager:
    """Manages Feishu WebSocket clients for all agents."""

    def __init__(self):
        self._clients: Dict[uuid.UUID, ws.Client] = {}
        # Tasks for reconnection or ping loops if we want to cancel them later
        self._tasks: Dict[uuid.UUID, asyncio.Task] = {}

    def _create_event_handler(self, agent_id: uuid.UUID) -> lark.EventDispatcherHandler:
        """Create an event dispatcher for a specific agent."""

        def handle_message(data: Any) -> None:
            """Handle im.message.receive_v1 events from Feishu WebSocket."""
            try:
                # The data object carries the raw event body
                logger.debug(f"[Feishu WS] ====> EVENT RECEIVED! entering handle_message for agent {agent_id}")
                raw_body = getattr(data, "raw_body", None)
                logger.debug(f"[Feishu WS] Received event: type={type(data)}, data={data}")
                if not raw_body:
                    # Some SDK versions pass the dict directly
                    if isinstance(data, dict):
                        body_dict = data
                    else:
                        # Handle lark_oapi.event.custom.CustomizedEvent
                        body_dict = {}
                        if hasattr(data, "header"):
                            header_obj = data.header
                            body_dict["header"] = vars(header_obj) if hasattr(header_obj, "__dict__") else {
                                "event_type": getattr(header_obj, "event_type", "im.message.receive_v1"),
                                "event_id": getattr(header_obj, "event_id", ""),
                                "create_time": getattr(header_obj, "create_time", "")
                            }
                            # Ensure event_type is present as it's required downstream
                            if "event_type" not in body_dict["header"]:
                                body_dict["header"]["event_type"] = getattr(header_obj, "event_type", "im.message.receive_v1")
                        else:
                            body_dict["header"] = {"event_type": "im.message.receive_v1"}

                        if hasattr(data, "event"):
                            body_dict["event"] = data.event
                        elif hasattr(data, "content") and isinstance(getattr(data, "content"), str):
                            try:
                                body_dict["event"] = json.loads(data.content)
                            except json.JSONDecodeError:
                                body_dict["event"] = {"content": data.content}
                        
                        if not hasattr(data, "header") and not hasattr(data, "event"):
                            logger.warning(f"[Feishu WS] Unexpected event data type with no recognizable fields: {type(data)}")
                            return
                else:
                    body_dict = json.loads(raw_body.decode("utf-8"))

                loop = asyncio.get_running_loop()
                loop.create_task(self._async_handle_message(agent_id, data))
            except RuntimeError:
                try:
                    # If no running loop in this thread, try to find the main event loop
                    # This is a heuristic and might need adjustment depending on the exact async framework setup
                    main_loop = [t for t in asyncio.all_tasks() if t.get_name() != "feishu-ws"][0].get_loop()
                    asyncio.run_coroutine_threadsafe(self._async_handle_message(agent_id, data), main_loop)
                except Exception as e:
                    logger.exception(f"[Feishu WS] Could not dispatch event to main loop: {e}")

        # For lark-oapi 1.5.x:
        # - The SDK generates specific registration methods for all known events
        # - im.message.receive_v1 becomes register_p2_im_message_receive_v1
        # - Also register bot enter event to avoid 'processor not found' warnings
        from lark_oapi import EventDispatcherHandler
        builder = EventDispatcherHandler.builder("", "")
        
        # 1. Register the main message receive event (required for getting messages
        if hasattr(builder, 'register_p2_im_message_receive_v1'):
            builder = getattr(builder, 'register_p2_im_message_receive_v1')(handle_message)
            logger.info("[Feishu WS] Used specific register_p2_im_message_receive_v1 method")
        elif hasattr(builder, 'register_p2_customized_event'):
            builder = builder.register_p2_customized_event("im.message.receive_v1", handle_message)
            logger.info("[Feishu WS] Fallback to register_p2_customized_event for im.message.receive_v1")
        else:
            logger.error("[Feishu WS] No available registration method found for im.message.receive_v1!")
        
        # 2. Also register the bot enter p2p chat event to avoid warning logs
        if hasattr(builder, 'register_p2_im_chat_access_event_bot_p2p_chat_entered_v1'):
            builder = getattr(builder, 'register_p2_im_chat_access_event_bot_p2p_chat_entered_v1')(handle_message)
            logger.info("[Feishu WS] Registered bot p2p chat enter event")
        
        dispatcher = builder.build()
        return dispatcher

    def _convert_sdk_object_to_dict(self, obj: Any) -> Any:
        """Recursively convert lark-oapi SDK objects to dictionaries for downstream processing."""
        if hasattr(obj, "__dict__") and not isinstance(obj, dict):
            # Convert SDK objects (strongly typed from codegen) to dict
            result = {}
            for k, v in vars(obj).items():
                if not k.startswith("_"):  # skip private attributes
                    result[k] = self._convert_sdk_object_to_dict(v)
            return result
        elif isinstance(obj, list):
            return [self._convert_sdk_object_to_dict(item) for item in obj]
        elif isinstance(obj, dict):
            return {k: self._convert_sdk_object_to_dict(v) for k, v in obj.items()}
        else:
            return obj

    async def _async_handle_message(self, agent_id: uuid.UUID, data: Any) -> None:
        """Handle im.message.receive_v1 events from Feishu WebSocket asynchronously."""
        try:
            # The data object carries the raw event body
            raw_body = getattr(data, "raw_body", None)
            if not raw_body:
                # Some SDK versions pass the dict directly
                if isinstance(data, dict):
                    body_dict = data
                else:
                    # Handle lark_oapi.event.customized.CustomizedEvent or generated event objects
                    body_dict = {}
                    if hasattr(data, "header"):
                        body_dict["header"] = self._convert_sdk_object_to_dict(data.header)
                        # Ensure event_type is present as it's required downstream
                        if "event_type" not in body_dict["header"]:
                            body_dict["header"]["event_type"] = getattr(data.header, "event_type", "im.message.receive_v1")
                    else:
                        body_dict["header"] = {"event_type": "im.message.receive_v1"}

                    if hasattr(data, "event"):
                        # For strongly typed event objects from SDK codegen,
                        # recursively convert all nested objects to dicts
                        body_dict["event"] = self._convert_sdk_object_to_dict(data.event)
                    elif hasattr(data, "content") and isinstance(getattr(data, "content"), str):
                        try:
                            body_dict["event"] = json.loads(data.content)
                        except json.JSONDecodeError:
                            body_dict["event"] = {"content": data.content}
                    
                    if not hasattr(data, "header") and not hasattr(data, "event"):
                        logger.warning(f"[Feishu WS] Unexpected event data type with no recognizable fields: {type(data)}")
                        return
            else:
                body_dict = json.loads(raw_body.decode("utf-8"))

            event_type = body_dict.get("header", {}).get("event_type", "unknown")
            logger.info(f"[Feishu WS] Event received for agent {agent_id}: {event_type}")

            # Import here to avoid circular dependencies
            from app.api.feishu import process_feishu_event

            async with async_session() as db:
                await process_feishu_event(agent_id, body_dict, db)

        except Exception as e:
            logger.exception(f"[Feishu WS] Error processing event for {agent_id}: {e}")

    async def start_client(
        self,
        agent_id: uuid.UUID,
        app_id: str,
        app_secret: str,
        stop_existing: bool = True,
        domain: str = "feishu",
    ):
        """Spawns a WebSocket client fully asynchronously inside FastAPI's loop."""
        if not _HAS_LARK:
            logger.warning("[Feishu WS] lark-oapi not installed, cannot start client")
            return
        if not app_id or not app_secret:
            logger.warning(f"[Feishu WS] Missing app_id or app_secret for {agent_id}, skipping")
            return

        logger.info(f"[Feishu WS] Starting async WS client for agent {agent_id} (App ID: {app_id})")

        # Stop existing client task if any
        if stop_existing and agent_id in self._tasks:
            old_task = self._tasks.pop(agent_id, None)
            if old_task and not old_task.done():
                old_task.cancel()
                logger.info(f"[Feishu WS] Cancelled old WS task for {agent_id}")

        try:
            event_handler = self._create_event_handler(agent_id)
        except Exception as e:
            logger.exception(f"[Feishu WS] Failed to create event handler for {agent_id}: {e}")
            return

        # Instantiate Client - for lark-oapi 1.5.x, domain is not a constructor parameter at this version
        # Default domain is feishu (china), which is what we need
        client = ws.Client(
            app_id,
            app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,  # 恢复 INFO 级别以便排查连接问题
        )
        self._clients[agent_id] = client

        # Build scoped proxy bypass: active only during _connect() to avoid
        # permanently replacing websockets.connect for the whole process.
        _no_proxy_ctx = (
            _make_no_proxy_connect(_orig_websockets_connect)
            if _PROXY_PATCH_AVAILABLE
            else None
        )

        # Direct Async runner bypassing the faulty client.start()
        async def _run_async_client():
            nonlocal client
            retry_count = 0
            max_retries = float('inf')  # 无限重试
            base_retry_delay = 5  # 基础延迟5秒
            max_retry_delay = 300  # 最大延迟5分钟
            ping_task: asyncio.Task | None = None
            while retry_count < max_retries:
                try:
                    # Wrap _connect() in the scoped proxy bypass so macOS system proxy
                    # settings cannot interfere with the WebSocket handshake.
                    if _no_proxy_ctx:
                        async with _no_proxy_ctx():
                            await client._connect()
                    else:
                        await client._connect()
                    logger.info(f"[Feishu WS] Connected for agent {agent_id}")
                    logger.info(f"[Feishu WS] Connection established with event_handler: type={type(event_handler)}, id={id(event_handler)}")
                    # Start ping loop natively after connection is established
                    ping_task = asyncio.create_task(client._ping_loop())

                    # Keep this task alive so it doesn't get canceled, and handle reconnections
                    while True:
                        await asyncio.sleep(3600)  # Keep-alive
                except asyncio.CancelledError:
                    logger.info(f"[Feishu WS] Async client task cancelled for {agent_id}")
                    if ping_task and not ping_task.done():
                        ping_task.cancel()
                        try:
                            await ping_task
                        except asyncio.CancelledError:
                            pass
                    await client._disconnect()
                    return
                except Exception as e:
                    error_str = str(e)
                    if "python-socks is required to use a SOCKS proxy" in error_str:
                        logger.error(f"[Feishu WS] Connection failed for {agent_id}: {error_str}")
                        logger.error("[Feishu WS] To use SOCKS proxy with Feishu WebSocket, please install python-socks: pip install python-socks[socksio]")
                        if ping_task and not ping_task.done():
                            ping_task.cancel()
                            try:
                                await ping_task
                            except asyncio.CancelledError:
                                pass
                        await client._disconnect()
                        self._clients.pop(agent_id, None)
                        return  # Don't retry if python-socks is missing

                    retry_count += 1
                    logger.exception(f"[Feishu WS] Async client exception for {agent_id}: {e} (retry {retry_count}/{max_retries})")
                    if ping_task and not ping_task.done():
                        ping_task.cancel()
                        try:
                            await ping_task
                        except asyncio.CancelledError:
                            pass
                    ping_task = None
                    await client._disconnect()
                    # 指数退避计算延迟时间
                    current_delay = min(base_retry_delay * (2 ** retry_count), max_retry_delay)
                    logger.info(f"[Feishu WS] Trying to reconnect in {current_delay} seconds... (retry {retry_count})")
                    logger.debug(f"[Feishu WS] Current event_handler type: {type(event_handler)}, id: {id(event_handler)}")
                    await asyncio.sleep(current_delay)
                    # Re-create the client for next retry
                    try:
                        logger.info(f"[Feishu WS] Recreating event handler and client for agent {agent_id}...")
                        event_handler = self._create_event_handler(agent_id)
                        logger.info(f"[Feishu WS] New event_handler created: type={type(event_handler)}, id: {id(event_handler)}")
                        client = ws.Client(
                            app_id,
                            app_secret,
                            event_handler=event_handler,
                            log_level=lark.LogLevel.INFO,
                        )
                        self._clients[agent_id] = client
                        logger.info(f"[Feishu WS] New client created and registered for agent {agent_id}")
                    except Exception as create_err:
                        logger.exception(f"[Feishu WS] Failed to recreate client for {agent_id}: {create_err}")
                        retry_count = max_retries
                        break

            if retry_count >= max_retries:
                logger.error(f"[Feishu WS] Max retries ({max_retries}) exceeded for {agent_id}, stopping reconnections")
                if ping_task and not ping_task.done():
                    ping_task.cancel()
                    try:
                        await ping_task
                    except asyncio.CancelledError:
                        pass
                await client._disconnect()
                self._clients.pop(agent_id, None)

        task = asyncio.create_task(_run_async_client(), name=f"feishu-ws-async-{str(agent_id)[:8]}")
        self._tasks[agent_id] = task
        logger.info(f"[Feishu WS] Async WS task scheduled for agent {agent_id}")

    async def stop_client(self, agent_id: uuid.UUID):
        """Stops an actively running WebSocket client for an agent."""
        if agent_id in self._tasks:
            task = self._tasks.pop(agent_id)
            if not task.done():
                task.cancel()
                logger.info(f"[Feishu WS] Stopped client task for agent {agent_id}")
        if agent_id in self._clients:
            client = self._clients.pop(agent_id)
            try:
                await client._disconnect()
            except Exception as e:
                logger.error(f"[Feishu WS] Error disconnecting client for {agent_id}: {e}")

    async def start_all(self):
        """Start WS clients for all configured Feishu agents."""
        if not _HAS_LARK:
            logger.info("[Feishu WS] lark-oapi not installed, skipping Feishu WS initialization")
            return
        logger.info("[Feishu WS] Initializing all active Feishu channels...")
        async with async_session() as db:
            result = await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.is_configured.is_(True),
                    ChannelConfig.channel_type == "feishu",
                )
            )
            configs = result.scalars().all()

        for config in configs:
            extra = config.extra_config or {}
            mode = extra.get("connection_mode", "webhook")
            domain = extra.get("domain", "feishu")
            if mode == "websocket":
                if config.app_id and config.app_secret:
                    await self.start_client(
                        config.agent_id, config.app_id, config.app_secret, 
                        stop_existing=True, domain=domain
                    )
                else:
                    logger.warning(f"[Feishu WS] Skipping agent {config.agent_id}: missing credentials")

    def status(self) -> dict:
        """Return status of all active WS tasks."""
        return {
            str(aid): not self._tasks[aid].done()
            for aid in self._tasks
        }


feishu_ws_manager = FeishuWSManager()
