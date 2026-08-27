"""CardKit streaming bridge for Feishu Card Mode — LangGraph integration.

Manages the full lifecycle of a Feishu streaming card bound to a single LangGraph
run.  The bridge is lazily created on the first ``_model()`` call and persists
across checkpoints via a module-level registry keyed by ``run_id``.

Reference: deepthink ``StreamingCardController`` + ``StreamingModeBackend``.

Lifecycle
---------
``start()`` → ``push_text()``* → ``update_tool_status()``* → ``finalize()``
                                                          → ``abort()``
``withdraw()`` — recall a pre-created card (resume path); valid at any point
after ``start()`` is scheduled, including before it runs and mid-creation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import TYPE_CHECKING, Awaitable, Callable

from loguru import logger

if TYPE_CHECKING:
    from app.services.feishu_service import FeishuService

# ---------------------------------------------------------------------------
# Global bridge registry — survives across LangGraph checkpoints.
# v1: in-process dict (single worker).  v2: migrate to Redis with TTL.
# ---------------------------------------------------------------------------

_card_bridges: dict[str, CardStreamBridge] = {}
_MAX_BRIDGE_AGE_SECONDS = 1800  # 30 min — older bridges are leaked / zombie


def get_bridge(run_id: str) -> CardStreamBridge | None:
    """Look up the active bridge for a run, evicting stale entries."""
    bridge = _card_bridges.get(run_id)
    if bridge is None:
        return None
    if time.monotonic() - bridge._start_time > _MAX_BRIDGE_AGE_SECONDS:
        logger.warning(
            "[FEISHU-CARD] bridge_expired run_id={}", run_id,
        )
        try:
            asyncio.create_task(bridge.abort("卡片超时"))
        except RuntimeError:
            pass  # no running event loop (tests / sync context)
        _card_bridges.pop(run_id, None)
        return None
    return bridge


def register_bridge(run_id: str, bridge: CardStreamBridge) -> None:
    """Register (or replace) a bridge for a run."""
    existing = _card_bridges.get(run_id)
    if existing is not None and existing is not bridge:
        logger.warning(
            "[FEISHU-CARD] bridge_replaced run_id={}", run_id,
        )
        try:
            asyncio.create_task(existing.abort("bridge 被替换"))
        except RuntimeError:
            pass  # no running event loop
    _card_bridges[run_id] = bridge


def unregister_bridge(run_id: str) -> None:
    """Remove a bridge from the registry after finalization."""
    _card_bridges.pop(run_id, None)


# ---------------------------------------------------------------------------
# FlushController — deepthink-style coalescing throttle
# ---------------------------------------------------------------------------

class FlushController:
    """合并式节流器：新 pending 替换旧 pending，不足阈值时兜底定时。

    参数对齐 deepthink ``FlushController``：
    ``min_interval`` — 两次推送的最小间隔 (s)
    ``min_delta``    — 触发即时推送的最小内容变化 (chars)
    """

    def __init__(self, min_interval: float = 0.6, min_delta: int = 30) -> None:
        self._min_interval = min_interval
        self._min_delta = min_delta
        self._timer: asyncio.TimerHandle | None = None
        self._last_flush_time = 0.0
        self._last_flushed_length = 0
        self._pending: Callable[[], Awaitable[None]] | None = None

    def schedule(self, current_length: int, flush_fn: Callable[[], Awaitable[None]]) -> None:
        loop = asyncio.get_running_loop()
        now = loop.time()
        if current_length - self._last_flushed_length < self._min_delta:
            if self._timer is None:
                self._pending = flush_fn
                self._timer = loop.call_later(self._min_interval, self._on_timer)
            else:
                self._pending = flush_fn  # 替换旧 pending，不堆积
            return

        self._pending = flush_fn
        elapsed = now - self._last_flush_time if self._last_flush_time else self._min_interval
        if elapsed >= self._min_interval:
            self._clear_timer()
            asyncio.ensure_future(self._execute())
        elif self._timer is None:
            self._timer = loop.call_later(self._min_interval - elapsed, self._on_timer)

    async def force_flush(self, flush_fn: Callable[[], Awaitable[None]]) -> None:
        self._clear_timer()
        self._pending = flush_fn
        await self._execute()

    def mark_flushed(self, length: int) -> None:
        self._last_flushed_length = length

    def dispose(self) -> None:
        self._clear_timer()
        self._pending = None

    def _on_timer(self) -> None:
        self._timer = None
        asyncio.ensure_future(self._execute())

    async def _execute(self) -> None:
        fn = self._pending
        self._pending = None
        if fn is None:
            return
        self._last_flush_time = asyncio.get_running_loop().time()
        try:
            await fn()
        except Exception:
            logger.exception("[FlushController] flush failed")

    def _clear_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None


# CardStreamBridge
# ---------------------------------------------------------------------------

class CardStreamBridge:
    """Manages the Feishu CardKit streaming lifecycle for a single LangGraph run.

    All CardKit API calls are serialised through an ``asyncio.Lock`` to keep
    ``sequence`` strictly increasing (Feishu requirement).

    Parameters
    ----------
    feishu_service:
        The global ``FeishuService`` singleton.
    app_id / app_secret:
        Per-agent Feishu app credentials.
    receive_id / receive_id_type:
        Target user / group to send the card to.
    agent_name:
        Display name for the card header.
    run_id:
        The real LangGraph run UUID; used for bridge registry lookup so the
        in-chat 「中断」 command can abort the streaming card.
    """

    # Element IDs for the multi-panel streaming skeleton.
    _ELEMENT_IDS: dict[str, str] = {
        "status_banner": "status_banner",
        "thinking_live_md": "thinking_live_md",
        "tools_live_md": "tools_live_md",
        "main_content": "main_content",
        "interrupt_hint": "interrupt_hint",
        "footer_note": "footer_note",
    }

    # 终版卡片思考面板的字符上限。思考原文逐轮累积无界增长（reasoning
    # 回放协议下历史大头即思考），超限只保留尾部（最新几轮），沿袭实时面板
    # [-2000:] 尾窗的展示思路；否则长 run 会打爆 CardKit 载荷上限，finalize
    # 失败导致整卡（含正文）降级为纯文本。取值依据见
    # docs/technical-plans/20260827-feishu-card-thinking-panel-truncation.md。
    _THINKING_TERMINAL_MAX_CHARS = 8_000

    # ---- helpers ------------------------------------------------------------

    @staticmethod
    def _quote_lines(text: str) -> str:
        """整段引用为 Markdown blockquote（每行 `> ` 前缀）."""
        return "\n".join(f"> {line}" for line in text.split("\n"))

    @staticmethod
    def _fmt_elapsed(seconds: float) -> str:
        if seconds < 60:
            return f"{seconds:.1f}秒"
        if seconds < 3600:
            m = int(seconds // 60)
            s = int(seconds % 60)
            return f"{m}分{s}秒"
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        return f"{h}小时{m}分{s}秒"

    # ---- construction -------------------------------------------------------

    def __init__(
        self,
        *,
        feishu_service: FeishuService,
        app_id: str,
        app_secret: str,
        receive_id: str,
        receive_id_type: str,
        agent_name: str,
        run_id: str,
    ):
        self._fs = feishu_service
        self._app_id = app_id
        self._app_secret = app_secret
        self._receive_id = receive_id
        self._receive_id_type = receive_id_type
        self._agent_name = agent_name
        self._run_id = run_id

        # Card identity
        self.card_id: str | None = None
        self.message_id: str | None = None
        self._sequence: int = 0
        self._lock = asyncio.Lock()  # serialise all CardKit API calls

        # Content tracking — per-element MD5 dedup (deepthink ``richSlotHashes``)
        self._last_main_hash: str = ""
        self._last_banner_hash: str = ""
        self._last_thinking_hash: str = ""
        self._last_tools_hash: str = ""
        self._last_footer_hash: str = ""

        # Accumulated content for terminal card
        self._thinking_text: str = ""

        # 辅助面板统一节流器 — thinking + tools + banner
        self._aux_flush = FlushController(min_interval=1.0, min_delta=0)
        # footer 独立计时器 — 更快刷新
        self._footer_flush = FlushController(min_interval=0.8, min_delta=0)

        # Tool state  ({tool_call_id: {name, status, start_time}})
        self._tool_states: dict[str, dict] = {}

        # 思考/工具面板延迟加载 — 骨架中为占位符，有内容时替换为 collapsible_panel
        self._thinking_panel_added: bool = False
        self._tools_panel_added: bool = False
        self._last_footer_push_time: float = 0.0

        # Lifecycle
        self._start_time: float = time.monotonic()
        self._state: str = "idle"
        self._creation_future: asyncio.Future | None = None
        self._streaming: bool = False
        self._finalized: bool = False
        self._withdrawn: bool = False
        self._card_ready: asyncio.Event = asyncio.Event()

        # Resilience
        self._patch_fail_count: int = 0
        self._max_patch_failures: int = 3

    # ---- public lifecycle ---------------------------------------------------

    async def start(self) -> None:
        """Create the CardKit entity, send the message, and enter streaming mode.

        Must be called before any ``push_text`` or tool-status calls.
        """
        self._start_time = time.monotonic()
        self._state = "creating"
        self._creation_future = asyncio.Future()

        if self._withdrawn:
            # 建卡开始前已被撤回 — 不建卡、不发送，直接终态。
            self._state = "withdrawn"
            self._card_ready.set()
            self._creation_future.set_result(None)
            return

        try:
            card = self._build_streaming_skeleton()
            self.card_id = await self._fs.create_card_entity(
                self._app_id, self._app_secret, card,
            )
            self._sequence = 1
            send_data = await self._fs.send_card_by_card_id(
                self._app_id, self._app_secret,
                self._receive_id, self.card_id, self._receive_id_type,
            )
            if isinstance(send_data, dict):
                data_payload = send_data.get("data")
                if isinstance(data_payload, dict):
                    self.message_id = data_payload.get("message_id")
            self._streaming = True
            self._state = "streaming"
            self._card_ready.set()
            logger.info("[FEISHU-CARD] card_created card_id={}", self.card_id)
        except Exception:
            self._state = "error"
            self._card_ready.set()  # 防止后续 _card_ready.wait() 永久挂起
            raise
        finally:
            self._creation_future.set_result(None)

        # If finalize / abort was requested during creation, honour it now.
        # withdraw() deletes the message itself — do not render a terminal card.
        if self._finalized and self._streaming and not self._withdrawn:
            await self.finalize("")

    async def withdraw(self) -> None:
        """Recall a card that was created ahead of time but is no longer needed.

        Used when the message turns out to resume an existing Run (its card keeps
        streaming): the pre-created skeleton card must not linger in the chat.
        If creation is still in flight, waits for it to finish; if ``start()``
        has not run yet, the pending start will bail out instead of sending.
        A recall failure is logged and contained — the resume flow continues.
        """
        self._withdrawn = True
        self._aux_flush.dispose()
        self._footer_flush.dispose()
        if self._state == "creating" and self._creation_future is not None:
            try:
                await asyncio.wait_for(self._creation_future, timeout=15)
            except Exception:
                logger.exception("[FEISHU-CARD] withdraw_creation_wait_failed")
                return
        self._streaming = False
        self._finalized = True
        self._state = "withdrawn"
        if not self.message_id:
            return
        try:
            await self._fs.delete_message(
                self._app_id, self._app_secret, self.message_id,
            )
            logger.info(
                "[FEISHU-CARD] card_withdrawn card_id={} message_id={}",
                self.card_id, self.message_id,
            )
        except Exception:
            # 撤回失败只留下一个空的骨架卡片 — 无害，resume 流程继续。
            logger.exception(
                "[FEISHU-CARD] card_withdraw_failed card_id={}", self.card_id,
            )

    async def push_text(self, content: str) -> None:
        """Push the *full accumulated* text to the main-content element.

        MD5-deduplicated; no-op if the bridge is no longer streaming.
        """
        if not self._streaming:
            return
        if not content.strip():
            # 空正文不得推送: CardKit 对空字符串返回 99992402
            # (content min len is 1)。模型只输出推理、未输出正文时,
            # 每步最终 flush 都会把空串推给 main_content, 且 MD5 去重
            # 因推送失败无法落定, 每步重复失败形成错误风暴 — 直接跳过。
            return
        await self._card_ready.wait()  # no-op once set by start()
        if not self.card_id:
            return

        # Truncate at 100K chars (Feishu practical limit, per deepthink).
        max_len = 100_000
        if len(content) > max_len:
            content = content[: max_len - 50] + "\n\n> ⚠️ 输出已截断（超过 100K 字符）"

        await self._enqueue_push(
            self._ELEMENT_IDS["main_content"],
            content,
            "_last_main_hash",
        )
        self._footer_flush.schedule(0, self._push_footer)
        self._aux_flush.schedule(0, self._flush_aux_panels)

    def start_tool(self, tool_call_id: str, tool_name: str) -> None:
        """Record that a tool started executing (non-blocking push)."""
        self._tool_states[tool_call_id] = {
            "name": tool_name,
            "status": "running",
            "start_time": time.monotonic(),
        }
        # 面板物化是结构性事件：直接触发专用任务，不与节流内容推送排队。
        # （曾排在 _flush_aux_panels 队尾，流式高峰下物化被拖后 10-20s。）
        asyncio.create_task(self._ensure_tools_panel())
        asyncio.create_task(self._schedule_aux_flush())

    def end_tool(self, tool_call_id: str, is_error: bool = False) -> None:
        """Record that a tool finished (non-blocking push)."""
        entry = self._tool_states.get(tool_call_id)
        if entry is None:
            return
        entry["status"] = "error" if is_error else "complete"
        entry["end_time"] = time.monotonic()
        asyncio.create_task(self._schedule_aux_flush())

    async def finalize(self, final_text: str) -> None:
        """Close streaming mode and replace the card with a terminal version.

        Must be called exactly once when the LangGraph run reaches a terminal
        status (completed / failed / cancelled).
        """
        if self._finalized or not self._streaming:
            return
        self._finalized = True

        # If the card is still being created, wait for creation to finish.
        if self._state == "creating" and self._creation_future is not None:
            try:
                await self._creation_future
            except Exception:
                pass

        if self._state == "error":
            raise RuntimeError("Card creation failed, cannot finalize")

        self._state = "finalizing"
        self._streaming = False
        self._aux_flush.dispose()
        self._footer_flush.dispose()

        # 模型只输出推理、未输出正文时 final_text 为空 — 终版卡片
        # 不能留白, 否则用户看到一张没有正文的卡片。
        display_text = (final_text or "").strip() or "✅ 任务已完成（本次未输出正文）"

        try:
            # Push terminal banner, then close streaming.
            await self._enqueue_push(
                "status_banner", "✅ 任务已经完成", "_last_banner_hash",
            )

            self._sequence += 1
            await self._fs.set_card_streaming_mode(
                self._app_id, self._app_secret,
                self.card_id, 0, self._sequence,
            )

            # Replace with terminal card.
            elapsed_ms = int((time.monotonic() - self._start_time) * 1000)
            terminal_card = self._build_terminal_card(display_text, elapsed_ms)

            self._sequence += 1
            await self._fs.update_cardkit_card(
                self._app_id, self._app_secret,
                self.card_id, terminal_card, self._sequence,
            )

            self._state = "terminal"
            logger.info(
                "[FEISHU-CARD] card_completed card_id={} elapsed={}ms",
                self.card_id, elapsed_ms,
            )
        except Exception as exc:
            logger.error("[FEISHU-CARD] card_finalize_failed: {}", exc)
            await self._fallback_to_text(display_text)

    async def abort(self, reason: str = "⏹ 回复已中断") -> None:
        """Interrupt — push banner, close streaming, replace with terminal card."""
        self._finalized = True
        self._streaming = False
        self._aux_flush.dispose()
        self._footer_flush.dispose()
        try:
            await self._enqueue_push(
                "status_banner", "⚠️ 已中断", "_last_banner_hash",
            )
            seq = self._sequence + 1
            await self._fs.set_card_streaming_mode(
                self._app_id, self._app_secret, self.card_id, 0, seq,
            )
            elapsed_ms = int((time.monotonic() - self._start_time) * 1000)
            terminal = self._build_terminal_card(reason or "⏹ 回复已中断", elapsed_ms)
            seq += 1
            await self._fs.update_cardkit_card(
                self._app_id, self._app_secret, self.card_id, terminal, seq,
            )
        except Exception:
            await self._fallback_to_text(reason or "回复已中断")

    async def fallback_error(self, error: str) -> None:
        """Error — push banner, close streaming, show error card."""
        self._finalized = True
        self._streaming = False
        self._aux_flush.dispose()
        self._footer_flush.dispose()
        try:
            await self._enqueue_push(
                "status_banner", "❌ 处理失败", "_last_banner_hash",
            )
            seq = self._sequence + 1
            await self._fs.set_card_streaming_mode(
                self._app_id, self._app_secret, self.card_id, 0, seq,
            )
            card = self._build_error_card(error)
            seq += 1
            await self._fs.update_cardkit_card(
                self._app_id, self._app_secret, self.card_id, card, seq,
            )
        except Exception:
            await self._fallback_to_text(error)

    def _build_error_card(self, error: str) -> dict:
        return {
            "schema": "2.0",
            "config": {"update_multi": True, "width_mode": "default"},
            "header": {
                "title": {"tag": "plain_text", "content": self._agent_name},
                "template": "blue",
            },
            "body": {"direction": "vertical", "elements": [
                {"tag": "markdown", "content": f"❌ 处理失败\n\n{error}"},
            ]},
        }

    # ---- card builders ------------------------------------------------------

    def _build_streaming_skeleton(self) -> dict:
        """Return the initial CardKit JSON — 2 collapsible panels + column_set footer."""
        return {
            "schema": "2.0",
            "config": {
                "streaming_mode": True,
                "streaming_config": {
                    "print_frequency_ms": {"default": 50},
                    "print_step": {"default": 2},
                    "print_strategy": "fast",
                },
                "update_multi": True,
                "width_mode": "default",
                "summary": {"content": self._agent_name},
            },
            "header": {
                "title": {"tag": "plain_text", "content": self._agent_name},
                "template": "blue",
            },
            "body": {
                "direction": "vertical",
                "elements": [
                    {"tag": "markdown", "element_id": "status_banner",
                     "content": "⏳ 准备中..."},

                    # 占位符 — 无内容时不可见，有内容时替换为 collapsible_panel
                    {"tag": "markdown", "element_id": "thinking_placeholder",
                     "content": "", "text_size": "notation"},
                    {"tag": "markdown", "element_id": "tools_placeholder",
                     "content": "", "text_size": "notation"},

                    {"tag": "markdown", "element_id": "main_content",
                     "content": "..."},

                    {"tag": "hr", "element_id": "card_divider", "margin": "0px 0px 0px 0px"},

                    {"tag": "column_set", "flex_mode": "none",
                     "columns": [
                         {"tag": "column", "width": "auto",
                          "elements": [
                              # 注意: 长连接(WS)模式下飞书不投递卡片按钮回调
                              # (card.action.trigger 仅走 Webhook), 按钮永远点不响。
                              # 因此用提示文案代替按钮 — 回复「中断」即可停止。
                              {"tag": "markdown", "element_id": "interrupt_hint",
                               "content": "💡 回复「中断」可停止",
                               "text_size": "notation"},
                          ]},
                         {"tag": "column", "width": "weighted", "weight": 1,
                          "elements": [
                              {"tag": "markdown", "element_id": "footer_note",
                               "content": "⏱ 0秒",
                               "text_size": "notation",
                               "text_align": "right"},
                          ]},
                     ]},
                ],
            },
        }

    def _build_terminal_card(self, final_text: str, elapsed_ms: int) -> dict:
        """Return the non-streaming final card JSON — retains thinking/tools panels."""
        text = final_text[:28_000]
        if len(final_text) > 28_000:
            text += "\n\n> ⚠️ 输出已截断"

        elements: list[dict] = []

        if self._thinking_text.strip():
            elements.append({
                "tag": "collapsible_panel", "expanded": False,
                "header": {"title": {"tag": "markdown", "content": "💭 思考过程"},
                            "background_color": "blue-50",
                            "icon": {"tag": "standard_icon",
                                     "token": "down-small-ccm_outlined",
                                     "size": "14px 14px"},
                            "icon_position": "right",
                            "icon_expanded_angle": 180},
                "elements": [{"tag": "markdown",
                              "content": self._build_thinking_blockquote()}],
            })

        tools = self._build_tools_timeline()
        if tools:
            elements.append({
                "tag": "collapsible_panel", "expanded": False,
                "header": {"title": {"tag": "markdown", "content": "🛠 工具"},
                            "background_color": "blue-50",
                            "icon": {"tag": "standard_icon",
                                     "token": "down-small-ccm_outlined",
                                     "size": "14px 14px"},
                            "icon_position": "right",
                            "icon_expanded_angle": 180},
                "elements": [{"tag": "markdown", "content": tools}],
            })

        elements.append({"tag": "markdown", "content": text})
        elements.append({"tag": "hr", "element_id": "card_divider", "margin": "0px 0px 0px 0px"})
        elements.append({
            "tag": "markdown",
            "content": f"⏱ {self._fmt_elapsed(elapsed_ms / 1000)}",
            "text_size": "notation",
            "text_align": "right",
        })

        return {
            "schema": "2.0",
            "config": {"update_multi": True, "width_mode": "default"},
            "header": {
                "title": {"tag": "plain_text", "content": self._agent_name},
                "template": "blue",
            },
            "body": {"direction": "vertical", "elements": elements},
        }

    # ---- thinking -----------------------------------------------------------

    async def push_thinking(self, content: str) -> None:
        """累积思考增量；由 aux flush 统一推送到思考面板。"""
        if not self._streaming:
            return
        await self._card_ready.wait()
        self._thinking_text += content
        if not self._thinking_text.strip():
            return
        self._aux_flush.schedule(len(self._thinking_text), self._flush_aux_panels)
        self._footer_flush.schedule(0, self._push_footer)

    def _build_thinking_panel(self) -> dict:
        """构建思考过程 collapsible_panel — 首次有内容时替换占位符."""
        return {
            "tag": "collapsible_panel",
            "element_id": "thinking_panel_live",
            "expanded": False,
            "header": {
                "title": {"tag": "markdown", "content": "💭 思考过程"},
                "background_color": "blue-50",
                "icon": {
                    "tag": "standard_icon",
                    "token": "down-small-ccm_outlined",
                    "size": "14px 14px",
                },
                "icon_position": "right",
                "icon_expanded_angle": 180,
            },
            "elements": [
                {"tag": "markdown", "element_id": "thinking_live_md", "content": ""}
            ],
        }

    def _build_tools_panel(self) -> dict:
        """构建工具执行 collapsible_panel — 首次有工具时替换占位符."""
        return {
            "tag": "collapsible_panel",
            "element_id": "tools_panel_live",
            "expanded": False,
            "header": {
                "title": {"tag": "markdown", "content": "🛠 工具"},
                "background_color": "blue-50",
                "icon": {
                    "tag": "standard_icon",
                    "token": "down-small-ccm_outlined",
                    "size": "14px 14px",
                },
                "icon_position": "right",
                "icon_expanded_angle": 180,
            },
            "elements": [
                {"tag": "markdown", "element_id": "tools_live_md", "content": ""}
            ],
        }

    def _build_thinking_blockquote(self) -> str:
        if not self._thinking_text.strip():
            return ""
        text = self._thinking_text
        truncated = False
        if len(text) > self._THINKING_TERMINAL_MAX_CHARS:
            # 保留尾部（最新几轮思考），在行边界裁剪，避免首行拦腰截断。
            # 退化场景：单条思考增量本身就是无换行、>上限的超长行时，
            # 上限优先于行边界（两者物理冲突，无法两全）。
            tail = text[-self._THINKING_TERMINAL_MAX_CHARS:]
            first_newline = tail.find("\n")
            if first_newline != -1:
                tail = tail[first_newline + 1:]
            text = tail
            truncated = True
        quoted = self._quote_lines(text)
        if truncated:
            quoted = "> ⚠️ 思考内容过长，仅展示最后一段\n" + quoted
        return quoted

    async def _push_footer(self) -> None:
        """独立推送 footer 计时器，不受辅刷新间隔限制."""
        if not self._streaming or not self.card_id:
            return
        elapsed = time.monotonic() - self._start_time
        try:
            await self._enqueue_push(
                "footer_note", f"⏱ {self._fmt_elapsed(elapsed)}",
                "_last_footer_hash", critical=False,
            )
        except Exception:
            logger.debug("[FEISHU-CARD] footer_push_failed", exc_info=True)

    # ---- unified aux flush (deepthink scheduleAuxFlush) --------------------

    async def _schedule_aux_flush(self) -> None:
        """调度辅助面板刷新 — 对齐 deepthink scheduleAuxFlush."""
        # min_delta=0 使 current_length 参数被忽略，每次调用均触发刷新
        self._aux_flush.schedule(0, self._flush_aux_panels)

    async def _ensure_tools_panel(self) -> None:
        """物化工具面板占位符 — 一次性结构性事件，直接执行不排队。

        检查-置位在锁内完成，防并发 flush 双物化导致 sequence 乱序。
        """
        if not self._streaming or not self.card_id or not self._tool_states:
            return
        if self._tools_panel_added:
            return  # 快路径：已物化，flush 兜底调用免锁跳过
        try:
            async with self._lock:
                if self._tools_panel_added:
                    return
                self._tools_panel_added = True
                panel = self._build_tools_panel()
                self._sequence += 1
                await self._fs.update_card_element(
                    self._app_id, self._app_secret,
                    self.card_id, "tools_placeholder",
                    panel, self._sequence,
                )
        except Exception:
            self._tools_panel_added = False
            logger.debug("[FEISHU-CARD] tools_panel_add_failed", exc_info=True)

    async def _flush_aux_panels(self) -> None:
        """统一刷新所有辅助面板 — 对齐 deepthink buildRichPanelPatches.

        在一次推送中刷新: 思考文本 + 状态横幅 + 工具面板 + footer。
        辅助面板是 best-effort 装饰，失败不触发降级 (critical=False)。
        """
        if not self._streaming or not self.card_id:
            return

        # 思考文本 — 首次有内容时替换占位符为 collapsible_panel
        if self._thinking_text.strip():
            if not self._thinking_panel_added:
                self._thinking_panel_added = True
                try:
                    panel = self._build_thinking_panel()
                    async with self._lock:
                        self._sequence += 1
                        await self._fs.update_card_element(
                            self._app_id, self._app_secret,
                            self.card_id, "thinking_placeholder",
                            panel, self._sequence,
                        )
                except Exception:
                    self._thinking_panel_added = False
                    logger.debug("[FEISHU-CARD] thinking_panel_add_failed", exc_info=True)
            if self._thinking_panel_added:
                text = self._thinking_text[-2000:]
                quoted = self._quote_lines(text)
                try:
                    await self._enqueue_push(
                        "thinking_live_md", quoted, "_last_thinking_hash",
                        critical=False, check_streaming=False,
                    )
                except Exception:
                    logger.debug("[FEISHU-CARD] aux_thinking_push_failed", exc_info=True)

        # 状态横幅
        try:
            await self._enqueue_push(
                "status_banner", self._build_status_banner(), "_last_banner_hash",
                critical=False, check_streaming=False,
            )
        except Exception:
            logger.debug("[FEISHU-CARD] aux_banner_push_failed", exc_info=True)

        # 工具面板 — 首次有工具时物化占位符（start_tool 已直发专用任务，此处兜底）
        if self._tool_states:
            await self._ensure_tools_panel()
        if self._tool_states and self._tools_panel_added:
            lines: list[str] = []
            for t in list(self._tool_states.values())[-8:]:
                icon = {"running": "🔄", "complete": "✅", "error": "❌"}.get(
                    t["status"], "○",
                )
                lines.append(f"{icon} {t['name']}")
            tools = "\n".join(lines)
            if tools:
                try:
                    await self._enqueue_push(
                        "tools_live_md", tools, "_last_tools_hash",
                        critical=False, check_streaming=False,
                    )
                except Exception:
                    logger.debug("[FEISHU-CARD] aux_tools_push_failed", exc_info=True)


    # ---- tool-status helpers (deprecated: logic inlined in _flush_aux_panels) --

    async def _push_tool_updates(self) -> None:
        """Push status banner + tools panel content.  Deprecated — use _schedule_aux_flush."""
        await self._schedule_aux_flush()

    def _build_status_banner(self) -> str:
        """Derive status-banner from tool states.  Terminal/abort/error pushed separately."""
        running = [t for t in self._tool_states.values() if t["status"] == "running"]
        if running:
            primary = running[0]["name"]
            n = len(running)
            return f"🛠 调用工具 `{primary}`" + (f" +{n - 1}个" if n > 1 else "")

        completed = [t for t in self._tool_states.values() if t["status"] == "complete"]
        if completed:
            return f"✅ 已完成 {len(completed)} 个工具"
        return "⏳ 思考中..."

    def _build_tools_timeline(self) -> str:
        """Render tool calls for the terminal card (flat, no seconds)."""
        if not self._tool_states:
            return ""
        lines: list[str] = []
        for t in list(self._tool_states.values())[-12:]:
            icon = {"running": "🔄", "complete": "✅", "error": "❌"}.get(
                t["status"], "○",
            )
            lines.append(f"{icon} {t['name']}")
        return "\n".join(lines)

    # ---- low-level push -----------------------------------------------------

    async def _enqueue_push(
        self, element_id: str, content: str, hash_attr: str,
        *, critical: bool = True, check_streaming: bool = True,
    ) -> None:
        """Serialised push with MD5 dedup and self-healing on timeout.

        Parameters
        ----------
        critical:
            True 时失败递增 _patch_fail_count 并可能触发降级。
            辅助面板 (thinking/tools/banner/footer) 使用 critical=False，
            失败不触发降级，保持正文推送不受影响。
        """
        h = hashlib.md5(content.encode()).hexdigest()
        if getattr(self, hash_attr) == h:
            return  # fast-path: unchanged

        async with self._lock:
            # Double-check inside the lock.
            if getattr(self, hash_attr) == h:
                return
            if check_streaming and not self._streaming:
                return

            self._sequence += 1
            try:
                await self._fs.stream_card_content(
                    self._app_id, self._app_secret,
                    self.card_id, element_id, content, self._sequence,
                )
                setattr(self, hash_attr, h)
                self._patch_fail_count = 0
            except Exception as exc:
                code = getattr(exc, "code", None)
                if code in (200850, 300309):
                    # Streaming timeout — re-enable and retry once.
                    logger.info(
                        "[FEISHU-CARD] streaming_timeout card_id={} code={}",
                        self.card_id, code,
                    )
                    self._sequence += 1
                    await self._fs.set_card_streaming_mode(
                        self._app_id, self._app_secret,
                        self.card_id, 1, self._sequence,
                    )
                    self._sequence += 1
                    await self._fs.stream_card_content(
                        self._app_id, self._app_secret,
                        self.card_id, element_id, content, self._sequence,
                    )
                    setattr(self, hash_attr, h)
                    self._patch_fail_count = 0
                else:
                    if critical:
                        self._patch_fail_count += 1
                    # 截断异常消息防止 app_secret 泄漏到日志
                    safe_msg = str(exc)[:200]
                    logger.error(
                        "[FEISHU-CARD] push_failed card_id={} element={} "
                        "fail_count={} critical={} error_preview={}",
                        self.card_id, element_id, self._patch_fail_count,
                        critical, safe_msg,
                    )
                    if critical and self._patch_fail_count >= self._max_patch_failures:
                        await self._degrade_to_v1()
                    raise

    async def _degrade_to_v1(self) -> None:
        """L1 fallback: close streaming, switch to full-card-update mode."""
        if not self._streaming:
            return
        logger.warning("[FEISHU-CARD] degrading_to_v1 card_id={}", self.card_id)
        self._aux_flush.dispose()
        self._footer_flush.dispose()

        try:
            self._sequence += 1
            await self._fs.set_card_streaming_mode(
                self._app_id, self._app_secret,
                self.card_id, 0, self._sequence,
            )
        except Exception:
            pass

        self._streaming = False
        # Subsequent pushes will reach the lock but skip because streaming is off;
        # finalize() will use update_cardkit_card which does not need streaming_mode.

    async def _fallback_to_text(self, content: str) -> None:
        """L2 fallback: send a plain-text message as last resort."""
        text = content[:5000]
        if len(content) > 5000:
            text += "\n\n...\n_(内容过长，已截断)_"
        try:
            await self._fs.send_message(
                self._app_id, self._app_secret,
                self._receive_id, "text",
                json.dumps({"text": text}),
                self._receive_id_type,
            )
        except Exception as exc:
            logger.error("[FEISHU-CARD] text_fallback_failed: {}", exc)
        logger.warning("[FEISHU-CARD] card_fallback: plain-text message sent")
