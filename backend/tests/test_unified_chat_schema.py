"""Static contracts for the forced unified-chat schema migration."""

from importlib import util
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "202607131910_unify_chat_schema.py"
)


def _load_migration():
    spec = util.spec_from_file_location("unify_chat_schema", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _constraint_names(
    table: sa.Table, constraint_type: type[sa.Constraint]
) -> set[str | None]:
    return {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, constraint_type)
    }


class FinalMetadataInspector:
    """Inspector double for a database created from the current ORM metadata."""

    def __init__(
        self,
        *,
        postgres_casts: bool = False,
        legacy_message_identity_nullability: bool = False,
    ):
        self.tables = {
            ChatSession.__table__.name: ChatSession.__table__,
            ChatMessage.__table__.name: ChatMessage.__table__,
        }
        self.postgres_casts = postgres_casts
        self.legacy_message_identity_nullability = legacy_message_identity_nullability

    def get_table_names(self) -> list[str]:
        return [*self.tables, "groups"]

    def get_columns(self, table_name: str) -> list[dict[str, object]]:
        columns = [
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
            for column in self.tables[table_name].columns
        ]
        if table_name == "chat_messages" and self.legacy_message_identity_nullability:
            for column in columns:
                if column["name"] in {"agent_id", "user_id"}:
                    column["nullable"] = False
        return columns

    def get_foreign_keys(self, table_name: str) -> list[dict[str, object]]:
        constraints = []
        for constraint in self.tables[table_name].constraints:
            if not isinstance(constraint, sa.ForeignKeyConstraint):
                continue
            target = constraint.elements[0].target_fullname.rsplit(".", 1)
            constraints.append(
                {
                    "name": constraint.name,
                    "constrained_columns": [
                        element.parent.name for element in constraint.elements
                    ],
                    "referred_table": target[0],
                    "referred_columns": [
                        element.target_fullname.rsplit(".", 1)[1]
                        for element in constraint.elements
                    ],
                }
            )
        return constraints

    def get_unique_constraints(self, table_name: str) -> list[dict[str, object]]:
        return [
            {
                "name": constraint.name,
                "column_names": list(constraint.columns.keys()),
            }
            for constraint in self.tables[table_name].constraints
            if isinstance(constraint, sa.UniqueConstraint)
        ]

    def get_check_constraints(self, table_name: str) -> list[dict[str, object]]:
        return [
            {"name": constraint.name, "sqltext": str(constraint.sqltext)}
            for constraint in self.tables[table_name].constraints
            if isinstance(constraint, sa.CheckConstraint)
        ]

    def get_indexes(self, table_name: str) -> list[dict[str, object]]:
        indexes = []
        for index in self.tables[table_name].indexes:
            where = index.dialect_options["postgresql"].get("where")
            if self.postgres_casts and index.name == "uq_chat_sessions_primary_direct":
                where = sa.text(
                    "((session_type)::text = 'direct'::text) "
                    "AND (is_primary = true) AND (deleted_at IS NULL)"
                )
            elif self.postgres_casts and index.name == "uq_chat_sessions_primary_group":
                where = sa.text(
                    "((session_type)::text = 'group'::text) "
                    "AND (group_id IS NOT NULL) AND (is_primary = true) "
                    "AND (deleted_at IS NULL)"
                )
            indexes.append(
                {
                    "name": index.name,
                    "column_names": list(index.columns.keys()),
                    "unique": index.unique,
                    "dialect_options": {"postgresql_where": where},
                }
            )
        return indexes


class ScalarResult:
    def __init__(self, value: int):
        self.value = value

    def scalar_one(self) -> int:
        return self.value


class RecordingBind:
    def __init__(self, results: list[int] | None = None):
        self.results = list(results or [])
        self.statements: list[str] = []

    def execute(self, statement):
        self.statements.append(str(statement))
        value = self.results.pop(0) if self.results else 0
        return ScalarResult(value)


def test_unified_chat_models_match_the_target_metadata_contract():
    sessions = ChatSession.__table__
    messages = ChatMessage.__table__

    assert {
        "tenant_id",
        "session_type",
        "group_id",
        "created_by_participant_id",
        "deleted_at",
        "updated_at",
    } <= set(sessions.columns.keys())
    assert sessions.c.tenant_id.nullable is False
    assert sessions.c.session_type.nullable is False
    assert sessions.c.agent_id.nullable is True
    assert sessions.c.user_id.nullable is True
    assert _constraint_names(sessions, sa.UniqueConstraint) == {
        "uq_chat_sessions_agent_ext_conv",
        "uq_chat_sessions_tenant_id_id",
    }
    assert _constraint_names(sessions, sa.CheckConstraint) == {
        "ck_chat_sessions_session_type"
    }
    assert {
        "uq_chat_sessions_primary_direct",
        "uq_chat_sessions_primary_group",
        "ix_chat_sessions_tenant_id",
        "ix_chat_sessions_group_id",
    } <= {index.name for index in sessions.indexes}
    assert "uq_chat_sessions_primary_platform" not in {
        index.name for index in sessions.indexes
    }

    assert messages.c.agent_id.nullable is True
    assert messages.c.user_id.nullable is True
    assert messages.c.mentions.nullable is False
    assert isinstance(messages.c.mentions.type, sa.dialects.postgresql.JSONB)
    assert str(messages.c.mentions.server_default.arg) == "'[]'::jsonb"


@pytest.mark.parametrize("postgres_casts", [False, True])
def test_fresh_metadata_is_a_check_first_noop(monkeypatch, postgres_casts):
    migration = _load_migration()
    inspector = FinalMetadataInspector(postgres_casts=postgres_casts)
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
        "alter_column",
        lambda table, column, **_kwargs: writes.append(f"alter:{table}.{column}"),
    )

    migration.upgrade()

    assert migration.revision == "unify_chat_schema"
    assert migration.down_revision == "create_group_domain_schema"
    assert writes == []


def test_partially_final_message_identity_columns_do_not_short_circuit_upgrade():
    migration = _load_migration()
    inspector = FinalMetadataInspector(legacy_message_identity_nullability=True)

    assert migration._final_columns_present(inspector) is False


def test_legacy_audits_run_before_ordered_backfills():
    migration = _load_migration()
    bind = RecordingBind()

    migration._audit_tenant_backfill(bind)
    migration._backfill(bind)
    migration._audit_finalizable_data(bind)

    normalized = [" ".join(statement.split()) for statement in bind.statements]
    first_update = next(
        index for index, statement in enumerate(normalized) if statement.startswith("UPDATE")
    )
    assert first_update == 3
    assert "SET tenant_id = a.tenant_id" in normalized[first_update]
    session_type_update = normalized[first_update + 1]
    assert "SET session_type = CASE" in session_type_update
    assert session_type_update.index("source_channel = 'agent'") < session_type_update.index(
        "source_channel = 'trigger'"
    )
    assert session_type_update.index("source_channel = 'trigger'") < session_type_update.index(
        "is_group"
    )
    assert any(
        statement.startswith("UPDATE chat_messages SET mentions = '[]'::jsonb")
        for statement in normalized
    )
    assert not any("default tenant" in statement.lower() for statement in normalized)


def test_tenant_audit_fails_before_any_backfill():
    migration = _load_migration()
    bind = RecordingBind(results=[1])

    with pytest.raises(RuntimeError, match="source Agent tenant is missing"):
        migration._audit_tenant_backfill(bind)

    assert len(bind.statements) == 1
    assert bind.statements[0].lstrip().startswith("SELECT count")


def test_downgrade_refuses_new_session_semantics_before_ddl(monkeypatch):
    migration = _load_migration()
    inspector = FinalMetadataInspector()
    bind = RecordingBind(results=[0, 1])
    destructive_writes: list[str] = []

    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(migration.sa, "inspect", lambda _bind: inspector)
    monkeypatch.setattr(
        migration.op,
        "drop_column",
        lambda table, column: destructive_writes.append(f"drop:{table}.{column}"),
    )

    with pytest.raises(RuntimeError, match="new-only semantics"):
        migration.downgrade()

    assert bind.statements[0] == (
        "LOCK TABLE chat_sessions, chat_messages IN ACCESS EXCLUSIVE MODE"
    )
    assert destructive_writes == []


def test_downgrade_mentions_guard_rejects_nonempty_mentions():
    migration = _load_migration()
    bind = RecordingBind(results=[0, 0, 1])

    with pytest.raises(RuntimeError, match="messages contain mentions"):
        migration._audit_downgrade(bind)

    assert len(bind.statements) == 3
