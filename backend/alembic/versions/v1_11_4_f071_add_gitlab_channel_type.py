"""Add 'gitlab' to the channel_type enum.

Background:
    GitLab agent binding (docs/technical-plans/20260820-gitlab-agent-binding)
    reuses channel_configs with channel_type='gitlab'. The column is a native
    PostgreSQL enum, so the new value must be added with ALTER TYPE.

Scope:
    - Pure DDL: ALTER TYPE channel_type_enum ADD VALUE IF NOT EXISTS 'gitlab'.

Idempotent:
    - IF NOT EXISTS makes the upgrade idempotent on re-run.
    - downgrade() is a no-op: PostgreSQL does not support dropping enum values;
      rolling back code versions keeps the unused value harmlessly.

Revision ID: f071_add_gitlab_channel_type
Revises: f070_command_fence_defer
Create Date: 2026-08-26 00:00:00
"""
from typing import Sequence, Union

from alembic import op

revision: str = "f071_add_gitlab_channel_type"
down_revision: Union[str, Sequence[str], None] = "f070_command_fence_defer"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PG15: ADD VALUE is allowed inside a transaction; the new value must not be
    # used within the same transaction (this migration only adds it).
    op.execute("ALTER TYPE channel_type_enum ADD VALUE IF NOT EXISTS 'gitlab'")


def downgrade() -> None:
    # PostgreSQL has no ALTER TYPE ... DROP VALUE; irreversible by design.
    pass
