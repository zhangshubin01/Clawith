"""add chat_message composite index

Revision ID: add_chat_message_indexes
Revises: user_tenant_onboarding_defaults
Create Date: 2026-05-14

核心查询模式 [agent_id, conversation_id, created_at] 无复合索引，
每次查询走全表扫描或部分索引组合，添加后降低 40-60% 查询延迟（#71）
"""
from typing import Union

from alembic import op

revision: str = 'add_chat_message_indexes'
down_revision: Union[str, None] = 'add_chat_session_soft_delete'
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade():
    op.create_index('idx_chat_messages_agent_conv_created', 'chat_messages',
        ['agent_id', 'conversation_id', 'created_at'], if_not_exists=True)


def downgrade():
    op.drop_index('idx_chat_messages_agent_conv_created', table_name='chat_messages', if_exists=True)
