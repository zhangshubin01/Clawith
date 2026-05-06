"""
流式输出缓冲管理器

按"完整行"发送文本；Markdown 表格按整块发送，避免被拆成半截列。

原逻辑嵌入在 jsonrpc_router.JSONRPCRouter._handle_chat_ask() 中，
抽取为独立类以提高可测试性和可维护性。
"""

import asyncio
import re
from typing import Callable, Awaitable

from loguru import logger

# 非表格文本累计阈值（字符数），降低阈值以减少流式卡顿感知
BUFFER_THRESHOLD = 80
# 无换行符时的强制 flush 间隔（秒），防止长时间无换行导致前端无输出
FORCE_FLUSH_INTERVAL = 0.2

_TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|\s*[:\-\| ]+\|\s*$")
_CODE_FENCE_RE = re.compile(r"^\s*`{3,}[^`]*$|^\s*~{3,}[^~]*$")


class StreamBufferManager:
    """管理流式文本的缓冲、表格完整性检测和批量发送。"""

    def __init__(self, send_fn: Callable[[str], Awaitable[None]], buffer_threshold: int = BUFFER_THRESHOLD):
        self._send_fn = send_fn
        self._buffer_threshold = buffer_threshold
        self._line_buffer = ""
        self._ready_segments: list[str] = []
        self._table_lines: list[str] = []
        self._table_mode = False
        self._code_fence_mode = False
        self._flush_timer_task: asyncio.Task | None = None

    @staticmethod
    def _is_table_line(line: str) -> bool:
        return bool(_TABLE_LINE_RE.match(line))

    @staticmethod
    def _is_code_fence_line(line: str) -> bool:
        return bool(_CODE_FENCE_RE.match(line))

    @staticmethod
    def _is_valid_table_block(lines: list[str]) -> bool:
        return len(lines) >= 2 and any(_TABLE_SEPARATOR_RE.match(ln) for ln in lines[1:])

    def _drain_ready_text(self, max_len_only: bool = False) -> str:
        if not self._ready_segments:
            return ""
        if max_len_only:
            acc = []
            total = 0
            while self._ready_segments and total < self._buffer_threshold:
                seg = self._ready_segments[0]
                if total > 0 and total + len(seg) > self._buffer_threshold:
                    break
                acc.append(self._ready_segments.pop(0))
                total += len(seg)
            return "".join(acc)
        text = "".join(self._ready_segments)
        self._ready_segments.clear()
        return text

    def _flush_table_lines(self) -> None:
        if not self._table_lines:
            return
        if self._is_valid_table_block(self._table_lines):
            self._ready_segments.append("".join(f"{ln}\n" for ln in self._table_lines))
        else:
            for ln in self._table_lines:
                self._ready_segments.append(f"{ln}\n")
        self._table_lines = []

    def _cancel_timer(self) -> None:
        if self._flush_timer_task and not self._flush_timer_task.done():
            self._flush_timer_task.cancel()
        self._flush_timer_task = None

    async def _force_flush_after(self, delay: float) -> None:
        """定时器：超过 delay 秒无换行符时强制发送缓冲内容。"""
        try:
            await asyncio.sleep(delay)
            await self.flush(force=False)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def flush(self, force: bool = False) -> None:
        """发送缓冲区中的就绪文本。

        Args:
            force: True 时强制清空所有缓冲（如流式结束时），包括未换行的残片。
        """
        self._cancel_timer()
        if force:
            if self._line_buffer:
                if self._table_mode:
                    self._table_lines.append(self._line_buffer)
                    self._flush_table_lines()
                    self._table_mode = False
                else:
                    self._ready_segments.append(self._line_buffer)
                self._line_buffer = ""
            if self._table_mode:
                self._flush_table_lines()
                self._table_mode = False
            payload = self._drain_ready_text(max_len_only=False)
            if payload:
                await self._send_fn(payload)
                logger.debug("[StreamBuffer] flushed (force): text_len=%d", len(payload))
            return

        # O(n) 一次性分割所有完整行，避免逐行 find+slice 导致的 O(n²) 复杂度
        if "\n" not in self._line_buffer:
            pass
        else:
            *complete_lines, self._line_buffer = self._line_buffer.split("\n")
            for line in complete_lines:
                if self._is_code_fence_line(line):
                    if self._table_mode:
                        self._flush_table_lines()
                        self._table_mode = False
                    self._code_fence_mode = not self._code_fence_mode
                    self._ready_segments.append(f"{line}\n")
                    continue

                if self._code_fence_mode:
                    self._ready_segments.append(f"{line}\n")
                    continue

                if self._table_mode:
                    if self._is_table_line(line):
                        self._table_lines.append(line)
                        continue
                    self._flush_table_lines()
                    self._table_mode = False
                    if line == "":
                        self._ready_segments.append("\n")
                    elif self._is_table_line(line):
                        self._table_mode = True
                        self._table_lines = [line]
                    else:
                        self._ready_segments.append(f"{line}\n")
                else:
                    if self._is_table_line(line):
                        self._table_mode = True
                        self._table_lines = [line]
                    else:
                        self._ready_segments.append(f"{line}\n")

        payload = self._drain_ready_text(max_len_only=True)
        if payload:
            await self._send_fn(payload)
            logger.debug("[StreamBuffer] flushed: text_len=%d", len(payload))

    def feed(self, text: str) -> bool:
        """将新文本块追加到行缓冲区。

        Returns:
            True 需要调用 flush(), False 不需要。
        """
        self._line_buffer += text
        needs_flush = "\n" in text or len(self._line_buffer) >= self._buffer_threshold

        # 无换行符且未达阈值时启动 200ms 强制 flush 定时器
        if text and not needs_flush and self._flush_timer_task is None:
            self._flush_timer_task = asyncio.create_task(self._force_flush_after(FORCE_FLUSH_INTERVAL))

        return needs_flush

    @property
    def pending_length(self) -> int:
        """返回未刷新文本的预估长度。"""
        return len(self._line_buffer) + sum(len(s) for s in self._ready_segments)
