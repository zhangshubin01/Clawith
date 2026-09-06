"""Add the three runtime_no_progress_* values to activity_action_enum.

The no-progress circuit breaker (model_step_service.py) records its nudge /
pivot / stop events via ``log_activity`` with ``action_type`` in
{runtime_no_progress_nudge, runtime_no_progress_pivot, runtime_no_progress_stop},
but the PostgreSQL enum lacked these values, so the INSERT failed with
"invalid input value for enum activity_action_enum" and ``log_activity``
swallowed the error — the no-progress breaker's audit events were silently lost.

Follow-up to f075_add_runtime_activity_enum (the five runtime_* values),
which did not yet cover the no-progress signal levels emitted at
model_step_service.py:3159 (``runtime_no_progress_{level}``).

Idempotent:
    - ADD VALUE IF NOT EXISTS, mirroring f075_add_runtime_activity_enum.py.

Revision ID: f076_add_no_progress_enum
Revises: f075_add_runtime_activity_enum
Create Date: 2026-09-05 00:00:00
"""

from typing import Sequence, Union

from alembic import op

revision: str = "f076_add_no_progress_enum"
down_revision: Union[str, Sequence[str], None] = "f075_add_runtime_activity_enum"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Keep the PostgreSQL enum in sync with AgentActivityLog.action_type.
    for value in (
        "runtime_no_progress_nudge",
        "runtime_no_progress_pivot",
        "runtime_no_progress_stop",
    ):
        op.execute(
            f"ALTER TYPE activity_action_enum ADD VALUE IF NOT EXISTS '{value}'"
        )


def downgrade() -> None:
    # PostgreSQL cannot safely remove enum values in-place.
    pass
