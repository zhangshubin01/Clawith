"""Completed-actions pipeline (A) — pure construction seam tests.

Ticket A / S2: ``build_completed_actions`` builds the deterministic
ADD-only progress facts injected into the compaction payload: succeeded
executions only, ordered by settlement time, deduplicated by execution id,
bounded to 50 entries / ~2KB with oldest-first trimming.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services.agent_runtime.run_compactor import build_completed_actions


def _execution(
    *,
    execution_id: str,
    call_id: str,
    tool_name: str = "edit_file",
    status: str = "succeeded",
    path: str | None = "src/app.py",
    summary: str | None = "Replaced 1 occurrence(s).",
    started_at: datetime | str = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC),
) -> SimpleNamespace:
    arguments = {"path": path} if path is not None else {}
    return SimpleNamespace(
        id=execution_id,
        tool_call_id=call_id,
        tool_name=tool_name,
        status=status,
        sanitized_arguments=arguments,
        result_summary=summary,
        started_at=started_at,
    )


def _serialized_bytes(actions: list) -> int:
    return len(json.dumps(actions, ensure_ascii=False).encode("utf-8"))


def test_builds_succeeded_actions_ordered_by_settlement_time() -> None:
    base = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)
    executions = [
        _execution(execution_id="e2", call_id="c2", started_at=base + timedelta(minutes=2)),
        _execution(execution_id="e1", call_id="c1", started_at=base),
        _execution(execution_id="e3", call_id="c3", status="failed", started_at=base + timedelta(minutes=3)),
        _execution(execution_id="e4", call_id="c4", status="started", started_at=base + timedelta(minutes=4)),
    ]

    actions = build_completed_actions(executions)

    assert [action["call_id"] for action in actions] == ["c1", "c2"]
    assert all(action["status"] == "succeeded" for action in actions)


def test_deduplicates_by_execution_id() -> None:
    execution = _execution(execution_id="e1", call_id="c1")
    actions = build_completed_actions([execution, execution, execution])
    assert len(actions) == 1


def test_capacity_keeps_most_recent_fifty() -> None:
    base = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)
    executions = [
        _execution(
            execution_id=f"e{i}",
            call_id=f"c{i}",
            started_at=base + timedelta(minutes=i),
        )
        for i in range(60)
    ]

    # Isolate the count bound: a generous byte budget keeps only the 50-cap.
    actions = build_completed_actions(executions, max_bytes=1_000_000)

    assert len(actions) == 50
    # The oldest ten were trimmed; the pipeline keeps recent progress.
    assert actions[0]["call_id"] == "c10"
    assert actions[-1]["call_id"] == "c59"


def test_byte_budget_trims_oldest_entries_first() -> None:
    base = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)
    long_summary = "Replaced occurrence(s) " + ("x" * 120)
    executions = [
        _execution(
            execution_id=f"e{i}",
            call_id=f"c{i}",
            summary=long_summary,
            started_at=base + timedelta(minutes=i),
        )
        for i in range(60)
    ]

    actions = build_completed_actions(executions)

    assert _serialized_bytes(actions) <= 2048
    assert actions  # never trims everything
    # Recent entries survive; the trimmed ones are the oldest.
    assert actions[-1]["call_id"] == "c59"
    assert actions[0]["call_id"] != "c0"
    for action in actions:
        assert len(action["summary"]) <= 200


def test_entry_shape_and_path_extraction() -> None:
    with_path = _execution(execution_id="e1", call_id="c1", path="src/app.py")
    without_path = _execution(execution_id="e2", call_id="c2", path=None)

    actions = build_completed_actions([without_path, with_path])

    # Ascending settlement order: e1 (with path) first, e2 (no path) second.
    first, second = actions
    assert set(first) == {"call_id", "tool", "status", "summary", "settled_at", "path"}
    assert first["tool"] == "edit_file"
    assert first["path"] == "src/app.py"
    assert first["settled_at"] == "2026-09-04T12:00:00+00:00"
    assert first["summary"] == "Replaced 1 occurrence(s)."
    assert "path" not in second


def test_summary_capped_at_200_chars() -> None:
    execution = _execution(
        execution_id="e1",
        call_id="c1",
        summary="y" * 500,
    )
    actions = build_completed_actions([execution])
    assert len(actions[0]["summary"]) == 200


def test_ignores_non_string_path_and_preserves_string_timestamps() -> None:
    execution = _execution(
        execution_id="e1",
        call_id="c1",
        started_at="2026-09-04T12:00:00+00:00",
    )
    execution.sanitized_arguments = {"path": 12345}
    actions = build_completed_actions([execution])
    assert "path" not in actions[0]
    assert actions[0]["settled_at"] == "2026-09-04T12:00:00+00:00"


def test_empty_input_returns_empty_list() -> None:
    assert build_completed_actions([]) == []
