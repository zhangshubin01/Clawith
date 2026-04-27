"""Merge heads after main merge

Revision ID: 5fe287d9d58b
Revises: fd6e34661d12, rm_agent_credential_secrets
Create Date: 2026-04-27 22:09:41.460653
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5fe287d9d58b'
down_revision: Union[str, None] = ('fd6e34661d12', 'rm_agent_credential_secrets')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
