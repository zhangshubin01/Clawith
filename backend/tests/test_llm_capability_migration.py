"""Static tests for the LLM runtime-capability Alembic revision."""

from importlib import util
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.models.llm import LLMModel


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "202607131920_add_llm_runtime_capabilities.py"
)


def _load_migration():
    spec = util.spec_from_file_location("add_llm_runtime_capabilities", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MetadataInspector:
    def __init__(
        self,
        *,
        include_capability_columns: bool = True,
        include_checks: bool = True,
    ):
        table = LLMModel.__table__
        capability_names = {
            "context_window_tokens",
            "context_window_tokens_override",
            "max_input_tokens",
            "max_input_tokens_override",
            "capability_source",
            "capability_checked_at",
        }
        self.columns = [
            {
                "name": column.name,
                "type": column.type,
                "nullable": column.nullable,
                "default": (
                    str(column.server_default.arg)
                    if column.server_default is not None
                    else None
                ),
            }
            for column in table.columns
            if include_capability_columns or column.name not in capability_names
        ]
        self.checks = (
            [
                {"name": constraint.name, "sqltext": str(constraint.sqltext)}
                for constraint in table.constraints
                if isinstance(constraint, sa.CheckConstraint)
            ]
            if include_checks
            else []
        )

    @staticmethod
    def get_table_names() -> list[str]:
        return ["llm_models"]

    def get_columns(self, _table_name: str) -> list[dict[str, object]]:
        return self.columns

    def get_check_constraints(self, _table_name: str) -> list[dict[str, object]]:
        return self.checks


class ScalarResult:
    def __init__(self, value: int):
        self.value = value

    def scalar_one(self) -> int:
        return self.value


class RecordingBind:
    def __init__(self, counts: list[int] | None = None):
        self.counts = list(counts or [])
        self.statements: list[str] = []

    def execute(self, statement):
        self.statements.append(str(statement))
        return ScalarResult(self.counts.pop(0) if self.counts else 0)


def test_revision_follows_unified_chat_schema():
    migration = _load_migration()

    assert migration.revision == "add_llm_runtime_capabilities"
    assert migration.down_revision == "unify_chat_schema"
    assert "max_output_tokens" not in migration._COLUMN_FACTORIES


def test_fresh_current_metadata_is_a_noop(monkeypatch):
    migration = _load_migration()
    inspector = MetadataInspector()
    writes: list[str] = []

    monkeypatch.setattr(migration.op, "get_bind", lambda: object())
    monkeypatch.setattr(migration.sa, "inspect", lambda _bind: inspector)
    monkeypatch.setattr(
        migration.op,
        "add_column",
        lambda table, column: writes.append(f"add:{table}.{column.name}"),
    )
    monkeypatch.setattr(
        migration.op,
        "create_check_constraint",
        lambda name, *_args, **_kwargs: writes.append(f"check:{name}"),
    )

    migration.upgrade()

    assert writes == []


def test_legacy_schema_adds_six_columns_and_five_checks(monkeypatch):
    migration = _load_migration()
    legacy = MetadataInspector(include_capability_columns=False, include_checks=False)
    expanded = MetadataInspector(include_checks=False)
    inspectors = iter((legacy, expanded))
    added_columns: list[str] = []
    added_checks: list[str] = []

    monkeypatch.setattr(migration.op, "get_bind", lambda: object())
    monkeypatch.setattr(migration.sa, "inspect", lambda _bind: next(inspectors))
    monkeypatch.setattr(
        migration.op,
        "add_column",
        lambda _table, column: added_columns.append(column.name),
    )
    monkeypatch.setattr(
        migration.op,
        "create_check_constraint",
        lambda name, *_args, **_kwargs: added_checks.append(name),
    )

    migration.upgrade()

    assert added_columns == list(migration._COLUMN_FACTORIES)
    assert set(added_checks) == {*migration._POSITIVE_CHECKS, migration._SOURCE_CHECK}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("nullable", False),
        ("type", sa.BigInteger()),
        ("default", "8192"),
    ],
)
def test_existing_capability_column_must_match_final_signature(
    monkeypatch, field, value
):
    migration = _load_migration()
    inspector = MetadataInspector()
    column = next(
        item for item in inspector.columns if item["name"] == "max_input_tokens"
    )
    column[field] = value

    monkeypatch.setattr(migration.op, "get_bind", lambda: object())
    monkeypatch.setattr(migration.sa, "inspect", lambda _bind: inspector)

    with pytest.raises(RuntimeError, match="max_input_tokens has schema"):
        migration.upgrade()


@pytest.mark.parametrize(
    ("check_name", "bad_expression"),
    [
        (
            "ck_llm_models_max_input_tokens_positive",
            "max_input_tokens IS NULL OR max_input_tokens >= 0",
        ),
        (
            "ck_llm_models_capability_source",
            "capability_source IS NULL OR capability_source IN ('manual', 'other')",
        ),
    ],
)
def test_incompatible_same_name_check_is_rejected(
    monkeypatch, check_name, bad_expression
):
    migration = _load_migration()
    inspector = MetadataInspector()
    target = next(item for item in inspector.checks if item["name"] == check_name)
    target["sqltext"] = bad_expression

    monkeypatch.setattr(migration.op, "get_bind", lambda: object())
    monkeypatch.setattr(migration.sa, "inspect", lambda _bind: inspector)

    with pytest.raises(RuntimeError, match="Existing check"):
        migration.upgrade()


def test_source_check_accepts_postgresql_any_and_casts():
    migration = _load_migration()

    migration._validate_source_check(
        "(capability_source IS NULL) OR ((capability_source)::text = ANY "
        "(ARRAY['manual'::character varying, 'provider_api'::character varying, "
        "'builtin_registry'::character varying, "
        "'runtime_config'::character varying]::text[]))"
    )


def test_downgrade_refuses_populated_capability_metadata(monkeypatch):
    migration = _load_migration()
    inspector = MetadataInspector()
    bind = RecordingBind(counts=[0, 1])
    destructive_writes: list[str] = []

    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(migration.sa, "inspect", lambda _bind: inspector)
    monkeypatch.setattr(
        migration.op,
        "drop_column",
        lambda table, column: destructive_writes.append(f"drop:{table}.{column}"),
    )

    with pytest.raises(RuntimeError, match="capability metadata is populated"):
        migration.downgrade()

    assert bind.statements[0] == "LOCK TABLE llm_models IN ACCESS EXCLUSIVE MODE"
    assert "max_output_tokens" not in bind.statements[1]
    assert destructive_writes == []


def test_empty_downgrade_drops_only_capability_checks_and_columns(monkeypatch):
    migration = _load_migration()
    inspector = MetadataInspector()
    bind = RecordingBind()
    dropped_checks: list[str] = []
    dropped_columns: list[str] = []

    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(migration.sa, "inspect", lambda _bind: inspector)
    monkeypatch.setattr(
        migration.op,
        "drop_constraint",
        lambda name, _table, **_kwargs: dropped_checks.append(name),
    )
    monkeypatch.setattr(
        migration.op,
        "drop_column",
        lambda _table, column: dropped_columns.append(column),
    )

    migration.downgrade()

    assert set(dropped_checks) == {*migration._POSITIVE_CHECKS, migration._SOURCE_CHECK}
    assert set(dropped_columns) == set(migration._COLUMN_FACTORIES)
    assert "max_output_tokens" not in dropped_columns
