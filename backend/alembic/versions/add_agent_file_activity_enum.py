"""Add agent_file_sent / agent_file_received to activity_action_enum.

The file send/receive tools (agent_tools.py) record agent activities with
these action types, but the PostgreSQL enum only had the original 12 values,
so the INSERT failed with "invalid input value for enum activity_action_enum".

Revision ID: 202608070100_add_agent_file_activity_enum
Revises: fcd83b56f246
Create Date: 2026-08-07 01:00:00
"""
from typing import Sequence, Union

from alembic import op


revision: str = "add_agent_file_activity_enum"
down_revision: Union[str, Sequence[str], None] = "fcd83b56f246"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Keep the PostgreSQL enum in sync with AgentActivityLog.action_type.
    for value in ("agent_file_sent", "agent_file_received"):
        op.execute(
            f"ALTER TYPE activity_action_enum ADD VALUE IF NOT EXISTS '{value}'"
        )


def downgrade() -> None:
    # PostgreSQL cannot safely remove enum values in-place.
    pass
