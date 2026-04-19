"""Add company country/region and OKR non-workday skip setting.

Revision ID: add_region_workday_skip
Revises: add_channel_enum_values
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_region_workday_skip"
down_revision: Union[str, Sequence[str], None] = "add_channel_enum_values"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("country_region", sa.String(length=10), nullable=False, server_default="001"),
    )
    op.add_column(
        "okr_settings",
        sa.Column("daily_report_skip_non_workdays", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )


def downgrade() -> None:
    op.drop_column("okr_settings", "daily_report_skip_non_workdays")
    op.drop_column("tenants", "country_region")
