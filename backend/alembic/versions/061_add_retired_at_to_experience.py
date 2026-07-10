"""add retired_at to experience_entries

Tracks when an entry was retired so retired entries not re-published within
30 days can be hard-deleted by the background sweep. Cleared on re-publish.

Revision ID: experience_retired_at
Revises: experience_library
Create Date: 2026-07-10
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'experience_retired_at'
down_revision: Union[str, None] = 'experience_library'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'experience_entries',
        sa.Column('retired_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('experience_entries', 'retired_at')
