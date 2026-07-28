"""add scope_type and scope_id to workspace tables

Revision ID: 4bb3f28af46e
Revises: allow_checkpoint_deliveries
Create Date: 2026-07-28 10:45:00
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '4bb3f28af46e'
down_revision: Union[str, None] = 'allow_checkpoint_deliveries'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # workspace_file_revisions
    op.add_column('workspace_file_revisions',
        sa.Column('scope_type', sa.String(20), nullable=False, server_default='agent'))
    op.add_column('workspace_file_revisions',
        sa.Column('scope_id', sa.UUID(), nullable=False, server_default=sa.text("'00000000-0000-0000-0000-000000000000'::uuid")))
    # Backfill: set scope_id = agent_id for all existing rows
    op.execute("UPDATE workspace_file_revisions SET scope_id = agent_id WHERE scope_id = '00000000-0000-0000-0000-000000000000'")
    op.create_check_constraint(
        'ck_workspace_file_revisions_scope_type',
        'workspace_file_revisions',
        "scope_type IN ('agent', 'group')")
    op.create_check_constraint(
        'ck_workspace_file_revisions_scope_identity',
        'workspace_file_revisions',
        "(scope_type = 'agent' AND agent_id IS NOT NULL AND scope_id = agent_id) OR (scope_type = 'group' AND agent_id IS NULL)")

    # workspace_edit_locks
    op.add_column('workspace_edit_locks',
        sa.Column('scope_type', sa.String(20), nullable=False, server_default='agent'))
    op.add_column('workspace_edit_locks',
        sa.Column('scope_id', sa.UUID(), nullable=False, server_default=sa.text("'00000000-0000-0000-0000-000000000000'::uuid")))
    # Backfill: set scope_id = agent_id for all existing rows
    op.execute("UPDATE workspace_edit_locks SET scope_id = agent_id WHERE scope_id = '00000000-0000-0000-0000-000000000000'")
    op.create_check_constraint(
        'ck_workspace_edit_locks_scope_type',
        'workspace_edit_locks',
        "scope_type IN ('agent', 'group')")
    op.create_check_constraint(
        'ck_workspace_edit_locks_scope_identity',
        'workspace_edit_locks',
        "(scope_type = 'agent' AND agent_id IS NOT NULL AND scope_id = agent_id) OR (scope_type = 'group' AND agent_id IS NULL)")


def downgrade() -> None:
    op.drop_constraint('ck_workspace_edit_locks_scope_identity', 'workspace_edit_locks')
    op.drop_constraint('ck_workspace_edit_locks_scope_type', 'workspace_edit_locks')
    op.drop_column('workspace_edit_locks', 'scope_id')
    op.drop_column('workspace_edit_locks', 'scope_type')

    op.drop_constraint('ck_workspace_file_revisions_scope_identity', 'workspace_file_revisions')
    op.drop_constraint('ck_workspace_file_revisions_scope_type', 'workspace_file_revisions')
    op.drop_column('workspace_file_revisions', 'scope_id')
    op.drop_column('workspace_file_revisions', 'scope_type')
