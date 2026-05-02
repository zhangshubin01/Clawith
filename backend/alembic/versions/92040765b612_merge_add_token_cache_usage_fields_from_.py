"""merge add_token_cache_usage_fields from main

Revision ID: 92040765b612
Revises: 482391030754, add_token_cache_usage_fields
Create Date: 2026-05-03 01:08:55.038173
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '92040765b612'
down_revision: Union[str, None] = ('482391030754', 'add_token_cache_usage_fields')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
