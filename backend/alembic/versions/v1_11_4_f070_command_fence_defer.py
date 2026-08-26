"""Add fence-defer columns to agent_run_commands.

Background:
    A Command that hits an active safe-read Tool fence is released and becomes
    reclaimable only after the fence lease expires (deferred_until); the first
    defer freezes deferred_started_at so a stall can be measured. See
    docs/adr/0002-safe-read-fence-defer-wait-and-breaker.md.

Scope:
    - Two nullable timestamptz columns; pure DDL, no data backfill.

Idempotent:
    - Both columns are guarded by inspector existence checks so fresh
      environments (001_initial_schema creates tables from the current model)
      do not hit DuplicateColumnError.
    - downgrade() is symmetric: drops the two columns in reverse order.

Revision ID: f070_command_fence_defer
Revises: f069_merge_main_heads
Create Date: 2026-08-26 00:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f070_command_fence_defer"
down_revision: Union[str, Sequence[str], None] = "f069_merge_main_heads"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = "agent_run_commands"
DEFERRED_UNTIL = "deferred_until"
DEFERRED_STARTED_AT = "deferred_started_at"


def _column_names() -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {c["name"] for c in inspector.get_columns(TABLE_NAME)}


def upgrade() -> None:
    columns = _column_names()
    if DEFERRED_UNTIL not in columns:
        op.add_column(
            TABLE_NAME,
            sa.Column(DEFERRED_UNTIL, sa.DateTime(timezone=True), nullable=True),
        )
    if DEFERRED_STARTED_AT not in columns:
        op.add_column(
            TABLE_NAME,
            sa.Column(DEFERRED_STARTED_AT, sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    columns = _column_names()
    if DEFERRED_STARTED_AT in columns:
        op.drop_column(TABLE_NAME, DEFERRED_STARTED_AT)
    if DEFERRED_UNTIL in columns:
        op.drop_column(TABLE_NAME, DEFERRED_UNTIL)
