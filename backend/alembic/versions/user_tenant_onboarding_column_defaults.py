"""Align user_tenant_onboardings varchar columns with migration server_default.

Revision ID: user_tenant_onboarding_defaults
Revises: add_user_tenant_onboarding
Create Date: 2026-05-11

Tables created before the idempotent branch in add_user_tenant_onboarding may
lack these PostgreSQL column defaults; this revision sets them to match
create_table(..., server_default=...).
"""

from typing import Sequence, Union

from alembic import op


revision: str = "user_tenant_onboarding_defaults"
down_revision: Union[str, Sequence[str], None] = "add_user_tenant_onboarding"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE user_tenant_onboardings
          ALTER COLUMN status SET DEFAULT 'in_progress',
          ALTER COLUMN current_step SET DEFAULT 'assistant',
          ALTER COLUMN entry_mode SET DEFAULT 'create';
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE user_tenant_onboardings
          ALTER COLUMN status DROP DEFAULT,
          ALTER COLUMN current_step DROP DEFAULT,
          ALTER COLUMN entry_mode DROP DEFAULT;
        """
    )
