"""add chat_message composite index

Revision ID: add_chat_message_indexes
Revises: user_tenant_onboarding_defaults
Create Date: 2026-05-14

核心查询模式 [agent_id, conversation_id, created_at] 无复合索引，
每次查询走全表扫描或部分索引组合，添加后降低 40-60% 查询延迟（#71）

使用 CREATE INDEX CONCURRENTLY 避免锁表阻塞读写，
需要 autocommit_block 在事务外执行（#71 fix）
"""
from typing import Union

from alembic import op

revision: str = 'add_chat_message_indexes'
down_revision: Union[str, None] = 'add_chat_session_soft_delete'
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade():
    # 使用 autocommit_block 在事务外执行 CREATE INDEX CONCURRENTLY
    # PostgreSQL 的 CONCURRENTLY 不允许在事务块内执行，且不支持 IF NOT EXISTS
    with op.get_context().autocommit_block():
        op.execute("""
            CREATE INDEX CONCURRENTLY idx_chat_messages_agent_conv_created
            ON chat_messages (agent_id, conversation_id, created_at)
        """)


def downgrade():
    # DROP INDEX CONCURRENTLY 也需要在事务外执行
    with op.get_context().autocommit_block():
        op.execute("""
            DROP INDEX IF EXISTS idx_chat_messages_agent_conv_created
        """)
