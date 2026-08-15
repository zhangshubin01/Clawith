"""Add source column to skills table.

Background:
    The ORM model declares Skill.source (String(50), NOT NULL, default
    'custom'), but no migration ever created the column. On databases where
    the skills table predates the model field, every Skill query fails with
    UndefinedColumnError: column skills.source does not exist (observed in
    the startup Skills seed and Default agents seed).

Scope:
    - Add skills.source VARCHAR(50) NOT NULL DEFAULT 'custom'.
    - Pure DDL; no row reads or data backfill loops.

Idempotent:
    Uses ADD COLUMN IF NOT EXISTS because some environments already have the
    column from manual hotfixes. Re-running is a no-op. Downgrade drops the
    column if present.

Revision ID: f062_add_skill_source
Revises: 8c7fd88e793f
Create Date: 2026-08-14 23:00:00.000000
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f062_add_skill_source"
down_revision: Union[str, Sequence[str], None] = "8c7fd88e793f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE skills ADD COLUMN IF NOT EXISTS source VARCHAR(50) NOT NULL DEFAULT 'custom'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE skills DROP COLUMN IF EXISTS source")
