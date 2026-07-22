"""FeishuStreamBridge — LLM on_chunk 回调 ↔ channel.stream() producer 桥接器。

使用有界 asyncio.Queue + 丢帧策略连接 LLM token 流和 Feishu CardKit 流式卡片。
SDK 内置 Throttle(min_ms=100, min_chars=50) + UpdateQueue + 内容哈希去重，
producer 直接调 stream.append() 即可。

ContextVar 模式（与 ACP handler 一致）：LLM 流式回调通过 current_bridge
注入 token，无需改动 Runtime 管道。
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from lark_channel import FeishuChannel

# 活跃的流式桥接器引用，LLM on_chunk 回调通过此 ContextVar 注入 token
current_bridge: ContextVar["FeishuStreamBridge | None"] = ContextVar("feishu_bridge", default=None)

# ContextVar 在 Runtime 后台 worker 中丢失——改用全局 dict 按 agent_id 查找
_active_bridges_by_agent: dict[str, "FeishuStreamBridge"] = {}

def get_active_bridge(agent_id: str) -> "FeishuStreamBridge | None":
    return _active_bridges_by_agent.get(agent_id)

def set_active_bridge(agent_id: str, bridge: "FeishuStreamBridge") -> None:
    # 取消旧 bridge 防止跨聊天流式泄漏
    old = _active_bridges_by_agent.get(agent_id)
    if old is not None and old is not bridge:
        asyncio.ensure_future(old.cancel())
    _active_bridges_by_agent[agent_id] = bridge

def clear_active_bridge(agent_id: str) -> None:
    _active_bridges_by_agent.pop(agent_id, None)

_END = object()  # 哨兵值 — 不用 None，避免类型混淆


class FeishuStreamBridge:
    """连接 LLM on_chunk 回调和 channel.stream() producer 的桥接器。

    使用有界队列 + 丢帧策略：LLM token 产生速度远快于 CardKit API 吞吐量，
    卡片是视觉展示，丢中间帧不影响最终结果。
    """

    def __init__(self):
        # 有界队列 — 防止 LLM 背压下 OOM
        self._queue: asyncio.Queue[str | object] = asyncio.Queue(maxsize=32)
        self._dropped = 0
        self._stream_task: asyncio.Task | None = None
        self._completed = False

    async def feed_token(self, token: str) -> None:
        """LLM 每生成一个 token 时调用。队列满时丢帧，永不阻塞 LLM pipeline。"""
        if self._completed:
            return  # 借用 6: Terminal Guard
        try:
            self._queue.put_nowait(token)
        except asyncio.QueueFull:
            self._dropped += 1

    async def feed_tool_event(self, kind: str, name: str) -> None:
        """工具调用/完成事件。在卡片中显示进度。"""
        if self._completed:
            return
        icon = "🔧" if kind == "call" else "✅"
        self.feed_token(f"\n> {icon} {name}\n")

    async def feed_complete(self) -> None:
        """LLM 生成完毕时调用（无论成败，必须放在 finally 块中调用）。"""
        if self._completed:
            return
        self._completed = True
        await self._queue.put(_END)

    async def cancel(self) -> None:
        """取消流式输出。SDK 的 channel.stream() 在 producer 退出时自动 finish_streaming_card。
        CancelledError 直接 propagate，不追加 footer。"""
        try:
            self._queue.put_nowait(_END)
        except asyncio.QueueFull:
            pass
        self._completed = True
        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()

    async def _on_stream_failed(
        self,
        task: asyncio.Task,
        channel: "FeishuChannel",
        chat_id: str,
        opts: dict | None,
        accumulated: str,
    ) -> None:
        """借用 1: 三级降级链 — stream 失败后清理僵尸卡片 + 发送静态卡片/文本。"""
        if not task.cancelled() and (exc := task.exception()):
            logger.error("[Feishu-Stream] 流式卡片异常: {}", exc)

        # 借用 3: 流式失败后 recall_message 清理僵尸卡片
        try:
            msg_id = getattr(task, "_message_id", None)
            if msg_id:
                await channel.recall_message(msg_id)
        except Exception:
            pass

        # 降级到静态消息
        if accumulated:
            try:
                await channel.send(chat_id, {"text": accumulated}, opts)
            except Exception as e:
                logger.error("[Feishu-Stream] 降级发送失败: {}", e)

    async def _producer(self, stream):
        """传给 channel.stream() 的 producer 函数。
        SDK 内置 Throttle + UpdateQueue + 内容哈希去重，直接调 stream.append() 即可。
        """
        # 借用 5: 长回复截断在 15000 字符，防止单卡片超限
        MAX_STREAM_CHARS = 15000
        total_chars = 0

        while True:
            token = await self._queue.get()
            if token is _END:
                break
            if self._completed:
                break  # 借用 6: Terminal Guard — 可能在 queue.get() 等待期间被 cancel
            total_chars += len(token)
            if total_chars > MAX_STREAM_CHARS:
                if self._dropped == 0:
                    await stream.append("\n\n*...(内容过长，已截断)*")
                # drain 剩余 token 直到 _END，避免 feed_complete 死锁
                while True:
                    token = await self._queue.get()
                    if token is _END:
                        break
                break
            await stream.append(token)

    async def start_stream(
        self,
        channel: "FeishuChannel",
        chat_id: str,
        reply_to: str | None = None,
    ) -> asyncio.Task:
        """启动流式卡片，返回 Task。不可重复调用。"""
        if self._stream_task and not self._stream_task.done():
            raise RuntimeError("Stream already in progress")
        opts = {"reply_to": reply_to} if reply_to else None
        self._stream_task = asyncio.create_task(
            channel.stream(chat_id, {"markdown": self._producer}, opts)
        )
        # 关键：add_done_callback 防止 Task 异常静默丢失
        self._stream_task.add_done_callback(self._on_stream_done)
        return self._stream_task

    def _on_stream_done(self, task: asyncio.Task) -> None:
        self._completed = True
        if not task.cancelled() and (exc := task.exception()):
            logger.error("[Feishu-Stream] 流式卡片异常: {}", exc)
            # 异步触发降级清理（recall + 静态消息）
            # channel 引用在闭包中不可用，由调用方 except 块负责降级

    async def wait_complete(self, timeout: float = 30) -> None:
        """等待流式完成。带超时兜底，防止卡片永久 stuck 在 streaming 模式。"""
        if self._stream_task:
            await asyncio.wait_for(self._stream_task, timeout=timeout)
