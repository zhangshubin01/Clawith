"""Card mode (Feishu CardKit) unit and integration tests.

Covers:
- CardStreamBridge lifecycle (create/push_text/start_tool/end_tool/finalize/abort)
- Bridge registry (get/register/unregister, expiry)
- RuntimeContext card field propagation
- channel_chat card_mode delivery_target construction
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.agent_runtime.card_stream_bridge import (
    CardStreamBridge,
    get_bridge,
    register_bridge,
    unregister_bridge,
)
from app.services.agent_runtime.state import RuntimeContext


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_context(**overrides) -> RuntimeContext:
    defaults = dict(
        tenant_id="t1",
        run_id="r1",
        command_id="c1",
        executor=MagicMock(),
    )
    defaults.update(overrides)
    return RuntimeContext(**defaults)  # type: ignore[arg-type]


def _make_fake_feishu_service():
    fs = MagicMock()
    fs.create_card_entity = AsyncMock(return_value="card_001")
    fs.send_card_by_card_id = AsyncMock(return_value=None)
    fs.send_message = AsyncMock(return_value={"code": 0})
    fs.stream_card_content = AsyncMock(return_value=None)
    fs.set_card_streaming_mode = AsyncMock(return_value=None)
    fs.update_cardkit_card = AsyncMock(return_value=None)
    return fs


def _thinking_panel_content(terminal_card: dict) -> str:
    """Extract the 「💭 思考过程」 panel markdown content from a terminal card."""
    panel = next(
        el for el in terminal_card["body"]["elements"]
        if el.get("tag") == "collapsible_panel"
        and "思考过程" in (el.get("header") or {}).get("title", {}).get("content", "")
    )
    return panel["elements"][0]["content"]


# ---------------------------------------------------------------------------
# Bridge registry
# ---------------------------------------------------------------------------

class TestBridgeRegistry:
    def test_register_and_get(self):
        fs = _make_fake_feishu_service()
        b = CardStreamBridge(feishu_service=fs, app_id="a", app_secret="s",
                            receive_id="ou", receive_id_type="open_id",
                            agent_name="Test", run_id="run-1")
        register_bridge("run-1", b)
        assert get_bridge("run-1") is b
        unregister_bridge("run-1")
        assert get_bridge("run-1") is None

    def test_unknown_bridge_returns_none(self):
        assert get_bridge("no-such-run") is None

    def test_replace_same_key(self):
        fs = _make_fake_feishu_service()
        b1 = CardStreamBridge(feishu_service=fs, app_id="a", app_secret="s",
                             receive_id="ou", receive_id_type="open_id",
                             agent_name="A", run_id="run-1")
        b2 = CardStreamBridge(feishu_service=fs, app_id="a", app_secret="s",
                             receive_id="ou", receive_id_type="open_id",
                             agent_name="B", run_id="run-1")
        register_bridge("run-1", b1)
        register_bridge("run-1", b2)
        assert get_bridge("run-1") is b2
        unregister_bridge("run-1")

    def test_unregister_idempotent(self):
        unregister_bridge("no-such-run")  # should not raise


# ---------------------------------------------------------------------------
# CardStreamBridge
# ---------------------------------------------------------------------------

class TestCardStreamBridge:
    @pytest.mark.asyncio
    async def test_start_creates_card_and_enters_streaming(self):
        fs = _make_fake_feishu_service()
        b = CardStreamBridge(feishu_service=fs, app_id="aid", app_secret="sec",
                            receive_id="ou", receive_id_type="open_id",
                            agent_name="TestBot", run_id="run-1")
        await b.start()
        fs.create_card_entity.assert_awaited_once()
        fs.send_card_by_card_id.assert_awaited_once()
        assert b.card_id == "card_001"
        assert b._streaming is True
        assert b._state == "streaming"

    @pytest.mark.asyncio
    async def test_push_text_streams_content(self):
        fs = _make_fake_feishu_service()
        b = CardStreamBridge(feishu_service=fs, app_id="aid", app_secret="sec",
                            receive_id="ou", receive_id_type="open_id",
                            agent_name="TestBot", run_id="run-2")
        await b.start()
        await b.push_text("Hello world")
        # Should have streamed at least once
        assert fs.stream_card_content.await_count >= 1

    @pytest.mark.asyncio
    async def test_push_text_skips_when_not_streaming(self):
        fs = _make_fake_feishu_service()
        b = CardStreamBridge(feishu_service=fs, app_id="aid", app_secret="sec",
                            receive_id="ou", receive_id_type="open_id",
                            agent_name="TestBot", run_id="run-3")
        # No start() called
        await b.push_text("Hello")
        fs.stream_card_content.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_start_tool_end_tool_lifecycle(self):
        fs = _make_fake_feishu_service()
        b = CardStreamBridge(feishu_service=fs, app_id="aid", app_secret="sec",
                            receive_id="ou", receive_id_type="open_id",
                            agent_name="TestBot", run_id="run-4")
        await b.start()
        b.start_tool("tc1", "search")
        assert "tc1" in b._tool_states
        assert b._tool_states["tc1"]["status"] == "running"
        b.end_tool("tc1")
        # Give the background task a moment
        await asyncio.sleep(0.1)
        assert b._tool_states["tc1"]["status"] == "complete"

    @pytest.mark.asyncio
    async def test_end_tool_error_status(self):
        fs = _make_fake_feishu_service()
        b = CardStreamBridge(feishu_service=fs, app_id="aid", app_secret="sec",
                            receive_id="ou", receive_id_type="open_id",
                            agent_name="TestBot", run_id="run-5")
        await b.start()
        b.start_tool("tc2", "dangerous_op")
        b.end_tool("tc2", is_error=True)
        await asyncio.sleep(0.1)
        assert b._tool_states["tc2"]["status"] == "error"

    @pytest.mark.asyncio
    async def test_finalize_closes_streaming_and_updates_card(self):
        fs = _make_fake_feishu_service()
        b = CardStreamBridge(feishu_service=fs, app_id="aid", app_secret="sec",
                            receive_id="ou", receive_id_type="open_id",
                            agent_name="TestBot", run_id="run-6")
        await b.start()
        await b.finalize("Final answer text")
        fs.set_card_streaming_mode.assert_awaited()
        fs.update_cardkit_card.assert_awaited()
        assert b._streaming is False
        assert b._finalized is True

    @pytest.mark.asyncio
    async def test_finalize_idempotent(self):
        fs = _make_fake_feishu_service()
        b = CardStreamBridge(feishu_service=fs, app_id="aid", app_secret="sec",
                            receive_id="ou", receive_id_type="open_id",
                            agent_name="TestBot", run_id="run-7")
        await b.start()
        await b.finalize("First")
        call_count = fs.update_cardkit_card.await_count
        await b.finalize("Second")
        # Should not call again
        assert fs.update_cardkit_card.await_count == call_count

    @pytest.mark.asyncio
    async def test_push_text_skips_empty_and_whitespace_content(self):
        """Empty pushes get rejected by CardKit (99992402) — never send them."""
        fs = _make_fake_feishu_service()
        b = CardStreamBridge(feishu_service=fs, app_id="aid", app_secret="sec",
                            receive_id="ou", receive_id_type="open_id",
                            agent_name="TestBot", run_id="run-empty-push")
        await b.start()
        fs.stream_card_content.reset_mock()

        await b.push_text("")
        await b.push_text("   \n  ")
        fs.stream_card_content.assert_not_awaited()

        await b.push_text("Real content")
        assert fs.stream_card_content.await_count >= 1

    @pytest.mark.asyncio
    async def test_finalize_with_empty_answer_builds_non_blank_terminal_card(self):
        """Reasoning-only turns must never leave the final card blank."""
        fs = _make_fake_feishu_service()
        b = CardStreamBridge(feishu_service=fs, app_id="aid", app_secret="sec",
                            receive_id="ou", receive_id_type="open_id",
                            agent_name="TestBot", run_id="run-empty-final")
        await b.start()
        await b.finalize("")

        fs.update_cardkit_card.assert_awaited()
        args = fs.update_cardkit_card.await_args.args
        terminal_card = args[3]
        body_elements = terminal_card["body"]["elements"]
        text_element = next(
            el for el in body_elements if el.get("tag") == "markdown" and el.get("text_align") is None
        )
        assert "任务已完成" in text_element["content"]

    @pytest.mark.asyncio
    async def test_abort_delegates_to_finalize(self):
        fs = _make_fake_feishu_service()
        b = CardStreamBridge(feishu_service=fs, app_id="aid", app_secret="sec",
                            receive_id="ou", receive_id_type="open_id",
                            agent_name="TestBot", run_id="run-8")
        await b.start()
        await b.abort("Interrupted")
        assert b._finalized is True
        # abort() calls finalize() which closes streaming
        fs.set_card_streaming_mode.assert_awaited()

    @pytest.mark.asyncio
    async def test_finalize_fallback_to_text_on_error(self):
        fs = _make_fake_feishu_service()
        fs.set_card_streaming_mode = AsyncMock(side_effect=RuntimeError("streaming error"))
        b = CardStreamBridge(feishu_service=fs, app_id="aid", app_secret="sec",
                            receive_id="ou", receive_id_type="open_id",
                            agent_name="TestBot", run_id="run-9")
        await b.start()
        await b.finalize("Fallback text")
        # Should have sent a plain-text message as L2 fallback
        fs.send_message.assert_awaited()

    @pytest.mark.asyncio
    async def test_push_text_truncates_long_content(self):
        fs = _make_fake_feishu_service()
        b = CardStreamBridge(feishu_service=fs, app_id="aid", app_secret="sec",
                            receive_id="ou", receive_id_type="open_id",
                            agent_name="TestBot", run_id="run-10")
        await b.start()
        long_text = "x" * 200_000
        await b.push_text(long_text)
        # Should have truncated — the streamed content should be ≤ ~100K
        call_arg = fs.stream_card_content.call_args
        if call_arg:
            content = call_arg[0][3]  # 4th positional arg is content
            assert len(content) <= 101_000

    @pytest.mark.asyncio
    async def test_streaming_timeout_self_healing(self):
        """Verify that code=200850 triggers streaming mode re-enable and retry."""
        fs = _make_fake_feishu_service()
        # First stream call fails with timeout code
        call_count = [0]

        async def _fail_then_pass(_app_id, _secret, card_id, elem_id, content, seq):
            call_count[0] += 1
            if call_count[0] == 1:
                exc = RuntimeError("timeout")
                exc.code = 200850  # type: ignore[attr-defined]
                raise exc
            return None

        fs.stream_card_content = AsyncMock(side_effect=_fail_then_pass)
        b = CardStreamBridge(feishu_service=fs, app_id="aid", app_secret="sec",
                            receive_id="ou", receive_id_type="open_id",
                            agent_name="TestBot", run_id="run-11")
        await b.start()
        await b.push_text("Hello after timeout")
        # set_card_streaming_mode should have been called for re-enable
        assert fs.set_card_streaming_mode.await_count >= 1
        # stream_card_content should have been called at least twice (fail + retry)
        assert fs.stream_card_content.await_count >= 2

    @pytest.mark.asyncio
    async def test_thinking_panel_truncates_long_tail_with_note(self):
        """超长思考原文只保留尾部完整行并标注截断，防打爆 CardKit 载荷上限."""
        fs = _make_fake_feishu_service()
        b = CardStreamBridge(feishu_service=fs, app_id="aid", app_secret="sec",
                             receive_id="ou", receive_id_type="open_id",
                             agent_name="TestBot", run_id="run-think-long")
        await b.start()
        lines = ["开头第一行-不应出现在终版卡片"] + [
            f"第{i}轮思考-完整行内容" for i in range(900)
        ]
        thinking = "\n".join(lines)
        assert len(thinking) > 8_000  # 前置条件：确为超长内容
        await b.push_thinking(thinking)
        await b.finalize("终版答案")

        terminal_card = fs.update_cardkit_card.await_args.args[3]
        content = _thinking_panel_content(terminal_card)
        kept_lines = content.split("\n")

        # 截断提示在，且保留的是尾部（最新几轮）完整行
        assert kept_lines[0] == "> ⚠️ 思考内容过长，仅展示最后一段"
        assert "> 第899轮思考-完整行内容" in kept_lines
        assert "开头第一行" not in content
        # 保留的每一行都是原文的完整行（非拦腰截断的碎片）
        original_quoted = {f"> {line}" for line in lines}
        assert set(kept_lines[1:]) <= original_quoted
        # 原文保留上限 8000；quote 后整块载荷仍有界（上限+行前缀+提示行）
        raw_kept = "\n".join(line[2:] for line in kept_lines[1:])
        assert len(raw_kept) <= 8_000
        assert len(content) <= 12_000

    @pytest.mark.asyncio
    async def test_thinking_panel_short_content_no_truncation(self):
        """未超限的思考原文完整展示，不标注截断."""
        fs = _make_fake_feishu_service()
        b = CardStreamBridge(feishu_service=fs, app_id="aid", app_secret="sec",
                             receive_id="ou", receive_id_type="open_id",
                             agent_name="TestBot", run_id="run-think-short")
        await b.start()
        await b.push_thinking("简短思考")
        await b.finalize("终版答案")

        terminal_card = fs.update_cardkit_card.await_args.args[3]
        content = _thinking_panel_content(terminal_card)
        assert content == "> 简短思考"
        assert "截断" not in content

    @pytest.mark.asyncio
    async def test_thinking_panel_at_limit_no_truncation(self):
        """恰好 8000 字符的思考原文不截断（below/at/above 三点覆盖）."""
        fs = _make_fake_feishu_service()
        b = CardStreamBridge(feishu_service=fs, app_id="aid", app_secret="sec",
                             receive_id="ou", receive_id_type="open_id",
                             agent_name="TestBot", run_id="run-think-limit")
        await b.start()
        at_limit = "思" * 8_000
        await b.push_thinking(at_limit)
        await b.finalize("终版答案")

        terminal_card = fs.update_cardkit_card.await_args.args[3]
        content = _thinking_panel_content(terminal_card)
        assert content == "> " + at_limit
        assert "截断" not in content

    # ---- withdraw (resume 场景撤回) ---------------------------------------

    @pytest.mark.asyncio
    async def test_start_captures_message_id_from_send_response(self):
        """message_id 从响应体 data 层提取（飞书响应为 {"data": {"message_id"}}）。"""
        fs = _make_fake_feishu_service()
        fs.send_card_by_card_id = AsyncMock(
            return_value={"code": 0, "data": {"message_id": "om_card_1"}},
        )
        b = CardStreamBridge(feishu_service=fs, app_id="aid", app_secret="sec",
                             receive_id="ou", receive_id_type="open_id",
                             agent_name="TestBot", run_id="run-msgid")
        await b.start()
        assert b.message_id == "om_card_1"

    @pytest.mark.asyncio
    async def test_withdraw_recalls_sent_card(self):
        fs = _make_fake_feishu_service()
        fs.send_card_by_card_id = AsyncMock(
            return_value={"code": 0, "data": {"message_id": "om_card_1"}},
        )
        fs.delete_message = AsyncMock(return_value={"code": 0})
        b = CardStreamBridge(feishu_service=fs, app_id="aid", app_secret="sec",
                             receive_id="ou", receive_id_type="open_id",
                             agent_name="TestBot", run_id="run-withdraw")
        await b.start()
        await b.withdraw()
        fs.delete_message.assert_awaited_once_with("aid", "sec", "om_card_1")
        assert b._streaming is False
        assert b._state == "withdrawn"

    @pytest.mark.asyncio
    async def test_withdraw_waits_for_inflight_creation_then_recalls(self):
        """创建在途时撤回：等创建完成后删消息，且不渲染终版卡片。"""
        fs = _make_fake_feishu_service()
        send_started = asyncio.Event()

        async def slow_send(*_args, **_kwargs):
            send_started.set()
            await asyncio.sleep(0.05)
            return {"code": 0, "data": {"message_id": "om_card_1"}}

        fs.send_card_by_card_id = slow_send
        fs.delete_message = AsyncMock(return_value={"code": 0})
        b = CardStreamBridge(feishu_service=fs, app_id="aid", app_secret="sec",
                             receive_id="ou", receive_id_type="open_id",
                             agent_name="TestBot", run_id="run-inflight")
        start_task = asyncio.create_task(b.start())
        await send_started.wait()
        await b.withdraw()
        await start_task
        fs.delete_message.assert_awaited_once_with("aid", "sec", "om_card_1")
        # 撤回的卡片不得渲染终版卡片（finalize 未发生）
        fs.update_cardkit_card.assert_not_awaited()
        fs.set_card_streaming_mode.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_withdraw_before_start_prevents_card_creation(self):
        """start() 尚未执行时撤回：建卡与发送都被跳过。"""
        fs = _make_fake_feishu_service()
        b = CardStreamBridge(feishu_service=fs, app_id="aid", app_secret="sec",
                             receive_id="ou", receive_id_type="open_id",
                             agent_name="TestBot", run_id="run-early")
        await b.withdraw()
        await b.start()
        fs.create_card_entity.assert_not_awaited()
        fs.send_card_by_card_id.assert_not_awaited()
        assert b._state == "withdrawn"

    @pytest.mark.asyncio
    async def test_withdraw_recall_failure_is_contained(self):
        """撤回失败只记日志、不抛出 —— resume 流程继续。"""
        fs = _make_fake_feishu_service()
        fs.send_card_by_card_id = AsyncMock(
            return_value={"code": 0, "data": {"message_id": "om_card_1"}},
        )
        fs.delete_message = AsyncMock(side_effect=RuntimeError("recall denied"))
        b = CardStreamBridge(feishu_service=fs, app_id="aid", app_secret="sec",
                             receive_id="ou", receive_id_type="open_id",
                             agent_name="TestBot", run_id="run-recall-fail")
        await b.start()
        await b.withdraw()  # 不抛
        assert b._streaming is False
        assert b._state == "withdrawn"


# ---------------------------------------------------------------------------
# RuntimeContext card fields
# ---------------------------------------------------------------------------

class TestRuntimeContextCardFields:
    def test_card_fields_default_to_falsy(self):
        ctx = _make_context()
        assert ctx.card_mode is False
        assert ctx.card_bridge_key == ""
        assert ctx.card_app_id == ""
        assert ctx.card_app_secret == ""
        assert ctx.card_receive_id == ""
        assert ctx.card_receive_id_type == ""

    def test_all_card_fields_are_present(self):
        ctx = _make_context(
            card_mode=True,
            card_bridge_key="key-1",
            card_app_id="app-1",
            card_app_secret="s",
            card_receive_id="ou_123",
            card_receive_id_type="open_id",
        )
        assert ctx.card_mode is True
        assert ctx.card_bridge_key == "key-1"
        assert ctx.card_app_id == "app-1"
        assert ctx.card_app_secret == "s"
        assert ctx.card_receive_id == "ou_123"
        assert ctx.card_receive_id_type == "open_id"

    def test_runtime_context_is_frozen(self):
        ctx = _make_context(card_mode=True)
        with pytest.raises(Exception):
            ctx.card_mode = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# channel_chat card_mode delivery_target
# ---------------------------------------------------------------------------

class TestChannelChatCardMode:
    def test_card_config_built_with_app_id_and_secret(self):
        """Verify that _card_config is correctly built in channel_delivery_target."""
        target = {
            "receive_id": "ou_test",
            "receive_id_type": "open_id",
            "_card_config": {
                "app_id": "cli_abc",
                "app_secret": "sec_xyz",
            },
        }
        assert target["_card_config"]["app_id"] == "cli_abc"
        assert target["_card_config"]["app_secret"] == "sec_xyz"
        assert target["receive_id"] == "ou_test"

    def test_delivery_target_without_card_mode(self):
        """No _card_config when card_mode is not set."""
        target = {
            "receive_id": "ou_test",
            "receive_id_type": "open_id",
        }
        assert "_card_config" not in target


# ---------------------------------------------------------------------------
# run with: cd backend && pytest tests/test_feishu_card_mode.py -v
# ---------------------------------------------------------------------------
