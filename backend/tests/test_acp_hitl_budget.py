"""HITL 等待期间 compute 预算不被 permission 等待消耗。"""
import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, "/Users/shubinzhang/Documents/agent/Clawith/backend")

from app.plugins.clawith_acp.turn_budget import TurnBudget, set_turn_budget
from app.plugins.clawith_acp.tool_bridge import _send_with_budget


@pytest.mark.asyncio
async def test_permission_wait_extends_compute_deadline():
    budget = TurnBudget(workflow_seconds=7200, compute_seconds=60)
    set_turn_budget(budget)
    before = budget.remaining_compute()
    handler = MagicMock()
    handler.send_request = AsyncMock(return_value={"ok": True})

    async def slow_send(*_a, **_k):
        await asyncio.sleep(0.08)
        return {"ok": True}

    handler.send_request.side_effect = slow_send
    await _send_with_budget(handler, "fs/safe_delete", {}, 120.0)
    after = budget.remaining_compute()
    # HITL 等待 80ms 期间 compute 应暂停，剩余预算不应明显减少
    assert after >= before - 0.02
    set_turn_budget(None)
