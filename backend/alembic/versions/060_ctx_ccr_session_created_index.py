"""CCR 索引优化 M4/M5 — session LRU + 移除冗余 hash 索引。

Revision ID: ctx_ccr_session_created_idx
Revises: add_chat_message_query_index
Create Date: 2026-07-05
"""
from typing import Sequence, Union

from alembic import op

revision: str = "ctx_ccr_session_created_idx"
down_revision: Union[str, None] = "add_chat_message_query_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE INDEX IF NOT EXISTS ix_ctx_ccr_session_created ON ctx_ccr_entries (session_id, created_at)")
    op.execute("DROP INDEX IF EXISTS ix_ctx_ccr_session_hash")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_ctx_ccr_session_created")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ctx_ccr_session_hash ON ctx_ccr_entries (session_id, content_hash)")
