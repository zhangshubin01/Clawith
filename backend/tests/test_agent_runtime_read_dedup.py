"""Read-dedup (P0) — pure seam tests.

``build_read_dedup_map`` marks, from the execution ledger and the current
cycle's messages, which repeated ``read_file`` results should be replaced by a
soft placeholder.  Behaviour-level: no DB, no checkpoint state — only the pure
decision function and the placeholder text.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.agent_runtime.read_dedup import (
    DEFAULT_READ_DEDUP_N,
    build_read_dedup_map,
    read_dedup_placeholder,
)


def _execution(
    *,
    call_id: str,
    tool_name: str = "read_file",
    status: str = "succeeded",
    path: str | None = "app/src/main/MainActivity.kt",
    content_hash: str | None = "hash-1",
) -> SimpleNamespace:
    arguments = {"path": path} if path is not None else {}
    metadata = {"content_hash": content_hash} if content_hash is not None else {}
    return SimpleNamespace(
        tool_call_id=call_id,
        tool_name=tool_name,
        status=status,
        result_metadata=metadata,
        sanitized_arguments=arguments,
    )


def _tool_message(call_id: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id}


def _read(*call_ids: str) -> list:
    """A cycle's tool messages interleaved with an assistant message."""
    messages: list = []
    for call_id in call_ids:
        messages.append({"role": "assistant", "tool_calls": []})
        messages.append(_tool_message(call_id))
    return messages


def test_first_n_reads_are_kept_then_deduplicated() -> None:
    executions = [_execution(call_id=f"c{i}") for i in range(1, 6)]
    messages = _read("c1", "c2", "c3", "c4", "c5")

    marked = build_read_dedup_map(executions, messages)

    # Same (path, content_hash): occurrences 4 and 5 exceed N=3.
    assert set(marked) == {"c4", "c5"}
    assert marked["c4"] == {"path": "app/src/main/MainActivity.kt", "seen_count": 4}


def test_different_segments_are_not_deduplicated() -> None:
    # offset/limit change -> different content_hash (segment hash) -> released.
    executions = [
        _execution(call_id="c1", content_hash="seg-0-2000"),
        _execution(call_id="c2", content_hash="seg-2001-4000"),
        _execution(call_id="c3", content_hash="seg-0-2000"),
        _execution(call_id="c4", content_hash="seg-0-2000"),
    ]
    messages = _read("c1", "c2", "c3", "c4")

    marked = build_read_dedup_map(executions, messages)

    # seg-0-2000 seen 3 times (c1,c3,c4) -> never exceeds N=3.
    assert marked == {}


def test_write_after_read_releases_repeated_read() -> None:
    # Same path but content changed after a write -> new hash -> not deduped.
    executions = [
        _execution(call_id="c1", content_hash="before-write"),
        _execution(call_id="c2", content_hash="before-write"),
        _execution(call_id="c3", content_hash="after-write"),
        _execution(call_id="c4", content_hash="after-write"),
        _execution(call_id="c5", content_hash="after-write"),
        _execution(call_id="c6", content_hash="after-write"),
    ]
    messages = _read("c1", "c2", "c3", "c4", "c5", "c6")

    marked = build_read_dedup_map(executions, messages)

    # "before-write" seen twice, "after-write" seen four times -> c6 (4th) deduped.
    assert set(marked) == {"c6"}


def test_failed_reads_never_deduplicated_and_do_not_consume_budget() -> None:
    executions = [
        _execution(call_id="c1", status="failed", content_hash=None),
        _execution(call_id="c2", status="succeeded", content_hash="h"),
        _execution(call_id="c3", status="succeeded", content_hash="h"),
        _execution(call_id="c4", status="succeeded", content_hash="h"),
    ]
    messages = _read("c1", "c2", "c3", "c4")

    marked = build_read_dedup_map(executions, messages)

    # Only succeeded reads count; three succeeded reads never exceed N=3.
    assert marked == {}


def test_non_read_file_tools_are_ignored() -> None:
    executions = [
        _execution(call_id="c1", tool_name="edit_file", content_hash="h"),
        _execution(call_id="c2", tool_name="edit_file", content_hash="h"),
        _execution(call_id="c3", tool_name="edit_file", content_hash="h"),
        _execution(call_id="c4", tool_name="edit_file", content_hash="h"),
    ]
    messages = _read("c1", "c2", "c3", "c4")

    assert build_read_dedup_map(executions, messages) == {}


def test_unknown_tool_call_ids_are_skipped() -> None:
    # A tool message whose call_id has no execution record must be ignored.
    executions = [_execution(call_id="c1")]
    messages = [_tool_message("ghost"), _tool_message("c1")]

    assert build_read_dedup_map(executions, messages) == {}


def test_missing_hash_or_path_are_never_deduplicated() -> None:
    executions = [
        _execution(call_id="c1", content_hash=None),
        _execution(call_id="c2", path=None),
        _execution(call_id="c3", content_hash="h"),
        _execution(call_id="c4", content_hash="h"),
        _execution(call_id="c5", content_hash="h"),
        _execution(call_id="c6", content_hash="h"),
    ]
    messages = _read("c1", "c2", "c3", "c4", "c5", "c6")

    marked = build_read_dedup_map(executions, messages)

    # c1/c2 lack hash/path -> skipped entirely; c3..c6 are four succeeded reads
    # of the same content -> c6 (4th) deduped.
    assert set(marked) == {"c6"}


def test_n_below_one_disables_deduplication() -> None:
    executions = [_execution(call_id=f"c{i}") for i in range(1, 6)]
    messages = _read("c1", "c2", "c3", "c4", "c5")

    assert build_read_dedup_map(executions, messages, n=0) == {}


def test_empty_inputs() -> None:
    assert build_read_dedup_map([], []) == {}
    assert build_read_dedup_map([], [_tool_message("c1")]) == {}


def test_placeholder_carries_path_count_and_preview() -> None:
    body = "line-1\n" + "x" * 300 + "\nline-end"
    placeholder = read_dedup_placeholder("app/src/main/MainActivity.kt", 4, body)

    assert "app/src/main/MainActivity.kt" in placeholder
    assert "已读 4 次" in placeholder
    assert "read_file" in placeholder
    # head/tail preview, not the full 300-char body.
    assert "line-1" in placeholder
    assert "line-end" in placeholder
    assert "x" * 300 not in placeholder


def test_placeholder_without_content_has_no_preview_line() -> None:
    placeholder = read_dedup_placeholder("a.txt", 5, None)
    assert "a.txt" in placeholder
    assert "已读 5 次" in placeholder
    assert "\n预览：" not in placeholder


def test_default_threshold_is_three() -> None:
    assert DEFAULT_READ_DEDUP_N == 3
