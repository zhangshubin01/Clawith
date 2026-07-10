"""为 invitation_codes 表添加 expires_at 过期时间字段

Revision ID: add_invitation_code_expires_at
Revises: add_gateway_message_expires_at
Create Date: 2026-05-18

问题：InvitationCode 模型没有 expires_at 字段，邀请码可无限期使用（#NEW-027）。
修复：新增 expires_at 字段，邀请码默认 30 天后过期。
      已存在的邀请码 expires_at 为 NULL，视为永不过期（兼容历史数据）。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_invitation_code_expires_at"
down_revision: Union[str, None] = "add_gateway_message_expires_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return any(col.get("name") == column_name for col in inspector.get_columns(table_name))


def upgrade() -> None:
    if not _has_table("invitation_codes") or _has_column("invitation_codes", "expires_at"):
        return
    op.add_column(
        "invitation_codes",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    if not _has_table("invitation_codes") or not _has_column("invitation_codes", "expires_at"):
        return
    op.drop_column("invitation_codes", "expires_at")
