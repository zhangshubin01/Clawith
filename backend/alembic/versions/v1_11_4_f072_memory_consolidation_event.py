"""Add 'memory_consolidation_skipped' to the agent_run_events event_type check.

Background:
    The Runtime Memory Consolidation Gate (docs/adr/0005-runtime-memory-
    consolidation-hook) records one objective event when a Run finishes after
    workspace writes without a durable memory write. The new event type needs
    its slot in ck_agent_run_events_event_type before any run can emit it.

Scope:
    - Pure DDL: replace ck_agent_run_events_event_type with the widened
      whitelist. No data reads or writes.

Idempotent:
    - Fresh environments create the table from the current model in 001, so
      the check may already carry the new value — the inspector guard skips
      the drop in that case. downgrade() mirrors the guard in reverse.

Revision ID: f072_memory_consolidation_event
Revises: f071_add_gitlab_channel_type
Create Date: 2026-08-27 00:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f072_memory_consolidation_event"
down_revision: Union[str, Sequence[str], None] = "f071_add_gitlab_channel_type"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CHECK_NAME = "ck_agent_run_events_event_type"
_OLD_EVENT_TYPE_CHECK = (
    "event_type IN ('run_created', 'status_changed', 'waiting_started', 'resumed', "
    "'evidence_added', 'verification_updated', 'run_completed', 'run_failed', "
    "'run_cancelled', 'delivery_succeeded', 'delivery_failed', "
    "'channel_delivery_delivered', 'channel_delivery_failed')"
)
_NEW_EVENT_TYPE_CHECK = (
    "event_type IN ('run_created', 'status_changed', 'waiting_started', 'resumed', "
    "'evidence_added', 'verification_updated', 'run_completed', 'run_failed', "
    "'run_cancelled', 'delivery_succeeded', 'delivery_failed', "
    "'channel_delivery_delivered', 'channel_delivery_failed', "
    "'memory_consolidation_skipped')"
)


def _inspector():
    conn = op.get_bind()
    return sa.inspect(conn)


def _existing_check() -> dict | None:
    return next(
        (check for check in _inspector().get_check_constraints("agent_run_events") if check["name"] == _CHECK_NAME),
        None,
    )


def _has_memory_value(check: dict | None) -> bool:
    return check is not None and "memory_consolidation_skipped" in (check.get("sqltext") or "")


def upgrade() -> None:
    check = _existing_check()
    if _has_memory_value(check):
        # Fresh environment: 001 already built the table from the current
        # model, so the widened check is in place.
        return
    if check is not None:
        op.drop_constraint(_CHECK_NAME, "agent_run_events", type_="check")
    op.create_check_constraint(_CHECK_NAME, "agent_run_events", _NEW_EVENT_TYPE_CHECK)


def downgrade() -> None:
    check = _existing_check()
    if not _has_memory_value(check):
        # The widened value is already absent; nothing to restore.
        return
    op.drop_constraint(_CHECK_NAME, "agent_run_events", type_="check")
    op.create_check_constraint(_CHECK_NAME, "agent_run_events", _OLD_EVENT_TYPE_CHECK)
