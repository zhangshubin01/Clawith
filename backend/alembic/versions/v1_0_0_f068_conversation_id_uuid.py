"""Migrate chat_messages.conversation_id from varchar to uuid.

Background:
    conversation_id stores the string form of chat_sessions.id (UUID); every
    join between the two tables must CAST, which the type system cannot
    enforce (19 cast-join sites). Measured 2026-08-19: all 5485 rows are
    UUID-shaped and no legacy prefixed values (web_/feishu_/slack_/discord_)
    remain. See docs/adr/0001-conversation-id-uuid-migration.md.

Scope:
    - Single in-place ALTER COLUMN TYPE; pure DDL, no data backfill.
    - Non-UUID rows fail the USING cast with PostgreSQL's own diagnostic
      (invalid input syntax for type uuid: "<value>") — deliberately no
      silent coercion.
    - Code cleanup of the cast joins ships in the same commit/deployment:
      old code would raise operator-does-not-exist against the uuid column.

Downgrade:
    Casts back to varchar(200); values are UUID text, so the round-trip is
    lossless (code must be rolled back with the schema in the same deploy).
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f068_conversation_id_uuid"
down_revision: Union[str, Sequence[str], None] = "f067_chat_visible_role_idx"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE chat_messages "
        "ALTER COLUMN conversation_id TYPE uuid USING conversation_id::uuid"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE chat_messages "
        "ALTER COLUMN conversation_id TYPE varchar(200) USING conversation_id::text"
    )
