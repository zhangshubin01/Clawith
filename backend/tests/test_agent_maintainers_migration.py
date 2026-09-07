"""Deployment contract for the agent_maintainers table migration (f077)."""

from __future__ import annotations

import importlib.util
from pathlib import Path


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "v1_11_4_f077_add_agent_maintainers.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "add_agent_maintainers",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeInspector:
    def __init__(self, *, present: bool) -> None:
        self._tables = {"agent_maintainers"} if present else set()

    def get_table_names(self) -> list[str]:
        return list(self._tables)


def _install(monkeypatch, migration, *, present: bool):
    monkeypatch.setattr(
        migration.sa,
        "inspect",
        lambda bind: _FakeInspector(present=present),
    )
    monkeypatch.setattr(migration.op, "get_bind", lambda: object())


def _record(monkeypatch, migration) -> list[tuple[str, tuple, dict]]:
    calls: list[tuple[str, tuple, dict]] = []
    for attr in ("create_table", "create_index", "drop_index", "drop_table"):
        monkeypatch.setattr(
            migration.op,
            attr,
            lambda *a, _a=attr, **k: calls.append((_a, a, k)),
        )
    return calls


def test_revision_follows_no_progress_enum_head() -> None:
    migration = _load_migration()

    assert migration.revision == "f077_add_agent_maintainers"
    assert migration.down_revision == "f076_add_no_progress_enum"


def test_upgrade_creates_table_and_index_when_absent(monkeypatch) -> None:
    migration = _load_migration()
    _install(monkeypatch, migration, present=False)
    calls = _record(monkeypatch, migration)

    migration.upgrade()

    kinds = [c[0] for c in calls]
    assert kinds == ["create_table", "create_index"]
    assert calls[0][1][0] == "agent_maintainers"
    assert calls[1][1][0] == "ix_agent_maintainers_agent_id"


def test_upgrade_is_noop_when_table_exists(monkeypatch) -> None:
    migration = _load_migration()
    _install(monkeypatch, migration, present=True)
    calls = _record(monkeypatch, migration)

    migration.upgrade()

    assert calls == []


def test_downgrade_drops_index_then_table_when_present(monkeypatch) -> None:
    migration = _load_migration()
    _install(monkeypatch, migration, present=True)
    calls = _record(monkeypatch, migration)

    migration.downgrade()

    kinds = [c[0] for c in calls]
    assert kinds == ["drop_index", "drop_table"]
    assert calls[0][1][0] == "ix_agent_maintainers_agent_id"
    assert calls[1][1][0] == "agent_maintainers"


def test_downgrade_is_noop_when_table_absent(monkeypatch) -> None:
    migration = _load_migration()
    _install(monkeypatch, migration, present=False)
    calls = _record(monkeypatch, migration)

    migration.downgrade()

    assert calls == []
