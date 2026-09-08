"""Add the agent_list_items table for the structured list (清单) channel.

Background:
    The numbered-list channel is migrated off free-text extraction (R1
    ``ListPersistenceCompletionHandler``) onto a structured table, mirroring
    the focus migration (``agent_focus_items``). See
    docs/technical-plans/20260907-number-reference-amnesia-fix-plan.md.

    ``sort_order`` is the stable visible number per (agent, project); the
    second unique constraint ``(agent_id, project, sort_order)`` turns the
    DAO's ``max+1`` allocation from a non-atomic read-modify-write into an
    atomic number issuance (concurrent insert conflicts retry in the DAO).

Scope:
    - Pure DDL: create ``agent_list_items``. No data reads or writes.

Idempotent:
    - Fresh environments create the table from the current model in 001, so
      the table may already exist — the inspector guard skips creation.
      downgrade() mirrors the guard.

Revision ID: f078_add_agent_list_items
Revises: f077_add_agent_maintainers
Create Date: 2026-09-07 00:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f078_add_agent_list_items"
down_revision: Union[str, Sequence[str], None] = "f077_add_agent_maintainers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "agent_list_items"
_INDEXES = (
    ("ix_agent_list_items_agent_id", "agent_id"),
    ("ix_agent_list_items_project", "project"),
    ("ix_agent_list_items_key", "key"),
    ("ix_agent_list_items_status", "status"),
    ("ix_agent_list_items_created_at", "created_at"),
)


def _exists() -> bool:
    return _TABLE in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _exists():
        return
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("project", sa.String(200), nullable=False),
        sa.Column("key", sa.String(200), nullable=False),
        sa.Column("title", sa.String(200), nullable=True),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("agent_id", "project", "key", name="uq_agent_list_items_agent_project_key"),
        sa.UniqueConstraint("agent_id", "project", "sort_order", name="uq_agent_list_items_agent_project_sort"),
    )
    for index_name, _column in _INDEXES:
        op.create_index(index_name, _TABLE, [_column])


def downgrade() -> None:
    if not _exists():
        return
    for index_name, _column in _INDEXES:
        op.drop_index(index_name, table_name=_TABLE)
    op.drop_table(_TABLE)
