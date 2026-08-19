"""Partial index for user-visible message scans (chat_messages).

Background:
    The session-context background scanner and context-pack builder filter
    messages by ``role IN ('user', 'assistant')`` on every pass, but the only
    supporting index is ``ix_chat_messages_conversation_created_id``
    (conversation_id, created_at, id) — role is not in it. 81.5% of all
    chat_messages rows are ``tool_call`` (4413/5416 measured 2026-08-19), so
    every visible-role scan reads the full session and discards ~89% of the
    rows it touches (EXPLAIN ANALYZE on a 242-message session: 235 rows
    removed by filter, session scanned twice by the compactable NOT-IN
    subplan). This partial index covers exactly the visible subset (~18.5%
    of rows) and lets those scans go index-only; the planner proves the
    implication ``role IN ('user','assistant')`` → predicate (verified live:
    outer + subplan switch to Index Only Scan, buffer reads -79%).

Scope:
    - CREATE INDEX (partial predicate, btree) only; pure DDL, no data ops.
    - New additive index — the existing ``ix_chat_messages_conversation_created_id``
      stays untouched for other queries (unread badge uses
      assistant/system/tool_call roles and keeps using it).

Downgrade:
    Drops the partial index.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "f067_chat_visible_role_idx"
down_revision: Union[str, Sequence[str], None] = "f066_checkpoint_idx_cleanup"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX_NAME = "ix_chat_messages_conversation_visible_created_id"


def upgrade() -> None:
    op.create_index(
        _INDEX_NAME,
        "chat_messages",
        ["conversation_id", "created_at", "id"],
        postgresql_where=text("role IN ('user', 'assistant')"),
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="chat_messages")
