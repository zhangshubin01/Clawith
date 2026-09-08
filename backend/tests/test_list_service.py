"""Structured list service (清单线) tests — mirrors test_focus_service style."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.services import list_service


def _item(**overrides) -> dict:
    base = {
        "id": str(uuid.uuid4()),
        "agent_id": str(uuid.uuid4()),
        "project": "mydome1",
        "key": "gitlab_ci",
        "title": "GitLab CI 缺失",
        "description": "当前无 .gitlab-ci.yml",
        "status": "pending",
        "sort_order": 92,
        "completed_at": None,
        "created_at": "2026-09-07T00:00:00+00:00",
        "updated_at": "2026-09-07T00:00:00+00:00",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------- pure helpers


def test_slugify_list_key_normalizes() -> None:
    assert list_service.slugify_list_key("GitLab CI 缺失") == "gitlab_ci_缺失"
    assert list_service.slugify_list_key("  Run Lint!!  ") == "run_lint"
    assert list_service.slugify_list_key("") == "item"


def test_is_list_file_path() -> None:
    assert list_service.is_list_file_path("memory/清单.md")
    assert list_service.is_list_file_path("清单.md")
    assert not list_service.is_list_file_path("memory/focus.md")


# ---------------------------------------------------------------- upsert / list


@pytest.mark.asyncio
async def test_upsert_normalizes_key_and_status(monkeypatch):
    item = _item(key="run_lint", status="pending")
    monkeypatch.setattr(list_service, "migrate_legacy_list_file", AsyncMock(return_value=None))
    monkeypatch.setattr(list_service, "_serialize_list_item", lambda value: {"key": value["key"]})
    monkeypatch.setattr(list_service.list_dao, "upsert_item", AsyncMock(return_value=item))

    result = await list_service.upsert_list_item(
        uuid.uuid4(),
        project="mydome1",
        key="  Run Lint!!  ",
        title="从未实际跑过 lint",
        description="项目未接入 lint",
        status="bogus_status",
    )

    assert result == {"key": "run_lint"}
    list_service.list_dao.upsert_item.assert_awaited_once()
    kwargs = list_service.list_dao.upsert_item.await_args.kwargs
    assert kwargs["key"] == "run_lint"
    assert kwargs["status"] == "pending"  # invalid status normalized to pending
    assert kwargs["project"] == "mydome1"


@pytest.mark.asyncio
async def test_complete_returns_none_when_missing(monkeypatch):
    monkeypatch.setattr(list_service, "migrate_legacy_list_file", AsyncMock(return_value=None))
    monkeypatch.setattr(list_service.list_dao, "complete_item", AsyncMock(return_value=None))

    result = await list_service.complete_list_item(
        uuid.uuid4(),
        project="mydome1",
        key="ghost",
    )

    assert result is None
