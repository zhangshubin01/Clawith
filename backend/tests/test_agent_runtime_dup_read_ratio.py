"""Duplicate-read stall ratio (P2) — pure seam tests.

``build_dup_read_ratio`` measures, over a sliding window of the most recent
``read_file`` results, how many are re-reads of a ``(path, content_hash)`` key
already seen within the window.  Interleaved re-reads (A, B, A, ...) are the
blind spot of the consecutive ``_trailing_identical_calls`` breaker; this ratio
is the complementary dimension.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.agent_runtime.read_dedup import (
    DEFAULT_STALL_RATIO,
    DEFAULT_STALL_WINDOW,
    build_dup_read_ratio,
)
from app.services.agent_runtime.model_step_service import _dup_read_reminder_message


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


def _read(*call_ids: str) -> list:
    messages: list = []
    for call_id in call_ids:
        messages.append({"role": "assistant", "tool_calls": []})
        messages.append({"role": "tool", "tool_call_id": call_id})
    return messages


def _ratio(executions: list, messages: list, *, window: int = 20) -> float:
    signal = build_dup_read_ratio(executions, messages, window=window)
    assert signal is not None
    dup_count, total = signal
    return dup_count / total


def test_interleaved_rereads_are_counted_as_duplicates() -> None:
    # A, B, C, A, B, C — six reads, three distinct keys, three duplicates.
    executions = [
        _execution(call_id=f"c{i}", path=p, content_hash=h)
        for i, (p, h) in enumerate(
            [
                ("a.py", "h-a"),
                ("b.py", "h-b"),
                ("c.py", "h-c"),
                ("a.py", "h-a"),
                ("b.py", "h-b"),
                ("c.py", "h-c"),
            ],
            start=1,
        )
    ]
    messages = _read("c1", "c2", "c3", "c4", "c5", "c6")

    signal = build_dup_read_ratio(executions, messages, window=20)

    assert signal == (3, 6)


def test_long_task_reading_new_files_has_zero_ratio() -> None:
    # Distinct keys -> no duplicates -> the signal can never trigger.
    executions = [
        _execution(call_id=f"c{i}", path=f"src/file{i}.py", content_hash=f"hash-{i}")
        for i in range(1, 21)
    ]
    messages = _read(*(f"c{i}" for i in range(1, 21)))

    assert build_dup_read_ratio(executions, messages, window=20) == (0, 20)
    assert _ratio(executions, messages) == 0.0


def test_sustained_reread_of_one_file_near_saturates_ratio() -> None:
    executions = [
        _execution(call_id=f"c{i}", path="a.py", content_hash="h-a")
        for i in range(1, 21)
    ]
    messages = _read(*(f"c{i}" for i in range(1, 21)))

    assert build_dup_read_ratio(executions, messages, window=20) == (19, 20)


def test_window_slices_to_last_n_read_file_results() -> None:
    # 10 distinct reads, then 5 re-reads of a single file: only the trailing
    # window matters, so older distinct reads fall out of the count.
    executions = [
        _execution(call_id=f"c{i}", path=f"old{i}.py", content_hash=f"old-{i}")
        for i in range(1, 11)
    ] + [
        _execution(call_id=f"c{i}", path="a.py", content_hash="h-a")
        for i in range(11, 16)
    ]
    messages = _read(*(f"c{i}" for i in range(1, 16)))

    # window=5 -> the trailing five reads (all "a.py"): 4 duplicates, 5 total.
    assert build_dup_read_ratio(executions, messages, window=5) == (4, 5)


def test_failed_reads_never_enter_the_window() -> None:
    executions = [
        _execution(call_id="c1", status="failed", content_hash=None),
        _execution(call_id="c2", content_hash="h"),
        _execution(call_id="c3", content_hash="h"),
        _execution(call_id="c4", content_hash="h"),
    ]
    messages = _read("c1", "c2", "c3", "c4")

    # Only three succeeded reads enter; two duplicates among them.
    assert build_dup_read_ratio(executions, messages, window=20) == (2, 3)


def test_non_read_file_tools_are_ignored() -> None:
    executions = [
        _execution(call_id="c1", tool_name="edit_file", content_hash="h"),
        _execution(call_id="c2", tool_name="edit_file", content_hash="h"),
    ]
    messages = _read("c1", "c2")

    assert build_dup_read_ratio(executions, messages) is None


def test_missing_hash_or_path_are_skipped() -> None:
    executions = [
        _execution(call_id="c1", content_hash=None),
        _execution(call_id="c2", path=None),
    ]
    messages = _read("c1", "c2")

    assert build_dup_read_ratio(executions, messages) is None


def test_window_below_one_disables_signal() -> None:
    executions = [_execution(call_id="c1")]
    messages = _read("c1")

    assert build_dup_read_ratio(executions, messages, window=0) is None


def test_empty_inputs_return_none() -> None:
    assert build_dup_read_ratio([], []) is None
    assert build_dup_read_ratio([], [{"role": "tool", "tool_call_id": "ghost"}]) is None


def test_ratio_threshold_exceeds_default_for_stall() -> None:
    # Cycling 7 files keeps 7 distinct keys in a 20-window -> 13/20 duplicates
    # (0.65), below the default 0.7; cycling 2 files -> 18/20 (0.9), above it.
    low_executions = [
        _execution(call_id=f"c{i}", path=f"f{(i % 7) + 1}.py", content_hash=f"h{(i % 7) + 1}")
        for i in range(1, 21)
    ]
    low_messages = _read(*(f"c{i}" for i in range(1, 21)))
    dup_count, total = build_dup_read_ratio(low_executions, low_messages, window=20)
    assert dup_count / total < DEFAULT_STALL_RATIO

    high_executions = [
        _execution(call_id=f"c{i}", path=f"g{(i % 2) + 1}.py", content_hash=f"k{(i % 2) + 1}")
        for i in range(1, 21)
    ]
    high_messages = _read(*(f"c{i}" for i in range(1, 21)))
    dup_count, total = build_dup_read_ratio(high_executions, high_messages, window=20)
    assert dup_count / total >= DEFAULT_STALL_RATIO


def test_default_bounds() -> None:
    assert DEFAULT_STALL_WINDOW == 20
    assert DEFAULT_STALL_RATIO == 0.7


def test_reminder_message_is_imperative_and_ratio_accurate() -> None:
    message = _dup_read_reminder_message(18, 20)

    assert message.role == "user"
    assert "18" in message.content
    assert "20" in message.content
    assert "重复" in message.content
    assert "90%" in message.content  # 18/20 rounds to 90%
    assert "停止反复读取" in message.content
    # 字节确定性：同信号 → 同消息（前缀缓存友好）
    assert message == _dup_read_reminder_message(18, 20)
