"""add_title_to_agent_focus_items

Revision ID: 043e5f59eb8a
Revises: add_user_tenant_onboarding
Create Date: 2026-05-22 21:02:18.556576
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'add_title_to_agent_focus_items'
down_revision: Union[str, None] = 'merge_heads_20260521'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return any(col.get("name") == column_name for col in inspector.get_columns(table_name))


def upgrade() -> None:
    # 幂等：历史库可能已通过 create_all 提前加过 title
    if not _has_table("agent_focus_items") or _has_column("agent_focus_items", "title"):
        return
    op.add_column("agent_focus_items", sa.Column("title", sa.String(length=200), nullable=True))


def downgrade() -> None:
    if not _has_table("agent_focus_items") or not _has_column("agent_focus_items", "title"):
        return
    op.drop_column("agent_focus_items", "title")
