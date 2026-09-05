"""Add the five runtime circuit-breaker values to activity_action_enum.

The circuit-breaker auditors (model_step_service.py) record runtime loop /
failure / stall events via ``log_activity`` with ``action_type`` in
{runtime_compaction_loop, runtime_tool_config_failure_loop,
 runtime_tool_success_loop, runtime_duplicate_read_stall,
 runtime_duplicate_read_stall_compact}, but the PostgreSQL enum only had the
original 14 values, so the INSERT failed with "invalid input value for enum
activity_action_enum" and ``log_activity`` swallowed the error — circuit-breaker
audit events were silently lost (agent_activity_logs 0 rows; 2026-09-05
compaction-amnesia 764eb591 investigation, docs/analysis/
2026-09-05-compaction-amnesia-764eb591.md §四·根因4).

Idempotent:
    - ADD VALUE IF NOT EXISTS, mirroring add_agent_file_activity_enum.py.

Revision ID: f075_add_runtime_activity_enum
Revises: f074_add_stall_guard
Create Date: 2026-09-05 00:00:00
"""

from typing import Sequence, Union

from alembic import op

revision: str = "f075_add_runtime_activity_enum"
down_revision: Union[str, Sequence[str], None] = "f074_add_stall_guard"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Keep the PostgreSQL enum in sync with AgentActivityLog.action_type.
    for value in (
        "runtime_compaction_loop",
        "runtime_tool_config_failure_loop",
        "runtime_tool_success_loop",
        "runtime_duplicate_read_stall",
        "runtime_duplicate_read_stall_compact",
    ):
        op.execute(
            f"ALTER TYPE activity_action_enum ADD VALUE IF NOT EXISTS '{value}'"
        )


def downgrade() -> None:
    # PostgreSQL cannot safely remove enum values in-place.
    pass
