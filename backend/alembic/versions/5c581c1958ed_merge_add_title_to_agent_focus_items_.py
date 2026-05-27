"""merge add_title_to_agent_focus_items and 5c821c81d264

Revision ID: 5c581c1958ed
Revises: add_title_to_agent_focus_items, 5c821c81d264
Create Date: 2026-05-27 16:58:19.509817
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5c581c1958ed'
down_revision: Union[str, None] = ('add_title_to_agent_focus_items', '5c821c81d264')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
