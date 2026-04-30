"""merge remaining alembic heads

Revision ID: 87ff921e8e6f
Revises: 5fe287d9d58b, merge_pr494_heads
Create Date: 2026-04-30 15:51:22.875830
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '87ff921e8e6f'
down_revision: Union[str, None] = ('5fe287d9d58b', 'merge_pr494_heads')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
