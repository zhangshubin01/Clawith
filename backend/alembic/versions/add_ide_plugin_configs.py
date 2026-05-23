"""add ide_plugin_configs

Revision ID: add_ide_plugin_configs
Revises: user_refactor_v1
Create Date: 2026-05-01

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'add_ide_plugin_configs'
down_revision: Union[str, None] = 'user_refactor_v1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Skip if table already exists (may be pre-created by Base.metadata.create_all)
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if 'ide_plugin_configs' in inspector.get_table_names():
        return
    op.create_table(
        'ide_plugin_configs',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('scope_type', sa.String(length=20), nullable=False),
        sa.Column('scope_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('config_key', sa.String(length=100), nullable=False),
        sa.Column('config_value', sa.Text(), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('scope_type', 'scope_id', 'config_key', name='uq_plugin_config_scope_key'),
    )


def downgrade() -> None:
    op.drop_table('ide_plugin_configs')
