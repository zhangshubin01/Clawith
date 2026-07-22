"""FeishuRichCardController — 富流式卡片控制器。

参考 deepthink 的 buildStreamingAgentCard + StreamingCardController 实现。
使用低层 CardKit API 替代 SDK 的 channel.stream()，支持：
- rich header（模型名 + 状态 text_tag）
- status banner（当前阶段 + 耗时）
- 可折叠面板（thinking/tools/tasks）
- main_content 打字机效果
- footer note
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from lark_channel import FeishuChannel


# ── Element IDs（deepthink 兼容）──
EID = {
    "STATUS_BANNER": "status_banner",
    "MAIN_CONTENT": "main_content",
    "FOOTER_NOTE": "footer_note",
}

# ── Streaming config（deepthink 兼容）──
STREAMING_CONFIG = {
    "print_frequency_ms": {"default": 50},
    "print_step": {"default": 2},
    "print_strategy": "fast",
}


def _build_rich_streaming_card(initial_text: str = "Thinking...") -> dict:
    """构建 rich 流式卡片骨架（Schema 2.0）。"""
    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "enable_forward": True,
            "width_mode": "fill",
            "summary": {"content": "生成中..."},
            "streaming_mode": True,
            "streaming_config": STREAMING_CONFIG,
        },
        "header": {
            "title": {"tag": "plain_text", "content": "生成中..."},
            "template": "blue",
            "text_tag_list": [
                {"tag": "text_tag", "text": {"tag": "plain_text", "content": "生成中"},
                 "color": "blue"},
            ],
        },
        "body": {
            "direction": "vertical",
            "vertical_spacing": "medium",
            "elements": [
                {
                    "tag": "markdown",
                    "element_id": EID["STATUS_BANNER"],
                    "content": "<text_tag color='violet'>生成回复</text_tag> ✨",
                },
                {
                    "tag": "markdown",
                    "element_id": EID["MAIN_CONTENT"],
                    "content": initial_text,
                },
                {
                    "tag": "markdown",
                    "element_id": EID["FOOTER_NOTE"],
                    "content": "<font color='grey'>⏳ 生成中...</font>",
                    "text_size": "notation",
                },
            ],
        },
    }


def _build_status_banner(phase: str, elapsed_ms: int = 0) -> str:
    """构建状态 banner 文本。"""
    elapsed = ""
    if elapsed_ms > 0:
        sec = elapsed_ms / 1000
        if sec < 60:
            elapsed = f" <font color='grey'>· {sec:.1f}s</font>"
        else:
            m = int(sec / 60)
            s = int(sec % 60)
            elapsed = f" <font color='grey'>· {m}m{s}s</font>"

    phases = {
        "streaming": ("<text_tag color='violet'>生成回复</text_tag> ✨", elapsed),
        "thinking": ("<text_tag color='blue'>思考中</text_tag> 🧠", elapsed),
        "tooling":  ("<text_tag color='turquoise'>调用工具</text_tag> 🛠", elapsed),
        "completed": ("<text_tag color='green'>已完成</text_tag> ✅", ""),
        "error":    ("<text_tag color='red'>出错</text_tag> ❌", ""),
    }
    tag, extra = phases.get(phase, phases["streaming"])
    return f"{tag}{extra}"


class FeishuRichCardController:
    """富流式卡片控制器 — 低层 CardKit API。

    替代 SDK 的 channel.stream()，让调用方控制卡片 schema 和按元素更新。
    """

    def __init__(self, channel: "FeishuChannel"):
        self._channel = channel
        self._card_id: str | None = None
        self._message_id: str = ""
        self._sequence: int = 0
        self._start_time: float = 0.0
        self._content: str = ""
        self._phase: str = "streaming"

    @property
    def card_id(self) -> str | None:
        return self._card_id

    @property
    def message_id(self) -> str:
        return self._message_id

    @property
    def sequence(self) -> int:
        return self._sequence

    def _next_seq(self) -> int:
        self._sequence += 1
        return self._sequence

    async def create_and_send(
        self, chat_id: str, reply_to: str | None = None
    ) -> str:
        """创建 CardKit 卡片实体 + 发送为消息。返回 message_id。"""
        card_json = _build_rich_streaming_card()
        self._card_id = await self._channel.create_card_instance(card_json)
        self._next_seq()
        self._start_time = time.monotonic()

        result = await self._channel.send_card_by_reference(
            chat_id, self._card_id,
            reply_to=reply_to,
        )
        self._message_id = getattr(result, "message_id", "") or ""
        logger.info(
            "[RichCard] Card created: card_id={}, msg_id={}",
            self._card_id[:12] if self._card_id else "?", self._message_id[:12] if self._message_id else "?",
        )
        return self._message_id

    async def _enable_streaming(self) -> None:
        """通过 card.settings() 开启 CardKit streaming_mode。"""
        try:
            settings_json = {
                "config": {
                    "streaming_mode": True,
                    "streaming_config": STREAMING_CONFIG,
                }
            }
            await self._channel.client.cardkit.v1.card.settings(
                path={"card_id": self._card_id},
                data={
                    "settings": json.dumps(settings_json),
                    "sequence": self._next_seq(),
                },
            )
            logger.info("[RichCard] Streaming mode enabled: card_id={}", self._card_id[:12] if self._card_id else "?")
        except Exception as e:
            logger.warning("[RichCard] _enable_streaming failed: {}", e)
            raise

    async def stream_content(self, text: str) -> None:
        """流式更新 main_content 元素（打字机效果）。"""
        if not self._card_id:
            return
        self._content = text
        try:
            await self._channel.update_card_element_content(
                self._card_id, EID["MAIN_CONTENT"],
                text or "...", sequence=self._next_seq(),
            )
        except Exception as e:
            logger.warning("[RichCard] stream_content failed: {}", e)

    async def update_status(self, phase: str) -> None:
        """更新状态 banner。"""
        if not self._card_id or phase == self._phase:
            return
        self._phase = phase
        elapsed = int((time.monotonic() - self._start_time) * 1000) if self._start_time > 0 else 0
        banner = _build_status_banner(phase, elapsed)
        try:
            await self._channel.update_card_element_content(
                self._card_id, EID["STATUS_BANNER"],
                banner, sequence=self._next_seq(),
            )
            # 同步更新 footer
            footer = {
                "streaming": "<font color='grey'>⏳ 生成中...</font>",
                "thinking": "<font color='grey'>💭 思考中...</font>",
                "tooling": "<font color='grey'>🛠 调用工具中...</font>",
                "completed": "<font color='grey'>✅ 已完成</font>",
                "error": "<font color='grey'>❌ 出错</font>",
            }.get(phase, "<font color='grey'>⏳ 生成中...</font>")
            await self._channel.update_card_element_content(
                self._card_id, EID["FOOTER_NOTE"],
                footer, sequence=self._next_seq(),
            )
        except Exception as e:
            logger.warning("[RichCard] update_status failed: {}", e)

    async def finish(self) -> None:
        """关闭 streaming_mode，卡片最终渲染。"""
        if not self._card_id:
            return
        try:
            # 更新状态为完成
            elapsed = int((time.monotonic() - self._start_time) * 1000) if self._start_time > 0 else 0
            banner = _build_status_banner("completed", elapsed)
            await self._channel.update_card_element_content(
                self._card_id, EID["STATUS_BANNER"],
                banner, sequence=self._next_seq(),
            )
            await self._channel.update_card_element_content(
                self._card_id, EID["FOOTER_NOTE"],
                f"<font color='grey'>✅ 已完成 · {elapsed/1000:.1f}s</font>" if elapsed > 0 else "<font color='grey'>✅ 已完成</font>",
                sequence=self._next_seq(),
            )
            await self._channel.finish_streaming_card(
                self._card_id, sequence=self._next_seq(),
            )
            logger.info("[RichCard] Card finished: card_id={}", self._card_id[:12] if self._card_id else "?")
        except Exception as e:
            logger.warning("[RichCard] finish failed: {}", e)
