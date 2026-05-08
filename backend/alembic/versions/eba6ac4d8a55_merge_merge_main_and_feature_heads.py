"""merge: merge main and feature heads

Revision ID: eba6ac4d8a55
Revises: 6c3ec1ce4cab, add_agent_focus_items
Create Date: 2026-05-08 10:26:09.636646
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'eba6ac4d8a55'
down_revision: Union[str, None] = ('6c3ec1ce4cab', 'add_agent_focus_items')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
