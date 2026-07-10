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
    # gateway_messages 在部分部署路径中未创建（后续迁移会按条件 drop），跳过即可
    if not _has_table("gateway_messages") or _has_column("gateway_messages", "expires_at"):
        return
    op.add_column(
        "gateway_messages",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    if not _has_table("gateway_messages") or not _has_column("gateway_messages", "expires_at"):
        return
    op.drop_column("gateway_messages", "expires_at")
