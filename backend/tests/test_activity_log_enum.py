"""activity_action_enum must cover every circuit-breaker action_type.

Regression: the five ``runtime_*`` values were added to the model enum and the
PostgreSQL enum (migration f075_add_runtime_activity_enum) after circuit-breaker
audit events were silently lost — ``log_activity`` swallows the invalid-enum
INSERT error, so a missing value means zero DB trace of a fired breaker
(2026-09-05 compaction-amnesia 764eb591 investigation, §四·根因4).
"""

from app.models.activity_log import AgentActivityLog

_RUNTIME_BREAKER_VALUES = frozenset(
    {
        "runtime_compaction_loop",
        "runtime_tool_config_failure_loop",
        "runtime_tool_success_loop",
        "runtime_duplicate_read_stall",
        "runtime_duplicate_read_stall_compact",
    }
)


def test_activity_action_enum_has_runtime_breaker_values() -> None:
    column = AgentActivityLog.__table__.c.action_type
    enum_values = set(column.type.enums)
    assert _RUNTIME_BREAKER_VALUES <= enum_values
