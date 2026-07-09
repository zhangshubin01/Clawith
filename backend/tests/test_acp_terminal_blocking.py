"""blocking 路径 — wait_for_exit + release + RPC timeout 按 policy。"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.plugins.clawith_acp.terminal_policy import resolve_terminal_policy
from app.plugins.clawith_acp.tool_bridge import _try_acp_terminal


@pytest.mark.asyncio
async def test_blocking_wait_release_and_log(caplog):
    policy = resolve_terminal_policy("./gradlew test")
    handler = MagicMock()
    handler.session_id = "sess-1"
    handler.conn_id = "c1"
    handler.agent_id = None
    handler.user_id = None
    handler._cwd = "/proj"

    calls: list[tuple[str, dict, float | None]] = []

    async def fake_send(method, params, timeout=None):
        calls.append((method, params, timeout))
        if method == "terminal/create":
            return {"terminalId": "term-abc"}
        if method == "terminal/wait_for_exit":
            return {"exitCode": 0}
        if method == "terminal/output":
            return {"output": "BUILD SUCCESSFUL"}
        if method == "terminal/release":
            return {"ok": True}
        return {}

    handler.send_request = AsyncMock(side_effect=fake_send)

    result = await _try_acp_terminal(
            {"command": "./gradlew test"},
            handler,
            policy=policy,
    )

    assert result == "BUILD SUCCESSFUL"
    assert ("terminal/wait_for_exit", {"sessionId": "sess-1", "terminalId": "term-abc"}, 330.0) in calls
    assert any(c[0] == "terminal/release" for c in calls)
    assert any(c[0] == "terminal/wait_for_exit" for c in calls)
