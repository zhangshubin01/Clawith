"""移除用户 cw- API Key 字段

Revision ID: remove_user_api_key
Create Date: 2026-05-14

删除 users 表的 api_key_hash 列及其唯一索引。
cw- API Key 体系已从三端彻底删除，认证统一使用 JWT。
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'remove_user_api_key'
down_revision: Union[str, None] = 'user_tenant_onboarding_defaults'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index('ix_users_api_key_hash', table_name='users', if_exists=True)
    op.drop_column('users', 'api_key_hash')


def downgrade() -> None:
    op.add_column('users', sa.Column('api_key_hash', sa.String(64), nullable=True))
    op.create_index('ix_users_api_key_hash', 'users', ['api_key_hash'], unique=True)
