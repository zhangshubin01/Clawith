"""merge heads after main merge

Revision ID: 5c821c81d264
Revises: add_invitation_code_expires_at, merge_heads_20260521
Create Date: 2026-05-22 18:20:53.454348
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5c821c81d264'
down_revision: Union[str, None] = ('add_invitation_code_expires_at', 'merge_heads_20260521')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
