"""add composite index for ChatMessage history queries

覆盖 load_history_for_llm 和 load() 的查询模式:
WHERE conversation_id=? AND user_id=? AND role IN (?,?) ORDER BY created_at DESC LIMIT N

Revision ID: add_chat_message_query_index
Create Date: 2026-06-11
"""

from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "add_chat_message_query_index"
down_revision: Union[str, None] = "5c581c1958ed"  # merge_add_title_to_agent_focus_items
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """创建复合索引 — CONCURRENTLY 避免锁表。

    CREATE INDEX CONCURRENTLY 不能在事务内执行。
    通过 op.get_bind() 获取原始连接，使用 autocommit 绕过 Alembic 的事务管理。
    """
    connection = op.get_bind()
    connection.execution_options(isolation_level="AUTOCOMMIT").execute(
        sa.text(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_chat_messages_conv_user_role "
            "ON chat_messages "
            "(conversation_id, user_id, role, created_at DESC)"
        )
    )


def downgrade() -> None:
    """回退 — CONCURRENTLY 同样避免锁表。"""
    connection = op.get_bind()
    connection.execution_options(isolation_level="AUTOCOMMIT").execute(
        sa.text("DROP INDEX CONCURRENTLY IF EXISTS ix_chat_messages_conv_user_role")
    )
