"""Add agents.read_dedup_n (model-side read_file dedup threshold).

Background:
    Model-side read_file deduplication (docs/technical-plans/
    20260905-read-dedup-production-plan.md, P0) caps how many times the same
    (path, content_hash) read_file result is fed to the model within a
    compaction cycle; further repeats become soft placeholders. The threshold
    is per-agent tunable, mirroring ``max_tool_rounds``; 0 disables dedup.

Scope:
    - agents: + read_dedup_n (INTEGER NOT NULL DEFAULT 3)
    - Pure DDL; no row reads or backfill beyond the server default.

Idempotent:
    - Fresh deployments get the column from 001_initial_schema (current model),
      so the add is guarded by an inspector check to avoid DuplicateColumnError.

Revision ID: f073_add_read_dedup_n
Revises: f072_memory_consolidation_event
Create Date: 2026-09-05 00:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f073_add_read_dedup_n"
down_revision: Union[str, Sequence[str], None] = "f072_memory_consolidation_event"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    agent_columns = {col["name"] for col in inspector.get_columns("agents")}
    if "read_dedup_n" not in agent_columns:
        op.add_column(
            "agents",
            sa.Column("read_dedup_n", sa.Integer(), nullable=False, server_default="3"),
        )


def downgrade() -> None:
    op.drop_column("agents", "read_dedup_n")
