"""Deployment contract for the memory consolidation event type migration."""

from __future__ import annotations

import importlib.util
from pathlib import Path


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1] / "alembic" / "versions" / "v1_11_4_f072_memory_consolidation_event.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "memory_consolidation_event_type",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeInspector:
    def __init__(self, *, sqltext: str | None) -> None:
        self.sqltext = sqltext

    def get_check_constraints(self, _table: str) -> list[dict]:
        if self.sqltext is None:
            return []
        return [{"name": "ck_agent_run_events_event_type", "sqltext": self.sqltext}]


def test_revision_follows_the_gitlab_channel_type_head() -> None:
    migration = _load_migration()

    assert migration.revision == "f072_memory_consolidation_event"
    assert migration.down_revision == "f071_add_gitlab_channel_type"


def test_upgrade_widens_the_event_type_check_on_an_existing_database(monkeypatch) -> None:
    migration = _load_migration()
    calls: list[tuple[str, tuple]] = []
    monkeypatch.setattr(
        migration,
        "_inspector",
        lambda: FakeInspector(sqltext="event_type IN ('run_completed')"),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_constraint",
        lambda *args, **kwargs: calls.append(("drop", args + (kwargs,))),
    )
    monkeypatch.setattr(
        migration.op,
        "create_check_constraint",
        lambda *args, **kwargs: calls.append(("create", args + (kwargs,))),
    )

    migration.upgrade()

    assert [call[0] for call in calls] == ["drop", "create"]
    assert calls[0][1][0] == "ck_agent_run_events_event_type"
    assert calls[1][1][0] == "ck_agent_run_events_event_type"
    assert "memory_consolidation_skipped" in calls[1][1][2]
    assert calls[0][1][2] == {"type_": "check"}


def test_upgrade_is_a_noop_when_the_check_already_carries_the_value(monkeypatch) -> None:
    migration = _load_migration()
    monkeypatch.setattr(
        migration,
        "_inspector",
        lambda: FakeInspector(sqltext="event_type IN ('memory_consolidation_skipped')"),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_constraint",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected drop_constraint")),
    )
    monkeypatch.setattr(
        migration.op,
        "create_check_constraint",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected create_check_constraint")),
    )

    migration.upgrade()


def test_downgrade_restores_the_previous_event_type_whitelist(monkeypatch) -> None:
    migration = _load_migration()
    calls: list[tuple[str, tuple]] = []
    monkeypatch.setattr(
        migration,
        "_inspector",
        lambda: FakeInspector(sqltext="event_type IN ('memory_consolidation_skipped')"),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_constraint",
        lambda *args, **kwargs: calls.append(("drop", args + (kwargs,))),
    )
    monkeypatch.setattr(
        migration.op,
        "create_check_constraint",
        lambda *args, **kwargs: calls.append(("create", args + (kwargs,))),
    )

    migration.downgrade()

    assert [call[0] for call in calls] == ["drop", "create"]
    assert "memory_consolidation_skipped" not in calls[1][1][2]


def test_downgrade_is_a_noop_when_the_value_is_already_absent(monkeypatch) -> None:
    migration = _load_migration()
    monkeypatch.setattr(
        migration,
        "_inspector",
        lambda: FakeInspector(sqltext="event_type IN ('run_completed')"),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_constraint",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected drop_constraint")),
    )
    monkeypatch.setattr(
        migration.op,
        "create_check_constraint",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected create_check_constraint")),
    )

    migration.downgrade()
