"""Regression tests for agent settings update validation after the main merge.

The v1.11 merge introduced two validation changes that broke saves for
pre-existing agents:

1. ``AgentUpdate.max_tool_rounds`` gained an upper bound while legacy agents
   may hold values above it — the endpoint now clamps to the platform cap
   (10000) instead of rejecting, with ``_clamped_fields`` feedback (same
   pattern as tenant floors).
2. ``AgentUpdate.timezone`` gained an IANA validator while the UI sends an
   empty string for "inherit the company default" — empty now normalizes
   to None.
"""

import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.api import agents as agents_api
from app.schemas.schemas import AgentUpdate


def _make_agent(**overrides):
    values = {
        "id": uuid.uuid4(),
        "name": "Android Engineer",
        "tenant_id": None,
        "creator_id": uuid.uuid4(),
        "max_tool_rounds": 10000,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class RecordingDB:
    """Minimal async session double: only flush() is expected."""

    def __init__(self):
        self.execute_calls = 0

    async def flush(self):
        return None

    async def execute(self, *_args, **_kwargs):  # pragma: no cover - not expected
        self.execute_calls += 1
        raise AssertionError("unexpected execute() call")


async def _call_update_agent(monkeypatch, data: AgentUpdate, agent):
    async def fake_check_agent_access(_db, _user, _agent_id):
        return agent, SimpleNamespace()

    async def fake_agent_to_out(_db, updated_agent, _user_id):
        return SimpleNamespace(
            model_dump=lambda: {
                "id": str(updated_agent.id),
                "max_tool_rounds": updated_agent.max_tool_rounds,
            }
        )

    monkeypatch.setattr(agents_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(agents_api, "is_agent_creator", lambda _u, _a: True)
    monkeypatch.setattr(agents_api, "_agent_to_out", fake_agent_to_out)

    return await agents_api.update_agent(
        agent_id=agent.id,
        data=data,
        current_user=SimpleNamespace(id=uuid.uuid4(), role="user", tenant_id=None),
        db=RecordingDB(),
    )


# ─── schema: timezone normalization ────────────────────────────────────────


def test_timezone_empty_string_normalizes_to_none():
    update = AgentUpdate(timezone="")
    assert update.model_dump(exclude_unset=True)["timezone"] is None


def test_timezone_valid_iana_name_kept():
    update = AgentUpdate(timezone="Asia/Shanghai")
    assert update.model_dump(exclude_unset=True)["timezone"] == "Asia/Shanghai"


def test_timezone_invalid_name_rejected():
    with pytest.raises(ValidationError):
        AgentUpdate(timezone="Not/AZone")


# ─── endpoint: max_tool_rounds platform cap ────────────────────────────────


@pytest.mark.asyncio
async def test_update_agent_clamps_above_cap_max_tool_rounds(monkeypatch):
    agent = _make_agent(max_tool_rounds=20000)
    result = await _call_update_agent(monkeypatch, AgentUpdate(max_tool_rounds=20000), agent)

    assert agent.max_tool_rounds == 10000
    assert result["_clamped_fields"] == [
        {
            "field": "max_tool_rounds",
            "requested": 20000,
            "applied": 10000,
            "reason": "platform_cap",
        }
    ]


@pytest.mark.asyncio
async def test_update_agent_leaves_in_range_max_tool_rounds_untouched(monkeypatch):
    agent = _make_agent(max_tool_rounds=10000)
    result = await _call_update_agent(monkeypatch, AgentUpdate(max_tool_rounds=10000), agent)

    assert agent.max_tool_rounds == 10000
    assert "_clamped_fields" not in result
