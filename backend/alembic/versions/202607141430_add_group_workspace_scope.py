"""Add group scope to workspace revisions and edit locks.

Revision ID: add_group_workspace_scope
Revises: create_agent_runtime_schema
Create Date: 2026-07-14 14:30:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "add_group_workspace_scope"
down_revision: str | None = "create_agent_runtime_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _add_scope_columns(table_name: str) -> None:
    op.add_column(
        table_name,
        sa.Column("scope_type", sa.String(length=20), nullable=True),
    )
    op.add_column(
        table_name,
        sa.Column("scope_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute(
        sa.text(
            f"UPDATE {table_name} "
            "SET scope_type = 'agent', scope_id = agent_id "
            "WHERE scope_type IS NULL OR scope_id IS NULL"
        )
    )
    op.alter_column(table_name, "scope_type", nullable=False)
    op.alter_column(table_name, "scope_id", nullable=False)
    op.alter_column(table_name, "agent_id", nullable=True)


def _add_scope_constraints(table_name: str) -> None:
    op.create_check_constraint(
        f"ck_{table_name}_scope_type",
        table_name,
        "scope_type IN ('agent', 'group')",
    )
    op.create_check_constraint(
        f"ck_{table_name}_scope_identity",
        table_name,
        "(scope_type = 'agent' AND agent_id IS NOT NULL AND scope_id = agent_id) "
        "OR (scope_type = 'group' AND agent_id IS NULL)",
    )
    op.create_index(
        f"ix_{table_name}_scope_path",
        table_name,
        ["scope_type", "scope_id", "path"],
        unique=False,
    )


def upgrade() -> None:
    _add_scope_columns("workspace_file_revisions")
    _add_scope_columns("workspace_edit_locks")

    op.drop_constraint(
        "uq_workspace_edit_locks_agent_path",
        "workspace_edit_locks",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_workspace_edit_locks_scope_path",
        "workspace_edit_locks",
        ["scope_type", "scope_id", "path"],
    )
    _add_scope_constraints("workspace_file_revisions")
    _add_scope_constraints("workspace_edit_locks")


def _drop_scope_constraints(table_name: str) -> None:
    op.drop_index(f"ix_{table_name}_scope_path", table_name=table_name)
    op.drop_constraint(
        f"ck_{table_name}_scope_identity",
        table_name,
        type_="check",
    )
    op.drop_constraint(
        f"ck_{table_name}_scope_type",
        table_name,
        type_="check",
    )


def downgrade() -> None:
    op.execute("DELETE FROM workspace_edit_locks WHERE scope_type = 'group'")
    op.execute("DELETE FROM workspace_file_revisions WHERE scope_type = 'group'")
    _drop_scope_constraints("workspace_edit_locks")
    _drop_scope_constraints("workspace_file_revisions")
    op.drop_constraint(
        "uq_workspace_edit_locks_scope_path",
        "workspace_edit_locks",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_workspace_edit_locks_agent_path",
        "workspace_edit_locks",
        ["agent_id", "path"],
    )
    for table_name in ("workspace_edit_locks", "workspace_file_revisions"):
        op.alter_column(table_name, "agent_id", nullable=False)
        op.drop_column(table_name, "scope_id")
        op.drop_column(table_name, "scope_type")
