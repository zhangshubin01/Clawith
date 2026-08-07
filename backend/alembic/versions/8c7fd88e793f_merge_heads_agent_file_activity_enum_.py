"""merge heads: agent file activity enum and enterprise info tenant id

Revision ID: 8c7fd88e793f
Revises: add_agent_file_activity_enum, f061_enterprise_info_tenant_id
Create Date: 2026-08-07 18:31:09.070449
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8c7fd88e793f'
down_revision: Union[str, None] = ('add_agent_file_activity_enum', 'f061_enterprise_info_tenant_id')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
