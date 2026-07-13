"""Add model capability metadata required by the Agent runtime.

Revision ID: add_llm_runtime_capabilities
Revises: unify_chat_schema
Create Date: 2026-07-13 19:20:00
"""

from collections.abc import Sequence
import re

from alembic import op
import sqlalchemy as sa


revision: str = "add_llm_runtime_capabilities"
down_revision: str | None = "unify_chat_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_COLUMN_FACTORIES = {
    "context_window_tokens": lambda: sa.Column(
        "context_window_tokens", sa.Integer(), nullable=True
    ),
    "context_window_tokens_override": lambda: sa.Column(
        "context_window_tokens_override", sa.Integer(), nullable=True
    ),
    "max_input_tokens": lambda: sa.Column(
        "max_input_tokens", sa.Integer(), nullable=True
    ),
    "max_input_tokens_override": lambda: sa.Column(
        "max_input_tokens_override", sa.Integer(), nullable=True
    ),
    "capability_source": lambda: sa.Column(
        "capability_source", sa.String(length=32), nullable=True
    ),
    "capability_checked_at": lambda: sa.Column(
        "capability_checked_at", sa.DateTime(timezone=True), nullable=True
    ),
}

_POSITIVE_CHECKS = {
    "ck_llm_models_context_window_tokens_positive": "context_window_tokens",
    "ck_llm_models_context_window_tokens_override_positive": (
        "context_window_tokens_override"
    ),
    "ck_llm_models_max_input_tokens_positive": "max_input_tokens",
    "ck_llm_models_max_input_tokens_override_positive": "max_input_tokens_override",
}
_SOURCE_CHECK = "ck_llm_models_capability_source"
_SOURCE_VALUES = {"manual", "provider_api", "builtin_registry", "runtime_config"}


def _type_signature(column_type: sa.types.TypeEngine) -> tuple[object, ...]:
    if isinstance(column_type, sa.Integer) and not isinstance(
        column_type, (sa.BigInteger, sa.SmallInteger)
    ):
        return ("integer",)
    if isinstance(column_type, sa.DateTime):
        return ("datetime", bool(column_type.timezone))
    if isinstance(column_type, sa.String):
        return ("string", column_type.length)
    return (type(column_type).__name__.lower(),)


def _normalize_sql(value: object) -> str:
    normalized = str(value).strip().lower()
    normalized = re.sub(
        r"::\s*(?:character\s+varying(?:\(\d+\))?|text|integer|int4)(?:\[\])?",
        "",
        normalized,
    )
    normalized = re.sub(r"[()\"\s]+", " ", normalized)
    return normalized.strip()


def _columns(inspector: sa.Inspector) -> dict[str, dict[str, object]]:
    return {
        column["name"]: column for column in inspector.get_columns("llm_models")
    }


def _require_table(inspector: sa.Inspector) -> None:
    if "llm_models" not in set(inspector.get_table_names()):
        raise RuntimeError("LLM capability migration requires the llm_models table")


def _validate_columns(inspector: sa.Inspector) -> None:
    columns = _columns(inspector)
    expected = {
        "context_window_tokens": ("integer",),
        "context_window_tokens_override": ("integer",),
        "max_input_tokens": ("integer",),
        "max_input_tokens_override": ("integer",),
        "capability_source": ("string", 32),
        "capability_checked_at": ("datetime", True),
    }
    for name, expected_type in expected.items():
        column = columns.get(name)
        if column is None:
            raise RuntimeError(f"llm_models.{name} is missing after schema expansion")
        actual = (
            _type_signature(column["type"]),
            bool(column["nullable"]),
            column.get("default"),
        )
        expected_signature = (expected_type, True, None)
        if actual != expected_signature:
            raise RuntimeError(
                f"llm_models.{name} has schema {actual!r}; expected {expected_signature!r}"
            )


def _validate_positive_check(name: str, column: str, expression: object) -> None:
    normalized = _normalize_sql(expression)
    expected = f"{column} is null or {column} > 0"
    if normalized != expected:
        raise RuntimeError(
            f"Existing check {name} has expression {normalized!r}; expected {expected!r}"
        )


def _validate_source_check(expression: object) -> None:
    normalized = _normalize_sql(expression)
    literals = set(re.findall(r"'([^']+)'", normalized))
    supported_shape = (
        "capability_source is null or capability_source in " in normalized
        or "capability_source is null or capability_source = any " in normalized
    )
    if (
        not supported_shape
        or literals != _SOURCE_VALUES
    ):
        raise RuntimeError(
            f"Existing check {_SOURCE_CHECK} has an incompatible expression: {normalized!r}"
        )


def _ensure_checks(inspector: sa.Inspector) -> None:
    checks = {
        constraint.get("name"): constraint.get("sqltext")
        for constraint in inspector.get_check_constraints("llm_models")
    }
    for name, column in _POSITIVE_CHECKS.items():
        expression = checks.get(name)
        if expression is None:
            op.create_check_constraint(
                name,
                "llm_models",
                f"{column} IS NULL OR {column} > 0",
            )
        else:
            _validate_positive_check(name, column, expression)

    expression = checks.get(_SOURCE_CHECK)
    if expression is None:
        op.create_check_constraint(
            _SOURCE_CHECK,
            "llm_models",
            "capability_source IS NULL OR capability_source IN "
            "('manual', 'provider_api', 'builtin_registry', 'runtime_config')",
        )
    else:
        _validate_source_check(expression)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    _require_table(inspector)

    existing_columns = _columns(inspector)
    for name, column_factory in _COLUMN_FACTORIES.items():
        if name not in existing_columns:
            op.add_column("llm_models", column_factory())

    inspector = sa.inspect(bind)
    _validate_columns(inspector)
    _ensure_checks(inspector)


def _result_scalar(result: object) -> int:
    scalar_one = getattr(result, "scalar_one", None)
    if scalar_one is not None:
        return int(scalar_one())
    scalar = getattr(result, "scalar", None)
    if scalar is not None:
        return int(scalar())
    raise RuntimeError("LLM capability downgrade audit did not return a scalar")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    _require_table(inspector)
    bind.execute(sa.text("LOCK TABLE llm_models IN ACCESS EXCLUSIVE MODE"))

    columns = _columns(inspector)
    present_columns = [name for name in _COLUMN_FACTORIES if name in columns]
    if present_columns:
        used_predicate = " OR ".join(
            f"{column_name} IS NOT NULL" for column_name in present_columns
        )
        used_count = _result_scalar(
            bind.execute(
                sa.text(f"SELECT count(*) FROM llm_models WHERE {used_predicate}")
            )
        )
        if used_count:
            raise RuntimeError(
                "Refusing to downgrade LLM runtime capabilities because capability "
                f"metadata is populated: {used_count} row(s)"
            )

    checks = {
        constraint.get("name")
        for constraint in inspector.get_check_constraints("llm_models")
    }
    for name in (*_POSITIVE_CHECKS, _SOURCE_CHECK):
        if name in checks:
            op.drop_constraint(name, "llm_models", type_="check")
    for name in reversed(_COLUMN_FACTORIES):
        if name in columns:
            op.drop_column("llm_models", name)
