"""experience_library

Create the experience library tables (entries + references). Does not touch the
legacy plaza_* tables — those are retained read-only for historical data.

NOTE: verify `alembic heads` resolves to a single head before applying; adjust
`down_revision` if your local chain differs.

Revision ID: experience_library
Revises: add_title_to_agent_focus_items
Create Date: 2026-07-08
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSON, UUID

# revision identifiers, used by Alembic.
revision: str = 'experience_library'
down_revision: Union[str, None] = 'add_title_to_agent_focus_items'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'experience_entries',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('tenant_id', UUID(as_uuid=True), nullable=True),
        sa.Column('title', sa.String(length=200), nullable=False, server_default=''),
        sa.Column('scenario', sa.Text(), nullable=False, server_default=''),
        sa.Column('problem', sa.Text(), nullable=False, server_default=''),
        sa.Column('solution', sa.Text(), nullable=False, server_default=''),
        sa.Column('applicability', sa.Text(), nullable=False, server_default=''),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='draft'),
        sa.Column('tags', JSON(), nullable=True),
        # P0-6 visibility (mirrors agent_permissions.scope_type / scope_id)
        sa.Column('visibility_scope', sa.String(length=16), nullable=False, server_default='company'),
        sa.Column('visibility_scope_id', UUID(as_uuid=True), nullable=True),
        # entry provenance: chat (normal) | legacy_plaza (hard-isolated import)
        sa.Column('origin', sa.String(length=20), nullable=False, server_default='chat'),
        sa.Column('origin_session_id', UUID(as_uuid=True), nullable=True),
        sa.Column('origin_agent_id', UUID(as_uuid=True), nullable=True),
        sa.Column('created_by', UUID(as_uuid=True), nullable=False),
        sa.Column('reviewed_by', UUID(as_uuid=True), nullable=True),
        sa.Column('last_reviewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_experience_entries_tenant_id', 'experience_entries', ['tenant_id'])
    op.create_index('ix_experience_entries_status', 'experience_entries', ['status'])
    op.create_index('ix_experience_entries_visibility_scope', 'experience_entries', ['visibility_scope'])
    op.create_index('ix_experience_entries_origin', 'experience_entries', ['origin'])
    op.create_index('ix_experience_entries_created_at', 'experience_entries', ['created_at'])

    op.create_table(
        'experience_references',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('entry_id', UUID(as_uuid=True),
                  sa.ForeignKey('experience_entries.id', ondelete='CASCADE'), nullable=False),
        sa.Column('kind', sa.String(length=10), nullable=False, server_default='read'),
        sa.Column('tenant_id', UUID(as_uuid=True), nullable=True),
        sa.Column('agent_id', UUID(as_uuid=True), nullable=True),
        sa.Column('session_id', UUID(as_uuid=True), nullable=True),
        sa.Column('message_id', UUID(as_uuid=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_experience_references_entry_id', 'experience_references', ['entry_id'])
    op.create_index('ix_experience_references_kind', 'experience_references', ['kind'])
    op.create_index('ix_experience_references_tenant_id', 'experience_references', ['tenant_id'])
    op.create_index('ix_experience_references_agent_id', 'experience_references', ['agent_id'])
    op.create_index('ix_experience_references_created_at', 'experience_references', ['created_at'])


def downgrade() -> None:
    op.drop_table('experience_references')
    op.drop_table('experience_entries')
