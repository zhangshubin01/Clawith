"""Add agents.stall_window / stall_ratio / stall_guard_action (P2 stall guard).

Background:
    Duplicate-read stall guard (docs/technical-plans/
    20260905-read-dedup-production-plan.md, P2) measures the fraction of
    re-reads within a sliding window of recent read_file results; when it
    reaches ``stall_ratio`` the run converges via ``stall_guard_action``
    (remind default / compact / terminate / off). Per-agent tunable, mirroring
    ``max_tool_rounds`` / ``read_dedup_n``.

Scope:
    - agents: + stall_window (INTEGER NOT NULL DEFAULT 20)
              + stall_ratio (FLOAT NOT NULL DEFAULT 0.7)
              + stall_guard_action (VARCHAR(16) NOT NULL DEFAULT 'remind')
    - Pure DDL; no row reads or backfill beyond the server defaults.

Idempotent:
    - Fresh deployments get the columns from 001_initial_schema (current model),
      so each add is guarded by an inspector check to avoid DuplicateColumnError.

Revision ID: f074_add_stall_guard
Revises: f073_add_read_dedup_n
Create Date: 2026-09-05 00:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f074_add_stall_guard"
down_revision: Union[str, Sequence[str], None] = "f073_add_read_dedup_n"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    agent_columns = {col["name"] for col in inspector.get_columns("agents")}
    if "stall_window" not in agent_columns:
        op.add_column(
            "agents",
            sa.Column("stall_window", sa.Integer(), nullable=False, server_default="20"),
        )
    if "stall_ratio" not in agent_columns:
        op.add_column(
            "agents",
            sa.Column("stall_ratio", sa.Float(), nullable=False, server_default="0.7"),
        )
    if "stall_guard_action" not in agent_columns:
        op.add_column(
            "agents",
            sa.Column(
                "stall_guard_action",
                sa.String(16),
                nullable=False,
                server_default="remind",
            ),
        )


def downgrade() -> None:
    op.drop_column("agents", "stall_guard_action")
    op.drop_column("agents", "stall_ratio")
    op.drop_column("agents", "stall_window")
