"""Add Feishu group delivery targets to schedules and triggers.

Idempotent:
  Each nullable column is added or removed only when the current schema
  state requires the DDL operation (fresh environments create all tables
  via the models, so the column may already exist when this runs).

Revision ID: f065_feishu_group_target
Revises: f064_tool_call_tenants
Create Date: 2026-08-18 16:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "f065_feishu_group_target"
down_revision: str | Sequence[str] | None = "f064_tool_call_tenants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DELIVERY_TARGET_ID = "delivery_target_id"


def _column_names(table: str, *, offline_default: set[str]) -> set[str]:
    try:
        inspector = sa.inspect(op.get_bind())
    except sa.exc.NoInspectionAvailable:
        return offline_default
    return {str(column["name"]) for column in inspector.get_columns(table)}


def upgrade() -> None:
    schedules_columns = _column_names("agent_schedules", offline_default=set())
    if DELIVERY_TARGET_ID not in schedules_columns:
        op.add_column(
            "agent_schedules",
            sa.Column(DELIVERY_TARGET_ID, postgresql.UUID(as_uuid=True), nullable=True),
        )
    triggers_columns = _column_names("agent_triggers", offline_default=set())
    if DELIVERY_TARGET_ID not in triggers_columns:
        op.add_column(
            "agent_triggers",
            sa.Column(DELIVERY_TARGET_ID, postgresql.UUID(as_uuid=True), nullable=True),
        )


def downgrade() -> None:
    triggers_columns = _column_names("agent_triggers", offline_default={DELIVERY_TARGET_ID})
    if DELIVERY_TARGET_ID in triggers_columns:
        op.drop_column("agent_triggers", DELIVERY_TARGET_ID)
    schedules_columns = _column_names("agent_schedules", offline_default={DELIVERY_TARGET_ID})
    if DELIVERY_TARGET_ID in schedules_columns:
        op.drop_column("agent_schedules", DELIVERY_TARGET_ID)
