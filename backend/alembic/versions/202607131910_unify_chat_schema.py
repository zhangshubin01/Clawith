"""Unify direct and group chat storage on the existing chat tables.

Revision ID: unify_chat_schema
Revises: create_group_domain_schema
Create Date: 2026-07-13 19:10:00
"""

from collections.abc import Sequence
import re

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "unify_chat_schema"
down_revision: str | None = "create_group_domain_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_SESSION_NEW_COLUMNS: dict[str, sa.Column] = {
    "tenant_id": sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
    "session_type": sa.Column("session_type", sa.String(length=20), nullable=True),
    "group_id": sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=True),
    "created_by_participant_id": sa.Column(
        "created_by_participant_id", postgresql.UUID(as_uuid=True), nullable=True
    ),
    "deleted_at": sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    "updated_at": sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
}

_DERIVED_SESSION_TYPE = """
CASE
    WHEN cs.source_channel = 'agent' THEN 'a2a'
    WHEN cs.source_channel = 'trigger' THEN 'trigger'
    WHEN COALESCE(cs.is_group, false) = true THEN 'group'
    ELSE 'direct'
END
""".strip()


def _type_signature(column_type: sa.types.TypeEngine) -> tuple[object, ...]:
    if isinstance(column_type, postgresql.JSONB):
        return ("jsonb",)
    if isinstance(column_type, sa.Uuid):
        return ("uuid", bool(column_type.as_uuid))
    if isinstance(column_type, sa.DateTime):
        return ("datetime", bool(column_type.timezone))
    if isinstance(column_type, sa.String):
        return ("string", column_type.length)
    return (type(column_type).__name__.lower(),)


def _canonical_default(value: object | None) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"\s+", " ", str(value).strip().lower())
    while normalized.startswith("(") and normalized.endswith(")"):
        normalized = normalized[1:-1].strip()
    if normalized in {"now()", "current_timestamp", "current_timestamp()"}:
        return "now"
    if re.fullmatch(r"'\[\]'(?:::(?:jsonb|json))?", normalized):
        return "empty_json_array"
    return normalized


def _columns(inspector: sa.Inspector, table_name: str) -> dict[str, dict[str, object]]:
    return {column["name"]: column for column in inspector.get_columns(table_name)}


def _require_base_schema(inspector: sa.Inspector) -> None:
    tables = set(inspector.get_table_names())
    missing_tables = {"chat_sessions", "chat_messages", "groups"} - tables
    if missing_tables:
        raise RuntimeError(
            "Unified chat migration requires existing tables: "
            + ", ".join(sorted(missing_tables))
        )

    required_session_columns = {
        "id",
        "agent_id",
        "user_id",
        "source_channel",
        "is_group",
        "participant_id",
        "is_primary",
    }
    required_message_columns = {"id", "agent_id", "user_id"}
    missing_session = required_session_columns - set(_columns(inspector, "chat_sessions"))
    missing_message = required_message_columns - set(_columns(inspector, "chat_messages"))
    if missing_session or missing_message:
        details = []
        if missing_session:
            details.append(f"chat_sessions: {', '.join(sorted(missing_session))}")
        if missing_message:
            details.append(f"chat_messages: {', '.join(sorted(missing_message))}")
        raise RuntimeError("Unified chat migration found an incompatible base schema (" + "; ".join(details) + ")")


def _validate_staging_columns(inspector: sa.Inspector) -> None:
    sessions = _columns(inspector, "chat_sessions")
    messages = _columns(inspector, "chat_messages")
    expected_types = {
        "tenant_id": ("uuid", True),
        "session_type": ("string", 20),
        "group_id": ("uuid", True),
        "created_by_participant_id": ("uuid", True),
        "deleted_at": ("datetime", True),
        "updated_at": ("datetime", True),
        "agent_id": ("uuid", True),
        "user_id": ("uuid", True),
    }
    for name, expected_type in expected_types.items():
        actual = sessions.get(name)
        if actual is None or _type_signature(actual["type"]) != expected_type:
            actual_type = None if actual is None else _type_signature(actual["type"])
            raise RuntimeError(
                f"chat_sessions.{name} has type {actual_type!r}; expected {expected_type!r}"
            )

    mentions = messages.get("mentions")
    if mentions is None or _type_signature(mentions["type"]) != ("jsonb",):
        actual_type = None if mentions is None else _type_signature(mentions["type"])
        raise RuntimeError(f"chat_messages.mentions has type {actual_type!r}; expected ('jsonb',)")


def _final_columns_present(inspector: sa.Inspector) -> bool:
    sessions = _columns(inspector, "chat_sessions")
    messages = _columns(inspector, "chat_messages")
    expected_sessions = {
        "tenant_id": (("uuid", True), False, None),
        "session_type": (("string", 20), False, None),
        "group_id": (("uuid", True), True, None),
        "created_by_participant_id": (("uuid", True), True, None),
        "deleted_at": (("datetime", True), True, None),
        "updated_at": (("datetime", True), False, "now"),
        "agent_id": (("uuid", True), True, None),
        "user_id": (("uuid", True), True, None),
    }
    for name, (type_signature, nullable, default) in expected_sessions.items():
        column = sessions.get(name)
        if column is None:
            return False
        if (
            _type_signature(column["type"]),
            bool(column["nullable"]),
            _canonical_default(column.get("default")),
        ) != (type_signature, nullable, default):
            return False

    expected_messages = {
        "agent_id": (("uuid", True), True, None),
        "user_id": (("uuid", True), True, None),
        "mentions": (("jsonb",), False, "empty_json_array"),
    }
    for name, (type_signature, nullable, default) in expected_messages.items():
        column = messages.get(name)
        if column is None:
            return False
        if (
            _type_signature(column["type"]),
            bool(column["nullable"]),
            _canonical_default(column.get("default")),
        ) != (type_signature, nullable, default):
            return False
    return True


def _normalize_sql(value: object) -> str:
    normalized = str(value).strip().lower()
    normalized = re.sub(
        r"::\s*(?:character\s+varying(?:\(\d+\))?|text|jsonb|boolean|uuid)(?:\[\])?",
        "",
        normalized,
    )
    normalized = re.sub(r"[()\"\s]+", " ", normalized)
    return normalized.strip()


def _index_where(index: dict[str, object]) -> object | None:
    dialect_options = index.get("dialect_options") or {}
    where = dialect_options.get("postgresql_where")
    if where is None:
        where = index.get("postgresql_where")
    return where


def _validate_index(
    inspector: sa.Inspector,
    *,
    name: str,
    columns: tuple[str, ...],
    unique: bool,
    where_terms: tuple[str, ...] = (),
) -> bool:
    indexes = {index.get("name"): index for index in inspector.get_indexes("chat_sessions")}
    index = indexes.get(name)
    if index is None:
        return False
    actual = (tuple(index.get("column_names") or ()), bool(index.get("unique")))
    expected = (columns, unique)
    if actual != expected:
        raise RuntimeError(f"Existing index {name} has schema {actual!r}; expected {expected!r}")
    where_value = _index_where(index)
    where = _normalize_sql("" if where_value is None else where_value)
    if where_terms and not all(_normalize_sql(term) in where for term in where_terms):
        raise RuntimeError(f"Existing index {name} has incompatible predicate: {where!r}")
    return True


def _foreign_key_exists(
    inspector: sa.Inspector,
    *,
    name: str,
    local_column: str,
    remote_table: str,
) -> bool:
    foreign_keys = {
        constraint.get("name"): constraint
        for constraint in inspector.get_foreign_keys("chat_sessions")
    }
    constraint = foreign_keys.get(name)
    if constraint is None:
        return False
    actual = (
        tuple(constraint.get("constrained_columns") or ()),
        constraint.get("referred_table"),
        tuple(constraint.get("referred_columns") or ()),
    )
    expected = ((local_column,), remote_table, ("id",))
    if actual != expected:
        raise RuntimeError(f"Existing foreign key {name} has schema {actual!r}; expected {expected!r}")
    return True


def _unique_exists(inspector: sa.Inspector) -> bool:
    constraints = {
        constraint.get("name"): tuple(constraint.get("column_names") or ())
        for constraint in inspector.get_unique_constraints("chat_sessions")
    }
    columns = constraints.get("uq_chat_sessions_tenant_id_id")
    if columns is None:
        return False
    if columns != ("tenant_id", "id"):
        raise RuntimeError(
            "Existing uq_chat_sessions_tenant_id_id has columns "
            f"{columns!r}; expected ('tenant_id', 'id')"
        )
    return True


def _session_type_check_exists(inspector: sa.Inspector) -> bool:
    checks = {
        constraint.get("name"): constraint.get("sqltext")
        for constraint in inspector.get_check_constraints("chat_sessions")
    }
    expression = checks.get("ck_chat_sessions_session_type")
    if expression is None:
        return False
    normalized = _normalize_sql(expression)
    literals = set(re.findall(r"'([^']+)'", normalized))
    if "session_type" not in normalized or literals != {"direct", "group", "a2a", "trigger"}:
        raise RuntimeError(
            "Existing ck_chat_sessions_session_type does not restrict session_type to the supported values"
        )
    return True


def _final_relations_present(inspector: sa.Inspector) -> bool:
    required_foreign_keys = (
        ("fk_chat_sessions_tenant_id_tenants", "tenant_id", "tenants"),
        ("fk_chat_sessions_group_id_groups", "group_id", "groups"),
        (
            "fk_chat_sessions_created_by_participant_id_participants",
            "created_by_participant_id",
            "participants",
        ),
    )
    if not all(
        _foreign_key_exists(
            inspector,
            name=name,
            local_column=local_column,
            remote_table=remote_table,
        )
        for name, local_column, remote_table in required_foreign_keys
    ):
        return False
    if not _unique_exists(inspector) or not _session_type_check_exists(inspector):
        return False

    indexes = {index.get("name") for index in inspector.get_indexes("chat_sessions")}
    if "uq_chat_sessions_primary_platform" in indexes:
        return False
    return all(
        (
            _validate_index(
                inspector,
                name="uq_chat_sessions_primary_direct",
                columns=("tenant_id", "agent_id", "user_id"),
                unique=True,
                where_terms=(
                    "session_type = 'direct'",
                    "is_primary = true",
                    "deleted_at is null",
                ),
            ),
            _validate_index(
                inspector,
                name="uq_chat_sessions_primary_group",
                columns=("group_id",),
                unique=True,
                where_terms=(
                    "session_type = 'group'",
                    "group_id is not null",
                    "is_primary = true",
                    "deleted_at is null",
                ),
            ),
            _validate_index(
                inspector,
                name="ix_chat_sessions_tenant_id",
                columns=("tenant_id",),
                unique=False,
            ),
            _validate_index(
                inspector,
                name="ix_chat_sessions_group_id",
                columns=("group_id",),
                unique=False,
            ),
        )
    )


def _result_scalar(result: object) -> int:
    scalar_one = getattr(result, "scalar_one", None)
    if scalar_one is not None:
        return int(scalar_one())
    scalar = getattr(result, "scalar", None)
    if scalar is not None:
        return int(scalar())
    raise RuntimeError("Migration audit query did not return a scalar result")


def _require_zero(bind: sa.Connection, sql: str, message: str) -> None:
    count = _result_scalar(bind.execute(sa.text(sql)))
    if count:
        raise RuntimeError(f"{message}: {count} row(s)")


def _audit_tenant_backfill(bind: sa.Connection) -> None:
    candidate = "(cs.tenant_id IS NULL OR cs.session_type IS NULL)"
    _require_zero(
        bind,
        f"""
        SELECT count(*)
        FROM chat_sessions cs
        LEFT JOIN agents a ON a.id = cs.agent_id
        WHERE {candidate}
          AND cs.tenant_id IS NULL
          AND (cs.agent_id IS NULL OR a.tenant_id IS NULL)
        """,
        "Cannot backfill chat_sessions.tenant_id because the source Agent tenant is missing",
    )
    _require_zero(
        bind,
        f"""
        SELECT count(*)
        FROM chat_sessions cs
        LEFT JOIN users u ON u.id = cs.user_id
        WHERE {candidate}
          AND ({_DERIVED_SESSION_TYPE}) = 'direct'
          AND (cs.user_id IS NULL OR u.tenant_id IS NULL)
        """,
        "Cannot migrate direct chat session because the User tenant is missing",
    )
    _require_zero(
        bind,
        f"""
        SELECT count(*)
        FROM chat_sessions cs
        JOIN agents a ON a.id = cs.agent_id
        JOIN users u ON u.id = cs.user_id
        WHERE {candidate}
          AND ({_DERIVED_SESSION_TYPE}) = 'direct'
          AND a.tenant_id IS DISTINCT FROM u.tenant_id
        """,
        "Cannot migrate direct chat session because Agent and User tenants disagree",
    )


def _backfill(bind: sa.Connection) -> None:
    statements = (
        """
        UPDATE chat_sessions cs
        SET tenant_id = a.tenant_id
        FROM agents a
        WHERE cs.agent_id = a.id
          AND cs.tenant_id IS NULL
        """,
        f"""
        UPDATE chat_sessions cs
        SET session_type = {_DERIVED_SESSION_TYPE}
        WHERE cs.session_type IS NULL
        """,
        """
        UPDATE chat_sessions cs
        SET created_by_participant_id = p.id
        FROM participants p
        WHERE cs.created_by_participant_id IS NULL
          AND cs.session_type IN ('direct', 'group')
          AND p.type = 'user'
          AND p.ref_id = cs.user_id
        """,
        """
        UPDATE chat_sessions
        SET created_by_participant_id = participant_id
        WHERE created_by_participant_id IS NULL
          AND session_type = 'a2a'
          AND participant_id IS NOT NULL
        """,
        """
        UPDATE chat_sessions cs
        SET created_by_participant_id = p.id
        FROM participants p
        WHERE cs.created_by_participant_id IS NULL
          AND cs.session_type = 'trigger'
          AND p.type = 'agent'
          AND p.ref_id = cs.agent_id
        """,
        "UPDATE chat_sessions SET updated_at = now() WHERE updated_at IS NULL",
        "UPDATE chat_messages SET mentions = '[]'::jsonb WHERE mentions IS NULL",
    )
    for statement in statements:
        bind.execute(sa.text(statement))


def _audit_finalizable_data(bind: sa.Connection) -> None:
    _require_zero(
        bind,
        "SELECT count(*) FROM chat_sessions WHERE tenant_id IS NULL OR session_type IS NULL OR updated_at IS NULL",
        "Unified chat backfill left required chat_sessions fields empty",
    )
    _require_zero(
        bind,
        "SELECT count(*) FROM chat_messages WHERE mentions IS NULL",
        "Unified chat backfill left chat_messages.mentions empty",
    )
    _require_zero(
        bind,
        """
        SELECT count(*) FROM (
            SELECT tenant_id, agent_id, user_id
            FROM chat_sessions
            WHERE session_type = 'direct' AND is_primary = true AND deleted_at IS NULL
            GROUP BY tenant_id, agent_id, user_id
            HAVING count(*) > 1
        ) duplicates
        """,
        "Cannot create direct primary-session uniqueness because duplicate primaries exist",
    )
    _require_zero(
        bind,
        """
        SELECT count(*) FROM (
            SELECT group_id
            FROM chat_sessions
            WHERE session_type = 'group'
              AND group_id IS NOT NULL
              AND is_primary = true
              AND deleted_at IS NULL
            GROUP BY group_id
            HAVING count(*) > 1
        ) duplicates
        """,
        "Cannot create group primary-session uniqueness because duplicate primaries exist",
    )


def _ensure_constraints_and_indexes(inspector: sa.Inspector) -> None:
    foreign_keys = (
        ("fk_chat_sessions_tenant_id_tenants", "tenant_id", "tenants"),
        ("fk_chat_sessions_group_id_groups", "group_id", "groups"),
        (
            "fk_chat_sessions_created_by_participant_id_participants",
            "created_by_participant_id",
            "participants",
        ),
    )
    for name, local_column, remote_table in foreign_keys:
        if not _foreign_key_exists(
            inspector,
            name=name,
            local_column=local_column,
            remote_table=remote_table,
        ):
            op.create_foreign_key(
                name,
                "chat_sessions",
                remote_table,
                [local_column],
                ["id"],
            )

    if not _unique_exists(inspector):
        op.create_unique_constraint(
            "uq_chat_sessions_tenant_id_id",
            "chat_sessions",
            ["tenant_id", "id"],
        )
    if not _session_type_check_exists(inspector):
        op.create_check_constraint(
            "ck_chat_sessions_session_type",
            "chat_sessions",
            "session_type IN ('direct', 'group', 'a2a', 'trigger')",
        )

    indexes = {index.get("name") for index in inspector.get_indexes("chat_sessions")}
    if "uq_chat_sessions_primary_platform" in indexes:
        op.drop_index("uq_chat_sessions_primary_platform", table_name="chat_sessions")
    if not _validate_index(
        inspector,
        name="ix_chat_sessions_tenant_id",
        columns=("tenant_id",),
        unique=False,
    ):
        op.create_index("ix_chat_sessions_tenant_id", "chat_sessions", ["tenant_id"])
    if not _validate_index(
        inspector,
        name="ix_chat_sessions_group_id",
        columns=("group_id",),
        unique=False,
    ):
        op.create_index("ix_chat_sessions_group_id", "chat_sessions", ["group_id"])
    if not _validate_index(
        inspector,
        name="uq_chat_sessions_primary_direct",
        columns=("tenant_id", "agent_id", "user_id"),
        unique=True,
        where_terms=("session_type = 'direct'", "is_primary = true", "deleted_at is null"),
    ):
        op.create_index(
            "uq_chat_sessions_primary_direct",
            "chat_sessions",
            ["tenant_id", "agent_id", "user_id"],
            unique=True,
            postgresql_where=sa.text(
                "session_type = 'direct' AND is_primary = true AND deleted_at IS NULL"
            ),
        )
    if not _validate_index(
        inspector,
        name="uq_chat_sessions_primary_group",
        columns=("group_id",),
        unique=True,
        where_terms=(
            "session_type = 'group'",
            "group_id is not null",
            "is_primary = true",
            "deleted_at is null",
        ),
    ):
        op.create_index(
            "uq_chat_sessions_primary_group",
            "chat_sessions",
            ["group_id"],
            unique=True,
            postgresql_where=sa.text(
                "session_type = 'group' AND group_id IS NOT NULL "
                "AND is_primary = true AND deleted_at IS NULL"
            ),
        )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    _require_base_schema(inspector)

    session_columns = _columns(inspector, "chat_sessions")
    for name, column in _SESSION_NEW_COLUMNS.items():
        if name not in session_columns:
            op.add_column("chat_sessions", column)
    message_columns = _columns(inspector, "chat_messages")
    if "mentions" not in message_columns:
        op.add_column(
            "chat_messages",
            sa.Column("mentions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        )

    inspector = sa.inspect(bind)
    _validate_staging_columns(inspector)
    if _final_columns_present(inspector) and _final_relations_present(inspector):
        return

    bind.execute(sa.text("LOCK TABLE chat_sessions, chat_messages IN ACCESS EXCLUSIVE MODE"))
    _audit_tenant_backfill(bind)
    _backfill(bind)
    _audit_finalizable_data(bind)

    op.alter_column(
        "chat_sessions",
        "tenant_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.alter_column(
        "chat_sessions",
        "session_type",
        existing_type=sa.String(length=20),
        nullable=False,
    )
    op.alter_column(
        "chat_sessions",
        "agent_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.alter_column(
        "chat_sessions",
        "user_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.alter_column(
        "chat_sessions",
        "updated_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )
    op.alter_column(
        "chat_messages",
        "agent_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.alter_column(
        "chat_messages",
        "user_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.alter_column(
        "chat_messages",
        "mentions",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        nullable=False,
        server_default=sa.text("'[]'::jsonb"),
    )

    inspector = sa.inspect(bind)
    _ensure_constraints_and_indexes(inspector)


def _audit_downgrade(bind: sa.Connection) -> None:
    checks = (
        (
            """
            SELECT count(*) FROM chat_sessions
            WHERE group_id IS NOT NULL
               OR deleted_at IS NOT NULL
               OR agent_id IS NULL
               OR user_id IS NULL
            """,
            "Refusing to downgrade unified chat schema because sessions use new-only semantics",
        ),
        (
            f"""
            SELECT count(*) FROM chat_sessions cs
            WHERE session_type IS DISTINCT FROM ({_DERIVED_SESSION_TYPE})
            """,
            "Refusing to downgrade unified chat schema because session_type is not legacy-representable",
        ),
        (
            "SELECT count(*) FROM chat_messages WHERE mentions IS DISTINCT FROM '[]'::jsonb",
            "Refusing to downgrade unified chat schema because messages contain mentions",
        ),
        (
            "SELECT count(*) FROM chat_messages WHERE agent_id IS NULL OR user_id IS NULL",
            "Refusing to downgrade unified chat schema because messages use nullable legacy identities",
        ),
    )
    for sql, message in checks:
        _require_zero(bind, sql, message)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    _require_base_schema(inspector)
    bind.execute(sa.text("LOCK TABLE chat_sessions, chat_messages IN ACCESS EXCLUSIVE MODE"))
    _audit_downgrade(bind)

    indexes = {index.get("name") for index in inspector.get_indexes("chat_sessions")}
    for name in (
        "uq_chat_sessions_primary_group",
        "uq_chat_sessions_primary_direct",
        "ix_chat_sessions_group_id",
        "ix_chat_sessions_tenant_id",
    ):
        if name in indexes:
            op.drop_index(name, table_name="chat_sessions")

    checks = {constraint.get("name") for constraint in inspector.get_check_constraints("chat_sessions")}
    if "ck_chat_sessions_session_type" in checks:
        op.drop_constraint(
            "ck_chat_sessions_session_type", "chat_sessions", type_="check"
        )
    uniques = {
        constraint.get("name")
        for constraint in inspector.get_unique_constraints("chat_sessions")
    }
    if "uq_chat_sessions_tenant_id_id" in uniques:
        op.drop_constraint(
            "uq_chat_sessions_tenant_id_id", "chat_sessions", type_="unique"
        )
    foreign_keys = {
        constraint.get("name")
        for constraint in inspector.get_foreign_keys("chat_sessions")
    }
    for name in (
        "fk_chat_sessions_created_by_participant_id_participants",
        "fk_chat_sessions_group_id_groups",
        "fk_chat_sessions_tenant_id_tenants",
    ):
        if name in foreign_keys:
            op.drop_constraint(name, "chat_sessions", type_="foreignkey")

    op.alter_column(
        "chat_messages",
        "agent_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.alter_column(
        "chat_messages",
        "user_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.alter_column(
        "chat_sessions",
        "agent_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.alter_column(
        "chat_sessions",
        "user_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )

    op.drop_column("chat_messages", "mentions")
    for name in (
        "updated_at",
        "deleted_at",
        "created_by_participant_id",
        "group_id",
        "session_type",
        "tenant_id",
    ):
        op.drop_column("chat_sessions", name)

    if "uq_chat_sessions_primary_platform" not in indexes:
        op.create_index(
            "uq_chat_sessions_primary_platform",
            "chat_sessions",
            ["agent_id", "user_id"],
            unique=True,
            postgresql_where=sa.text(
                "is_primary = true AND source_channel = 'web' AND is_group = false"
            ),
        )
