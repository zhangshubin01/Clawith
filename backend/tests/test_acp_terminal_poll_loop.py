"""streaming poll_loop — 长时间静默不应打 DISAPPEARED 日志。"""

import asyncio
from io import StringIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from loguru import logger

from app.plugins.clawith_acp.terminal_policy import resolve_terminal_policy
from app.plugins.clawith_acp.tool_bridge import _try_acp_terminal_streaming


@pytest.mark.asyncio
async def test_silent_polls_no_disappeared_log():
    policy = resolve_terminal_policy("tail -f logs/app.log")
    handler = MagicMock()
    handler.session_id = "sess-poll"
    handler.conn_id = "c-poll"
    handler.agent_id = None
    handler.user_id = None
    handler._cwd = "/proj"
    handler._active_tasks = {}
    handler._terminal_events = {}
    handler._send_notification = AsyncMock()

    poll_count = 0
    log_buf = StringIO()
    sink_id = logger.add(log_buf, level="WARNING", format="{message}")

    async def fake_send(method, params, timeout=None):
        nonlocal poll_count
        if method == "terminal/create":
            return {"terminalId": "term-silent"}
        if method == "terminal/output":
            poll_count += 1
            if poll_count >= 16:
                return {"output": "line\n", "exitStatus": {"exitCode": 0}}
            return {"output": ""}
        if method == "terminal/release":
            return {"ok": True}
        if method == "terminal/kill":
            return {"ok": True}
        return {}

    handler.send_request = AsyncMock(side_effect=fake_send)

    try:
        with patch("app.plugins.clawith_acp.tool_bridge.asyncio.sleep", AsyncMock(return_value=None)):
            result = await _try_acp_terminal_streaming(
                {"command": "tail -f logs/app.log"},
                handler,
                policy=policy,
            )

        assert result is None
        task = handler._active_tasks.get("terminal-stream-term-silent")
        assert task is not None
        try:
            await asyncio.wait_for(task, timeout=3)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        # Phase1 1 次 + poll_loop 多次静默 poll
        assert poll_count >= 7
        assert "DISAPPEARED" not in log_buf.getvalue()
    finally:
        try:
            logger.remove(sink_id)
        except ValueError:
            pass
