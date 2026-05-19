"""为 gateway_messages 表添加 expires_at 过期时间字段

Revision ID: add_gateway_message_expires_at
Revises: add_chat_message_indexes
Create Date: 2026-05-18

问题：pending 消息若目标 OpenClaw 节点永久离线，将永远卡在队列中（#NEW-023）。
修复：新增 expires_at 字段，消息默认 7 天后过期，poll 时自动过滤已过期消息并清理。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "add_gateway_message_expires_at"
down_revision: Union[str, None] = "add_chat_message_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "gateway_messages",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("gateway_messages", "expires_at")
