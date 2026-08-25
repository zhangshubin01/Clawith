"""Merge the Feishu group target and conversation UUID migration heads.

Revision ID: f069_merge_main_heads
Revises: f065_feishu_group_target, f068_conversation_id_uuid
Create Date: 2026-08-25 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "f069_merge_main_heads"
down_revision: str | Sequence[str] | None = (
    "f065_feishu_group_target",
    "f068_conversation_id_uuid",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join both migration branches without extra DDL."""


def downgrade() -> None:
    """Split back to the two parent heads without extra DDL."""
