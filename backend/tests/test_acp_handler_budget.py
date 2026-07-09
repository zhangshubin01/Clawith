"""AcpHandler 超时优雅收尾消息格式。"""
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "/Users/shubinzhang/Documents/agent/Clawith/backend")

from app.plugins.clawith_acp.acp_handler import AcpHandler


@pytest.mark.asyncio
async def test_terminate_turn_gracefully_pushes_partial_notice():
    h = AcpHandler.__new__(AcpHandler)
    h.session_id = "sess-1"
    h.agent_id = "agent-1"
    h.user_id = "user-1"
    h._streamed_reply_parts = ["hello"]
    h._push_chunk = AsyncMock()
    h._flush_chunk_buffer = AsyncMock()
    h._persist_streamed_reply_fallback = AsyncMock()
    h._send_notification = AsyncMock()

    stats = {"tools_completed": 5}
    await h._terminate_turn_gracefully(
        "compute_exceeded",
        stats,
        "user msg",
        running_tool_ids=["call_1"],
    )

    h._push_chunk.assert_awaited()
    text = h._push_chunk.await_args[0][0]
    assert "计算预算超时" in text
    assert "5" in text
    h._persist_streamed_reply_fallback.assert_awaited_once()
    h._send_notification.assert_awaited()
