"""CCR 索引优化 M4/M5 — session LRU + 移除冗余 hash 索引。

Revision ID: ctx_ccr_session_created_idx
Revises: add_chat_message_query_index
Create Date: 2026-07-05
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "ctx_ccr_session_created_idx"
down_revision: Union[str, None] = "add_chat_message_query_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 确保表存在（首次部署时 alembic 在 create_all 之前执行，表可能未创建）
    op.create_table(
        "ctx_ccr_entries",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("agent_id", sa.String(64)),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("tool_name", sa.String(100), nullable=False, server_default=""),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("original_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("compressed_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("path", sa.String(20), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", "content_hash", name="uq_ctx_ccr_session_hash"),
    )
    op.create_index("ix_ctx_ccr_session_created", "ctx_ccr_entries", ["session_id", "created_at"], if_not_exists=True)
    op.create_index("ix_ctx_ccr_expires_at", "ctx_ccr_entries", ["expires_at"], if_not_exists=True)
    op.drop_index("ix_ctx_ccr_session_hash", table_name="ctx_ccr_entries", if_exists=True)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_ctx_ccr_session_created")
    op.execute("DROP INDEX IF EXISTS ix_ctx_ccr_expires_at")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ctx_ccr_session_hash ON ctx_ccr_entries (session_id, content_hash)")
