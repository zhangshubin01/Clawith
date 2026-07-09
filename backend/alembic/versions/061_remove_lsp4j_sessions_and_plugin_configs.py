"""删除 LSP4J 历史会话、消息、CCR 条目及 ide_plugin_configs 表。

Revision ID: remove_lsp4j_sessions
Revises: ctx_ccr_session_created_idx
Create Date: 2026-07-06
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "remove_lsp4j_sessions"
down_revision: Union[str, None] = "ctx_ccr_session_created_idx"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LSP4J_SESSION_FILTER = """
    source_channel = 'ide_lsp4j' OR client_type = 'ide_lsp4j'
"""


def upgrade() -> None:
    op.execute(
        f"""
        DELETE FROM ctx_ccr_entries
        WHERE session_id IN (
            SELECT id::text FROM chat_sessions WHERE {_LSP4J_SESSION_FILTER}
        )
        """
    )
    op.execute(
        f"""
        DELETE FROM chat_messages
        WHERE conversation_id IN (
            SELECT id::text FROM chat_sessions WHERE {_LSP4J_SESSION_FILTER}
        )
        """
    )
    op.execute(f"DELETE FROM chat_sessions WHERE {_LSP4J_SESSION_FILTER}")

    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "ide_plugin_configs" in inspector.get_table_names():
        op.drop_table("ide_plugin_configs")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "ide_plugin_configs" in inspector.get_table_names():
        return
    op.create_table(
        "ide_plugin_configs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope_type", sa.String(length=20), nullable=False),
        sa.Column("scope_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("config_key", sa.String(length=100), nullable=False),
        sa.Column("config_value", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope_type", "scope_id", "config_key", name="uq_plugin_config_scope_key"),
    )
