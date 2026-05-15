"""#77 ChatSession 软删除和状态字段

添加 deleted_at, status, message_count 列
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "add_chat_session_soft_delete"
down_revision: Union[str, None] = "remove_user_api_key"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("chat_sessions", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("chat_sessions", sa.Column("status", sa.String(20), nullable=False, server_default="active"))
    op.add_column("chat_sessions", sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("chat_sessions", "message_count")
    op.drop_column("chat_sessions", "status")
    op.drop_column("chat_sessions", "deleted_at")
