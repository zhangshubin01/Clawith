"""Map stable Runtime events back to the existing Web Chat packet contract."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol
import asyncio
import uuid

from sqlalchemy import select

from app.models.audit import ChatMessage
from app.services.agent_runtime.command_worker import RuntimeSessionFactory
from app.services.agent_runtime.contracts import (
    RunHandle,
    RuntimeEvent,
    RuntimeEventCursor,
)
from app.services.agent_runtime.event_stream import DatabaseRuntimeEventStream


ChatStreamStatus = Literal["completed", "failed", "cancelled", "waiting_user"]
PacketSender = Callable[[dict], Awaitable[None]]


class RuntimeEventSource(Protocol):
    def stream_run(
        self,
        handle: RunHandle,
        *,
        after: RuntimeEventCursor | None = None,
    ) -> AsyncIterator[RuntimeEvent]: ...


class ChatRuntimeStreamError(RuntimeError):
    """A stable Runtime event cannot be mapped to the requested Web session."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ChatRuntimeStreamOutcome:
    """The user-visible boundary reached by one stream attachment."""

    status: ChatStreamStatus
    content: str
    cursor: RuntimeEventCursor
    correlation_id: str | None = None


async def _load_delivered_message(
    session_factory: RuntimeSessionFactory,
    *,
    message_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
) -> ChatMessage:
    async with session_factory() as db:
        result = await db.execute(
            select(ChatMessage).where(
                ChatMessage.id == message_id,
                ChatMessage.agent_id == agent_id,
                ChatMessage.user_id == user_id,
                ChatMessage.conversation_id == str(session_id),
            )
        )
        message = result.scalar_one_or_none()
    if message is None or message.role not in {"assistant", "system"}:
        raise ChatRuntimeStreamError(
            "runtime_delivery_message_missing",
            "Runtime delivery receipt does not resolve to this Web Chat session",
        )
    return message


def _cursor(event: RuntimeEvent) -> RuntimeEventCursor:
    if event.created_at is None or event.event_id is None:
        raise ChatRuntimeStreamError(
            "invalid_runtime_event_position",
            "Runtime event has no stable reconnect position",
        )
    return RuntimeEventCursor(event.created_at, event.event_id)


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


# 工具执行结果推送到前端 "代码与执行" 面板的工具集合
_STREAMING_CODE_TOOLS: frozenset[str] = frozenset(
    {"execute_code", "execute_code_e2b", "android_compile"}
)

# 活跃的工具输出流式订阅任务（key = "session_id:agent_id"）
_tool_stream_tasks: dict[str, asyncio.Task] = {}

# 频道中间推送去重（key = "session_id:tool_name:status" -> 上次推送时间）
_channel_push_sent: dict[str, float] = {}

_CHANNEL_PUSH_COOLDOWN = 3.0  # 同状态去重间隔（秒）


async def _subscribe_and_forward(
    stream_key: str,
    send_packet: PacketSender,
) -> None:
    """订阅 Redis pubsub 通道，将工具输出转发为 agentbay_live WebSocket 消息。"""
    from app.services.tool_stream import subscribe_tool_output

    session_id, agent_id = stream_key.split(":", 1)
    try:
        async for output, stream in subscribe_tool_output(session_id, agent_id):
            await send_packet(
                {
                    "type": "agentbay_live",
                    "env": "code",
                    "output": output,
                    "stream": stream,
                }
            )
    except asyncio.CancelledError:
        pass


async def _push_tool_status_to_channel(
    session_factory: RuntimeSessionFactory,
    session_id: uuid.UUID,
    run_id: uuid.UUID,
    tool_name: str,
    tool_status: str,
    result_text: str = "",
) -> None:
    """工具执行状态变更时，推送进度消息到外部频道（飞书等）。

    通过 stage_channel_delivery 加入 outbox，由 ChannelDeliveryWorker 异步发送。
    仅对 source_channel != "web" 的会话生效。
    """
    import time as _time
    from datetime import datetime, timezone

    from app.models.agent_run import AgentRun
    from app.models.chat_session import ChatSession
    from app.services.agent_runtime.channel_delivery import stage_channel_delivery

    # 去重：同会话同工具同状态冷却期内不重复推送
    dedup_key = f"{session_id}:{tool_name}:{tool_status}"
    now = _time.monotonic()
    last = _channel_push_sent.get(dedup_key, 0)
    if now - last < _CHANNEL_PUSH_COOLDOWN:
        return
    _channel_push_sent[dedup_key] = now

    try:
        async with session_factory() as db:
            session = await db.get(ChatSession, session_id)
            if session is None or session.source_channel == "web":
                return

            run = await db.get(AgentRun, run_id)
            if run is None:
                return

            # 构建进度消息
            if tool_status == "running":
                content = f"⚡️ 正在执行 `{tool_name}`..."
            elif tool_status == "done":
                if result_text:
                    content = f"✅ `{tool_name}` 完成\n\n{result_text[:300]}"
                else:
                    content = f"✅ `{tool_name}` 完成"
            else:
                return

            # 保存消息并加入 outbox
            message = ChatMessage(
                id=uuid.uuid4(),
                conversation_id=str(session_id),
                role="assistant",
                content=content,
                created_at=datetime.now(timezone.utc),
            )
            db.add(message)
            stage_channel_delivery(
                db,
                run=run,
                session=session,
                message_id=message.id,
                idempotency_key=f"tool_{run_id}_{tool_name}_{tool_status}",
                clock=lambda: datetime.now(timezone.utc),
            )
            await db.commit()
    except Exception:
        pass  # 频道中间推送失败不应阻断 WebSocket 主流程


async def stream_web_chat_run(
    *,
    handle: RunHandle,
    session_factory: RuntimeSessionFactory,
    send_packet: PacketSender,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    after: RuntimeEventCursor | None = None,
    event_source: RuntimeEventSource | None = None,
) -> ChatRuntimeStreamOutcome:
    """Stream one start/resume attachment until terminal or waiting-user delivery."""
    source = event_source or DatabaseRuntimeEventStream(session_factory=session_factory)
    terminal_status: ChatStreamStatus | None = None
    waiting_correlation_id: str | None = None
    latest_cursor = after

    async for event in source.stream_run(handle, after=after):
        latest_cursor = _cursor(event)
        payload = event.payload

        activity_type = payload.get("activity_type")
        packet_position = {
            "run_id": str(handle.run_id),
            "event_id": str(event.event_id),
            "event_cursor": f"{event.created_at.isoformat()}|{event.event_id}",
        }
        if event.event_type == "status_changed" and activity_type == "thinking":
            content = _text(payload.get("content"))
            if content is not None:
                await send_packet({"type": "thinking", "content": content, **packet_position})
            continue
        if event.event_type == "status_changed" and activity_type == "assistant_progress":
            content = _text(payload.get("content"))
            if content is not None:
                await send_packet({"type": "chunk", "content": content, **packet_position})
            continue
        if event.event_type == "status_changed" and activity_type == "tool_call":
            tool_name = _text(payload.get("name"))
            call_id = _text(payload.get("call_id"))
            tool_status = payload.get("status")
            if tool_name is not None and call_id is not None and tool_status in {"running", "done"}:
                await send_packet(
                    {
                        "type": "tool_call",
                        "name": tool_name,
                        "call_id": call_id,
                        "args": payload.get("args") if isinstance(payload.get("args"), dict) else {},
                        "status": tool_status,
                        "result": str(payload.get("result") or ""),
                        "reasoning_content": str(payload.get("reasoning_content") or ""),
                        "execution_status": payload.get("execution_status"),
                        "error_code": payload.get("error_code"),
                        **packet_position,
                    }
                )
                # 代码执行类工具：通过 Redis pubsub 订阅流式输出，推送到前端 "代码与执行" 面板
                if tool_name in _STREAMING_CODE_TOOLS:
                    stream_key = f"{session_id}:{agent_id}"
                    if tool_status == "running":
                        # 工具开始执行 → 打开面板 + 启动 Redis 订阅
                        await send_packet(
                            {
                                "type": "agentbay_live",
                                "env": "code",
                                "output": f"[{tool_name}] 执行中...\n",
                                "stream": "stdout",
                            }
                        )
                        # 启动后台任务订阅 Redis pubsub 流式输出
                        _stream_task = asyncio.create_task(
                            _subscribe_and_forward(stream_key, send_packet)
                        )
                        _tool_stream_tasks[stream_key] = _stream_task
                    elif tool_status == "done":
                        # 工具完成 → 取消 Redis 订阅 + 推送最终输出
                        _task = _tool_stream_tasks.pop(stream_key, None)
                        if _task and not _task.done():
                            _task.cancel()
                            try:
                                await _task
                            except asyncio.CancelledError:
                                pass
                        result_text = str(payload.get("result") or "")
                        if result_text:
                            await send_packet(
                                {
                                    "type": "agentbay_live",
                                    "env": "code",
                                    "output": f"\n--- 构建完成 ---\n{result_text}",
                                    "stream": "stdout",
                                }
                            )
                    # 推送工具执行进度到外部频道（飞书等），不阻塞 WebSocket 主流程
                    asyncio.create_task(
                        _push_tool_status_to_channel(
                            session_factory,
                            session_id,
                            handle.run_id,
                            tool_name,
                            tool_status,
                            str(payload.get("result") or ""),
                        )
                    )
            continue

        if event.event_type == "waiting_started" and payload.get("waiting_type") == "user":
            waiting_correlation_id = _text(payload.get("correlation_id"))
            if waiting_correlation_id is None:
                raise ChatRuntimeStreamError(
                    "runtime_wait_correlation_missing",
                    "waiting_user Runtime event has no resume correlation",
                )
            terminal_status = "waiting_user"
        elif event.event_type == "resumed":
            waiting_correlation_id = None
            terminal_status = None
        elif event.event_type == "run_completed":
            terminal_status = "completed"
        elif event.event_type == "run_failed":
            terminal_status = "failed"
        elif event.event_type == "run_cancelled":
            terminal_status = "cancelled"

        if event.event_type not in {"delivery_succeeded", "delivery_failed"}:
            await send_packet(
                {
                    "type": "runtime_status",
                    "run_id": str(handle.run_id),
                    "event": event.event_type,
                    "status": payload.get("status"),
                }
            )
            continue

        delivery_kind = payload.get("delivery_kind")
        if delivery_kind not in {"waiting", "terminal"}:
            continue
        if latest_cursor is None:
            raise ChatRuntimeStreamError(
                "invalid_runtime_event_position",
                "Runtime delivery has no reconnect position",
            )

        receipt_status = payload.get("lifecycle_status")
        if receipt_status not in {None, "waiting_user", "completed", "failed", "cancelled"}:
            raise ChatRuntimeStreamError(
                "invalid_runtime_delivery_receipt",
                "Runtime delivery receipt has an invalid lifecycle status",
            )
        status = terminal_status or receipt_status
        if delivery_kind == "waiting":
            status = "waiting_user"
            waiting_correlation_id = waiting_correlation_id or _text(
                payload.get("correlation_id")
            )
            if waiting_correlation_id is None:
                raise ChatRuntimeStreamError(
                    "runtime_wait_correlation_missing",
                    "waiting_user delivery has no resume correlation",
                )
        if status is None:
            raise ChatRuntimeStreamError(
                "runtime_delivery_without_lifecycle",
                "Runtime delivery arrived without its lifecycle event",
            )

        if event.event_type == "delivery_failed":
            content = "Runtime result could not be delivered to this chat."
            await send_packet(
                {
                    "type": "done",
                    "role": "assistant",
                    "content": content,
                    "run_id": str(handle.run_id),
                    "runtime_status": status,
                    "delivery_error": payload.get("error_code"),
                }
            )
            return ChatRuntimeStreamOutcome(
                status=status,
                content=content,
                cursor=latest_cursor,
                correlation_id=waiting_correlation_id,
            )

        raw_message_id = payload.get("message_id")
        try:
            message_id = uuid.UUID(str(raw_message_id))
        except (TypeError, ValueError) as exc:
            raise ChatRuntimeStreamError(
                "invalid_runtime_delivery_receipt",
                "Runtime delivery receipt has no valid message ID",
            ) from exc
        message = await _load_delivered_message(
            session_factory,
            message_id=message_id,
            agent_id=agent_id,
            session_id=session_id,
            user_id=user_id,
        )
        packet = {
            "type": "done",
            "role": "assistant",
            "content": message.content,
            "message_id": str(message.id),
            "run_id": str(handle.run_id),
            "runtime_status": status,
        }
        if waiting_correlation_id is not None:
            packet["correlation_id"] = waiting_correlation_id
        await send_packet(packet)
        return ChatRuntimeStreamOutcome(
            status=status,
            content=message.content,
            cursor=latest_cursor,
            correlation_id=waiting_correlation_id,
        )

    raise ChatRuntimeStreamError(
        "runtime_stream_ended_without_delivery",
        "Runtime event stream ended before a Web Chat delivery boundary",
    )


__all__ = [
    "ChatRuntimeStreamError",
    "ChatRuntimeStreamOutcome",
    "RuntimeEventSource",
    "stream_web_chat_run",
]
