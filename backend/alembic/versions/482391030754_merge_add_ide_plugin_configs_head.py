"""merge add_ide_plugin_configs head

Revision ID: 482391030754
Revises: 87ff921e8e6f, add_ide_plugin_configs
Create Date: 2026-05-03 00:47:53.273733
"""
from typing import Sequence, Union



# revision identifiers, used by Alembic.
revision: str = '482391030754'
down_revision: Union[str, None] = ('87ff921e8e6f', 'add_ide_plugin_configs')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
