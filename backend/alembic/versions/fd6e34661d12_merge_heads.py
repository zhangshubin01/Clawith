"""merge heads

Revision ID: fd6e34661d12
Revises: 25811072c8fd, increase_api_key_length
Create Date: 2026-04-15 18:21:26.090795
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fd6e34661d12'
down_revision: Union[str, None] = ('25811072c8fd', 'increase_api_key_length')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
