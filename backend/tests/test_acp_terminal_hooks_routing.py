"""tool_hooks — gradlew 走 blocking，tail -f 走 streaming。"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.plugins.clawith_acp.tool_bridge import current_acp_handler
from app.plugins.clawith_acp.tool_hooks import _bridge_handlers, install_acp_tool_hooks

install_acp_tool_hooks()


@pytest.mark.asyncio
async def test_gradlew_routes_to_blocking():
    handler = MagicMock()
    handler._cancel_event = None
    token = current_acp_handler.set(handler)
    try:
        acp_handler_fn = _bridge_handlers[-1]
        with patch(
            "app.plugins.clawith_acp.tool_bridge._try_acp_terminal",
            new_callable=AsyncMock,
            return_value="ok-blocking",
        ) as mock_block, patch(
            "app.plugins.clawith_acp.tool_bridge._try_acp_terminal_streaming",
            new_callable=AsyncMock,
        ) as mock_stream:
            result = await acp_handler_fn(
                "execute_command",
                {"command": "./gradlew test"},
                uuid.uuid4(),
                uuid.uuid4(),
            )
            assert result == "ok-blocking"
            mock_block.assert_awaited_once()
            mock_stream.assert_not_awaited()
    finally:
        current_acp_handler.reset(token)


@pytest.mark.asyncio
async def test_tail_f_routes_to_streaming():
    handler = MagicMock()
    handler._cancel_event = None
    token = current_acp_handler.set(handler)
    try:
        acp_handler_fn = _bridge_handlers[-1]
        with patch(
            "app.plugins.clawith_acp.tool_bridge._try_acp_terminal",
            new_callable=AsyncMock,
        ) as mock_block, patch(
            "app.plugins.clawith_acp.tool_bridge._try_acp_terminal_streaming",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_stream:
            result = await acp_handler_fn(
                "bash",
                {"command": "tail -f /tmp/x"},
                uuid.uuid4(),
                uuid.uuid4(),
            )
            assert "流式输出中" in (result or "")
            mock_stream.assert_awaited_once()
            mock_block.assert_not_awaited()
    finally:
        current_acp_handler.reset(token)
