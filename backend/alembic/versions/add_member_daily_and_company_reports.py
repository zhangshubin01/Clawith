"""Add member daily reports and company reports.

Revision ID: add_member_daily_company_reports
Revises: add_region_workday_skip
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "add_member_daily_company_reports"
down_revision: Union[str, Sequence[str], None] = "add_region_workday_skip"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "member_daily_reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("member_type", sa.String(length=20), nullable=False),
        sa.Column("member_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("report_date", sa.Date(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="submitted"),
        sa.Column("source", sa.String(length=30), nullable=False, server_default="okr_agent_assisted"),
        sa.Column("submitted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "member_type", "member_id", "report_date",
            name="uq_member_daily_report",
        ),
    )
    op.create_index("ix_member_daily_reports_tenant_id", "member_daily_reports", ["tenant_id"])
    op.create_index("ix_member_daily_reports_member_id", "member_daily_reports", ["member_id"])
    op.create_index("ix_member_daily_reports_report_date", "member_daily_reports", ["report_date"])

    op.create_table(
        "company_reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("report_type", sa.String(length=10), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("period_label", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("submitted_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("missing_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("needs_refresh", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("generated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "report_type", "period_start", "period_end",
            name="uq_company_report_period",
        ),
    )
    op.create_index("ix_company_reports_tenant_id", "company_reports", ["tenant_id"])
    op.create_index("ix_company_reports_period_start", "company_reports", ["period_start"])


def downgrade() -> None:
    op.drop_index("ix_company_reports_period_start", table_name="company_reports")
    op.drop_index("ix_company_reports_tenant_id", table_name="company_reports")
    op.drop_table("company_reports")

    op.drop_index("ix_member_daily_reports_report_date", table_name="member_daily_reports")
    op.drop_index("ix_member_daily_reports_member_id", table_name="member_daily_reports")
    op.drop_index("ix_member_daily_reports_tenant_id", table_name="member_daily_reports")
    op.drop_table("member_daily_reports")

