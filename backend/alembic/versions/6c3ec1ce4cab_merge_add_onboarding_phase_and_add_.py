"""merge add_onboarding_phase and add_token_cache_usage_fields

Revision ID: 6c3ec1ce4cab
Revises: 92040765b612, add_onboarding_phase
Create Date: 2026-05-06 16:05:18.936470
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6c3ec1ce4cab'
down_revision: Union[str, None] = ('92040765b612', 'add_onboarding_phase')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
