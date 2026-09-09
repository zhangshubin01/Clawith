"""Structured list service (清单线) tests — mirrors test_focus_service style."""

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


# ---------------------------------------------------------------- migration (P1)

_LEGACY_UUID_1 = "11111111-1111-1111-1111-111111111111"
_LEGACY_UUID_2 = "22222222-2222-2222-2222-222222222222"


def _capture_bulk_insert(captured: dict):
    async def _bulk(rows):
        captured["rows"] = rows
        return len(rows)

    return _bulk


@pytest.mark.asyncio
async def test_migrate_null_project_section_into_passed_project(monkeypatch):
    content = (
        f"## list:{_LEGACY_UUID_1} | project: - | 标题：待办 | 2026-09-08\n"
        "1. GitLab CI 缺失 — 当前无 .gitlab-ci.yml\n"
        "2. README 过时 — 与代码脱节\n"
    )
    monkeypatch.setattr(list_service, "_load_legacy_list_file", AsyncMock(return_value=content))
    monkeypatch.setattr(list_service.list_dao, "max_sort_order", AsyncMock(return_value=0))
    captured: dict = {}
    monkeypatch.setattr(list_service.list_dao, "bulk_insert_legacy_rows", _capture_bulk_insert(captured))

    await list_service.migrate_legacy_list_file(uuid.uuid4(), project="mydome1")

    assert captured["rows"]
    assert all(row["project"] == "mydome1" for row in captured["rows"])
    assert {row["key"] for row in captured["rows"]} == {"gitlab_ci_缺失", "readme_过时"}


@pytest.mark.asyncio
async def test_migrate_multiple_null_project_sections_assign_contiguous_sort_order(monkeypatch):
    content = (
        f"## list:{_LEGACY_UUID_1} | project: - | 标题：段一 | 2026-09-08\n"
        "1. 任务A — 描述A\n"
        "2. 任务B — 描述B\n"
        f"## list:{_LEGACY_UUID_2} | project: - | 标题：段二 | 2026-09-08\n"
        "1. 任务C — 描述C\n"
        "2. 任务D — 描述D\n"
    )
    monkeypatch.setattr(list_service, "_load_legacy_list_file", AsyncMock(return_value=content))
    monkeypatch.setattr(list_service.list_dao, "max_sort_order", AsyncMock(return_value=0))
    captured: dict = {}
    monkeypatch.setattr(list_service.list_dao, "bulk_insert_legacy_rows", _capture_bulk_insert(captured))

    await list_service.migrate_legacy_list_file(uuid.uuid4(), project="mydome1")

    orders = [row["sort_order"] for row in captured["rows"]]
    assert sorted(orders) == [1, 2, 3, 4]
    assert len(set(orders)) == 4  # no collision across merged sections


@pytest.mark.asyncio
async def test_migrate_continues_after_existing_rows(monkeypatch):
    content = (
        f"## list:{_LEGACY_UUID_1} | project: - | 标题：段一 | 2026-09-08\n"
        "1. 任务A — 描述A\n"
    )
    monkeypatch.setattr(list_service, "_load_legacy_list_file", AsyncMock(return_value=content))
    monkeypatch.setattr(list_service.list_dao, "max_sort_order", AsyncMock(return_value=5))
    captured: dict = {}
    monkeypatch.setattr(list_service.list_dao, "bulk_insert_legacy_rows", _capture_bulk_insert(captured))

    await list_service.migrate_legacy_list_file(uuid.uuid4(), project="mydome1")

    assert captured["rows"][0]["sort_order"] == 6  # after existing max


@pytest.mark.asyncio
async def test_migrate_keeps_named_project_section(monkeypatch):
    content = (
        f"## list:{_LEGACY_UUID_1} | project: emails | 标题：邮件 | 2026-09-08\n"
        "1. 合作邮件草稿 — 已定稿未发送\n"
    )
    monkeypatch.setattr(list_service, "_load_legacy_list_file", AsyncMock(return_value=content))
    monkeypatch.setattr(list_service.list_dao, "max_sort_order", AsyncMock(return_value=0))
    captured: dict = {}
    monkeypatch.setattr(list_service.list_dao, "bulk_insert_legacy_rows", _capture_bulk_insert(captured))

    await list_service.migrate_legacy_list_file(uuid.uuid4(), project="mydome1")

    assert captured["rows"][0]["project"] == "emails"  # named section keeps its own scope


@pytest.mark.asyncio
async def test_migrate_falls_back_to_legacy_when_no_project(monkeypatch):
    content = (
        f"## list:{_LEGACY_UUID_1} | project: - | 标题：待办 | 2026-09-08\n"
        "1. 任务A — 描述A\n"
    )
    monkeypatch.setattr(list_service, "_load_legacy_list_file", AsyncMock(return_value=content))
    monkeypatch.setattr(list_service.list_dao, "max_sort_order", AsyncMock(return_value=0))
    captured: dict = {}
    monkeypatch.setattr(list_service.list_dao, "bulk_insert_legacy_rows", _capture_bulk_insert(captured))

    await list_service.migrate_legacy_list_file(uuid.uuid4(), project=None)

    assert captured["rows"][0]["project"] == f"legacy:{_LEGACY_UUID_1}"


@pytest.mark.asyncio
async def test_migrate_dedupes_duplicate_key_across_sections(monkeypatch):
    content = (
        f"## list:{_LEGACY_UUID_1} | project: - | 标题：段一 | 2026-09-08\n"
        "1. Run lint — 描述A\n"
        f"## list:{_LEGACY_UUID_2} | project: - | 标题：段二 | 2026-09-08\n"
        "1. Run lint — 重复描述\n"
    )
    monkeypatch.setattr(list_service, "_load_legacy_list_file", AsyncMock(return_value=content))
    monkeypatch.setattr(list_service.list_dao, "max_sort_order", AsyncMock(return_value=0))
    captured: dict = {}
    monkeypatch.setattr(list_service.list_dao, "bulk_insert_legacy_rows", _capture_bulk_insert(captured))

    await list_service.migrate_legacy_list_file(uuid.uuid4(), project="mydome1")

    keys = [row["key"] for row in captured["rows"]]
    assert keys == ["run_lint"]  # deduped to a single row


@pytest.mark.asyncio
async def test_upsert_passes_project_to_migration(monkeypatch):
    captured: dict = {}

    async def _migrate(agent_id, db=None, *, project=None):
        captured["project"] = project
        return 0

    monkeypatch.setattr(list_service, "migrate_legacy_list_file", _migrate)
    monkeypatch.setattr(list_service.list_dao, "upsert_item", AsyncMock(return_value=_item()))
    monkeypatch.setattr(list_service, "_serialize_list_item", lambda value: value)

    await list_service.upsert_list_item(uuid.uuid4(), project="emails", key=None, description="x")

    assert captured["project"] == "emails"


@pytest.mark.asyncio
async def test_complete_passes_project_to_migration(monkeypatch):
    captured: dict = {}

    async def _migrate(agent_id, db=None, *, project=None):
        captured["project"] = project
        return 0

    monkeypatch.setattr(list_service, "migrate_legacy_list_file", _migrate)
    monkeypatch.setattr(list_service.list_dao, "complete_item", AsyncMock(return_value=None))

    await list_service.complete_list_item(uuid.uuid4(), project="emails", key="x")

    assert captured["project"] == "emails"


@pytest.mark.asyncio
async def test_list_passes_project_to_migration(monkeypatch):
    captured: dict = {}

    async def _impl(agent_id, *, project=None):
        captured["project"] = project
        return 0

    monkeypatch.setattr(list_service, "_migrate_legacy_list_file_impl", _impl)
    monkeypatch.setattr(list_service.list_dao, "list_by_project", AsyncMock(return_value=[]))

    await list_service.list_list_items(uuid.uuid4(), project="emails")

    assert captured["project"] == "emails"
