"""cancel 应 set 全部 _terminal_events，供 streaming poll_loop 退出。"""

import asyncio
from unittest.mock import MagicMock

import pytest

from app.plugins.clawith_acp.acp_handler import AcpHandler


@pytest.mark.asyncio
async def test_handle_cancel_sets_terminal_events():
    ws = MagicMock()
    handler = AcpHandler(ws, "user-1")
    handler._cancel_event = asyncio.Event()
    ev1 = asyncio.Event()
    ev2 = asyncio.Event()
    handler._terminal_events = {"t1": ev1, "t2": ev2}

    await handler._handle_cancel({})

    assert handler._cancel_event.is_set()
    assert ev1.is_set()
    assert ev2.is_set()
