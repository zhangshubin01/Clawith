"""Model-side ``read_file`` deduplication (P0).

A weak-working-memory model re-reads unchanged files after compaction,
re-inflating the context and stalling the run.  This module decides, from the
execution ledger and the *current compaction cycle's* messages, which repeated
``read_file`` results should stop being fed to the model in full.

Design contract (see docs/technical-plans/20260905-read-dedup-production-plan.md):

- **No new checkpoint state.**  The seen-count is computed from the execution
  ledger (``result_metadata.content_hash`` + ``sanitized_arguments.path``) and
  the current cycle's messages.  Compaction clears messages, so the count
  resets automatically — "within-cycle" semantics for free.
- **Segment-level hash.**  ``content_hash`` is ``sha256(full summary)``
  (``tool_execution.py:649``), so reading a *different* segment (``offset`` /
  ``limit`` change) or a file *after a write* yields a different hash and is
  never deduplicated ("better to leak than to wrongly drop").
- **Soft placeholder, not bare.**  The placeholder carries a head/tail preview
  and a note that the full body is archived and force-re-readable, so the model
  can still recover the real content when it genuinely needs it.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

DEFAULT_READ_DEDUP_N = 3

# P2 stall-signal bounds: a sliding window of the most recent ``read_file``
# results in which the fraction of re-reads (a key already seen within the
# window) is measured. Interleaved re-reads are invisible to the consecutive
# ``_trailing_identical_calls`` breaker, so this ratio is a separate dimension.
DEFAULT_STALL_WINDOW = 20
DEFAULT_STALL_RATIO = 0.7

_HEAD_PREVIEW_CHARS = 120
_TAIL_PREVIEW_CHARS = 120


def _preview(content: str | None) -> str:
    """A short head/tail window of a tool result body for the placeholder."""
    text = content.strip() if isinstance(content, str) else ""
    if not text:
        return ""
    if len(text) <= _HEAD_PREVIEW_CHARS + _TAIL_PREVIEW_CHARS:
        return text
    head = text[:_HEAD_PREVIEW_CHARS]
    tail = text[-_TAIL_PREVIEW_CHARS:]
    return f"{head} …… {tail}"


def read_dedup_placeholder(
    path: str,
    seen_count: int,
    content: str | None = None,
) -> str:
    """The soft placeholder replacing a repeated read_file body.

    ``seen_count`` is the 1-based occurrence index of this read within the
    cycle (the value that just exceeded the threshold N).
    """
    note = (
        f"📄 {path} — 内容未变（本周期已读 {seen_count} 次），此处省略正文。"
        "完整内容已归档，需要时请用 read_file 重新读取。"
    )
    preview = _preview(content)
    if preview:
        note = f"{note}\n预览：{preview}"
    return note


def build_read_dedup_map(
    executions: Sequence[Any],
    messages: Sequence[Mapping[str, Any]],
    n: int = DEFAULT_READ_DEDUP_N,
) -> dict[str, dict[str, Any]]:
    """Return ``{tool_call_id: {path, seen_count}}`` for reads to deduplicate.

    Walks the current cycle's messages (``runtime_messages_as_json(state)``)
    in order and counts, per ``(path, content_hash)``, how many times a
    *succeeded* ``read_file`` result has already been fed to the model.  Once
    the count exceeds ``n``, that tool result is marked for a soft placeholder.

    Only ``tool_call_id`` present in ``executions`` is considered; failed reads
    (``status != "succeeded"``) and reads without a usable hash/path are never
    deduplicated.  ``n < 1`` disables deduplication entirely.
    """
    if n < 1:
        return {}

    exec_by_id: dict[str, Any] = {}
    for execution in executions:
        call_id = getattr(execution, "tool_call_id", None)
        if isinstance(call_id, str) and call_id:
            exec_by_id[call_id] = execution

    seen: dict[tuple[str, str], int] = {}
    result: dict[str, dict[str, Any]] = {}
    for raw in messages:
        if not isinstance(raw, Mapping):
            continue
        if raw.get("role") != "tool":
            continue
        call_id = raw.get("tool_call_id")
        if not isinstance(call_id, str) or not call_id:
            continue
        execution = exec_by_id.get(call_id)
        if execution is None:
            continue
        if getattr(execution, "tool_name", None) != "read_file":
            continue
        if getattr(execution, "status", None) != "succeeded":
            continue

        metadata = getattr(execution, "result_metadata", None)
        content_hash = metadata.get("content_hash") if isinstance(metadata, Mapping) else None
        arguments = getattr(execution, "sanitized_arguments", None)
        path = arguments.get("path") if isinstance(arguments, Mapping) else None
        if not isinstance(content_hash, str) or not content_hash:
            continue
        if not isinstance(path, str) or not path:
            continue

        key = (path, content_hash)
        seen[key] = seen.get(key, 0) + 1
        count = seen[key]
        if count > n:
            result[call_id] = {"path": path, "seen_count": count}

    return result


def build_dup_read_ratio(
    executions: Sequence[Any],
    messages: Sequence[Mapping[str, Any]],
    *,
    window: int = DEFAULT_STALL_WINDOW,
) -> tuple[int, int] | None:
    """Return ``(dup_count, total)`` over the trailing ``window`` read_file results.

    Walks the current cycle's messages in order and keeps the last ``window``
    succeeded ``read_file`` results (resolved via ``tool_call_id`` → execution).
    Each result's identity key is ``(path, content_hash)``; a result is a
    *duplicate* when its key already appeared earlier within the window — an
    interleaved re-read, which ``_trailing_identical_calls`` cannot catch
    because it only counts *consecutive* identical calls. ``dup_count`` = total
    minus distinct keys; the caller derives ``dup_count / total`` and compares
    it against the ``stall_ratio`` threshold.

    Returns ``None`` when there are no read_file results in the window (or
    ``window < 1`` disables the signal). Failed reads and reads without a
    usable hash/path are ignored entirely: they never enter the denominator and
    never count as duplicates, so a mix of failed/partial reads cannot mask a
    real stall.
    """
    if window < 1:
        return None

    exec_by_id: dict[str, Any] = {}
    for execution in executions:
        call_id = getattr(execution, "tool_call_id", None)
        if isinstance(call_id, str) and call_id:
            exec_by_id[call_id] = execution

    keys: list[tuple[str, str]] = []
    for raw in messages:
        if not isinstance(raw, Mapping):
            continue
        if raw.get("role") != "tool":
            continue
        call_id = raw.get("tool_call_id")
        if not isinstance(call_id, str) or not call_id:
            continue
        execution = exec_by_id.get(call_id)
        if execution is None:
            continue
        if getattr(execution, "tool_name", None) != "read_file":
            continue
        if getattr(execution, "status", None) != "succeeded":
            continue

        metadata = getattr(execution, "result_metadata", None)
        content_hash = metadata.get("content_hash") if isinstance(metadata, Mapping) else None
        arguments = getattr(execution, "sanitized_arguments", None)
        path = arguments.get("path") if isinstance(arguments, Mapping) else None
        if not isinstance(content_hash, str) or not content_hash:
            continue
        if not isinstance(path, str) or not path:
            continue
        keys.append((path, content_hash))

    if not keys:
        return None
    window_keys = keys[-window:]
    total = len(window_keys)
    dup_count = total - len(set(window_keys))
    return dup_count, total
