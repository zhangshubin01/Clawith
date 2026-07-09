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
    # IF NOT EXISTS：create_all 或旧库可能已有列，避免 DuplicateColumnError 阻塞启动
    op.execute("ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP WITH TIME ZONE")
    op.execute(
        "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'active'"
    )
    op.execute(
        "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS message_count INTEGER NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.drop_column("chat_sessions", "message_count")
    op.drop_column("chat_sessions", "status")
    op.drop_column("chat_sessions", "deleted_at")
