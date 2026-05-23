"""add_title_to_agent_focus_items

Revision ID: 043e5f59eb8a
Revises: add_user_tenant_onboarding
Create Date: 2026-05-22 21:02:18.556576
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
<<<<<<< HEAD:backend/alembic/versions/059_add_title_to_agent_focus_items.py
revision: str = 'add_title_to_agent_focus_items'
=======
revision: str = '043e5f59eb8a'
>>>>>>> f5bb364460d8d714550cbbe43de342580e04485f:backend/alembic/versions/043e5f59eb8a_add_title_to_agent_focus_items.py
down_revision: Union[str, None] = 'merge_heads_20260521'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add title column to agent_focus_items
    op.add_column('agent_focus_items', sa.Column('title', sa.String(length=200), nullable=True))


def downgrade() -> None:
    # Remove title column from agent_focus_items
    op.drop_column('agent_focus_items', 'title')
