"""Add the agent_maintainers table for the Maintainer permission gate.

Background:
    The Maintainer gate (docs/technical-plans/20260905-maintainer-gate-g3-g4-
    production-plan.md) replaces the L3 delete approval flow with an explicit
    maintainer list: only the agent creator (implicit, never stored) plus rows
    in this table may drive write_file/edit_file/delete_file/move_file into
    ``workspace/`` and ``skills/``.

Scope:
    - Pure DDL: create ``agent_maintainers``. No data reads or writes (the
      autonomy_policy key cleanup for delete_files/write_workspace_files and
      the resolution of orphaned pending delete approvals are out-of-band data
      ops, kept OUT of this migration per backend/alembic/AGENTS.md §2).

Idempotent:
    - Fresh environments create the table from the current model in 001, so the
      table may already exist — the inspector guard skips creation in that case.
      downgrade() mirrors the guard.

Revision ID: f077_add_agent_maintainers
Revises: f076_add_no_progress_enum
Create Date: 2026-09-05 00:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f077_add_agent_maintainers"
down_revision: Union[str, Sequence[str], None] = "f076_add_no_progress_enum"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "agent_maintainers"
_INDEX = "ix_agent_maintainers_agent_id"


def _exists() -> bool:
    return _TABLE in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _exists():
        return
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("agent_id", "user_id", name="uq_agent_maintainers_agent_user"),
    )
    op.create_index(_INDEX, _TABLE, ["agent_id"])


def downgrade() -> None:
    if not _exists():
        return
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
