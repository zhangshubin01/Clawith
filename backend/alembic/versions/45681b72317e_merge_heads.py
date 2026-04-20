"""merge heads

Revision ID: 45681b72317e
Revises: 29f3f8de3ca0, f1a2b3c4d5e6
Create Date: 2026-04-13 17:58:56.805349
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '45681b72317e'
down_revision: Union[str, None] = ('29f3f8de3ca0', 'f1a2b3c4d5e6')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
