"""Files-read pipeline (P1) — pure construction seam tests.

Ticket 01 / P1: ``build_files_read`` builds the deterministic ADD-only
"files already read, content unchanged" facts injected into the compaction
payload: succeeded ``read_file`` executions only, each path kept once (latest
read by settlement time wins), ordered by settlement time, bounded to 50
entries / ~2KB with oldest-first trimming.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services.agent_runtime.run_compactor import build_files_read


def _execution(
    *,
    execution_id: str,
    path: str = "src/app.py",
    tool_name: str = "read_file",
    status: str = "succeeded",
    content_hash: str | None = "abc123",
    started_at: datetime | str = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC),
) -> SimpleNamespace:
    metadata = {"content_hash": content_hash} if content_hash is not None else {}
    return SimpleNamespace(
        id=execution_id,
        tool_call_id=f"call-{execution_id}",
        tool_name=tool_name,
        status=status,
        sanitized_arguments={"path": path},
        result_metadata=metadata,
        started_at=started_at,
    )


def _serialized_bytes(entries: list) -> int:
    return len(json.dumps(entries, ensure_ascii=False).encode("utf-8"))


def test_builds_succeeded_read_file_entries_ordered_by_settlement_time() -> None:
    base = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)
    executions = [
        _execution(
            execution_id="e2",
            path="src/b.py",
            started_at=base + timedelta(minutes=2),
        ),
        _execution(execution_id="e1", path="src/a.py", started_at=base),
        _execution(
            execution_id="e3",
            path="src/c.py",
            tool_name="edit_file",
            started_at=base + timedelta(minutes=3),
        ),
        _execution(
            execution_id="e4",
            path="src/d.py",
            status="failed",
            started_at=base + timedelta(minutes=4),
        ),
        _execution(
            execution_id="e5",
            path="src/e.py",
            status="started",
            started_at=base + timedelta(minutes=5),
        ),
    ]

    entries = build_files_read(executions)

    assert [entry["path"] for entry in entries] == ["src/a.py", "src/b.py"]
    assert all(
        set(entry) == {"path", "content_hash", "last_read_at"} for entry in entries
    )


def test_path_dedup_keeps_latest_read() -> None:
    base = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)
    executions = [
        _execution(
            execution_id="e1",
            path="src/app.py",
            content_hash="old",
            started_at=base,
        ),
        _execution(
            execution_id="e2",
            path="src/app.py",
            content_hash="new",
            started_at=base + timedelta(minutes=1),
        ),
    ]

    entries = build_files_read(executions)

    assert len(entries) == 1
    assert entries[0]["path"] == "src/app.py"
    assert entries[0]["content_hash"] == "new"


def test_missing_hash_or_path_are_skipped() -> None:
    no_hash = _execution(execution_id="e1", content_hash=None)
    no_metadata = _execution(execution_id="e2")
    no_metadata.result_metadata = None
    no_path = _execution(execution_id="e3")
    no_path.sanitized_arguments = {}

    assert build_files_read([no_hash, no_metadata, no_path]) == []


def test_capacity_keeps_most_recent_fifty() -> None:
    base = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)
    executions = [
        _execution(
            execution_id=f"e{i}",
            path=f"src/f{i}.py",
            started_at=base + timedelta(minutes=i),
        )
        for i in range(60)
    ]

    # Isolate the count bound: a generous byte budget keeps only the 50-cap.
    entries = build_files_read(executions, max_bytes=1_000_000)

    assert len(entries) == 50
    assert entries[0]["path"] == "src/f10.py"
    assert entries[-1]["path"] == "src/f59.py"


def test_byte_budget_trims_oldest_entries_first() -> None:
    base = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)
    executions = [
        _execution(
            execution_id=f"e{i}",
            path=f"src/very_long_path_name_{i}.py",
            content_hash="h" * 64,
            started_at=base + timedelta(minutes=i),
        )
        for i in range(60)
    ]

    entries = build_files_read(executions)

    assert _serialized_bytes(entries) <= 2048
    assert entries
    assert entries[-1]["path"] == "src/very_long_path_name_59.py"
    assert entries[0]["path"] != "src/very_long_path_name_0.py"


def test_preserves_string_timestamps_and_entry_shape() -> None:
    execution = _execution(
        execution_id="e1",
        path="src/app.py",
        started_at="2026-09-04T12:00:00+00:00",
    )

    entries = build_files_read([execution])

    assert len(entries) == 1
    assert entries[0] == {
        "path": "src/app.py",
        "content_hash": "abc123",
        "last_read_at": "2026-09-04T12:00:00+00:00",
    }


def test_empty_input_returns_empty_list() -> None:
    assert build_files_read([]) == []
