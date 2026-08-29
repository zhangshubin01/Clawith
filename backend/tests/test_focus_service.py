from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.services import focus_service


class _Result:
    def __init__(self, item):
        self.item = item

    def scalar_one_or_none(self):
        return self.item


@pytest.mark.asyncio
async def test_upsert_with_caller_session_refreshes_before_serializing(monkeypatch):
    """Server-generated timestamps must be loaded inside the async context."""
    item = SimpleNamespace(
        title=None,
        description="Previous description",
        status="in_progress",
        kind="normal",
        source="user",
        item_metadata={},
        completed_at=None,
    )
    events: list[str] = []

    async def flush():
        events.append("flush")

    async def refresh(value):
        assert value is item
        events.append("refresh")

    session = SimpleNamespace(
        execute=AsyncMock(return_value=_Result(item)),
        flush=flush,
        refresh=refresh,
    )
    agent_id = uuid.uuid4()
    item_key = "system:okr_reports"
    monkeypatch.setattr(focus_service, "migrate_legacy_focus_file", AsyncMock(return_value=None))
    monkeypatch.setattr(focus_service, "_serialize_focus_item", lambda value: {"key": item_key})
    monkeypatch.setattr(focus_service.focus_dao, "upsert_item", AsyncMock(return_value=item))

    result = await focus_service.upsert_focus_item(
        agent_id=agent_id,
        key=item_key,
        title=None,
        description="OKR reports",
        status="in_progress",
        kind="system",
        source="trigger",
        metadata=None,
        db=session,
    )

    assert result == {"key": item_key}
    focus_service.focus_dao.upsert_item.assert_awaited_once()


@pytest.mark.asyncio
async def test_render_focus_context_defaults_preserve_all_sections(monkeypatch):
    items = [
        {"key": "a1", "title": None, "description": "活动项", "status": "in_progress", "kind": "normal"},
        {"key": "s1", "title": None, "description": "系统项", "status": "in_progress", "kind": "system"},
        {"key": "c1", "title": None, "description": "完成项", "status": "completed", "kind": "normal"},
    ]
    monkeypatch.setattr(focus_service, "list_focus_items", AsyncMock(return_value=items))

    rendered = await focus_service.render_focus_context(uuid.uuid4())

    assert "In Progress" in rendered
    assert "System Focus" in rendered
    assert "Recently Completed" in rendered
    assert "活动项" in rendered
    assert "系统项" in rendered
    assert "完成项" in rendered


@pytest.mark.asyncio
async def test_render_focus_context_scope_and_budget_parameters(monkeypatch):
    items = [
        {"key": "a1", "title": None, "description": "第一条", "status": "in_progress", "kind": "normal"},
        {"key": "a2", "title": None, "description": "第二条", "status": "in_progress", "kind": "normal"},
        {"key": "s1", "title": None, "description": "系统项", "status": "in_progress", "kind": "system"},
        {"key": "c1", "title": None, "description": "完成项", "status": "completed", "kind": "normal"},
    ]
    monkeypatch.setattr(focus_service, "list_focus_items", AsyncMock(return_value=items))

    scoped = await focus_service.render_focus_context(
        uuid.uuid4(),
        include_system=False,
        include_completed=False,
        limit_active=1,
    )

    assert "第一条" in scoped
    assert "第二条" not in scoped
    assert "系统项" not in scoped
    assert "完成项" not in scoped
    assert "Recently Completed" not in scoped
    assert "System Focus" not in scoped

    truncated = await focus_service.render_focus_context(
        uuid.uuid4(),
        include_system=False,
        include_completed=False,
        max_chars=8,
    )

    assert truncated.endswith("...(truncated)")
    assert len(truncated.split("\n...(truncated)")[0]) == 8
