"""Model-visible boundaries for one shared LangGraph Thread."""

from collections.abc import Awaitable, Callable, Mapping, Sequence

from app.services.agent_runtime.state import JsonObject
from app.services.llm.finish import FINISH_PROTOCOL_REMINDER

# Deterministic head/marker/tail pruning for inlined prior tool results
# (deepseek-harness pruner style). The Run-inheritance plan spec allows up to
# head 4096 / tail 1024 code points; these budgets are conservatively smaller
# per the project's token-cost discipline. Python string slicing indexes code
# points, so the budget is encoding-independent.
_EXCERPT_HEAD_CODE_POINTS = 2048
_EXCERPT_TAIL_CODE_POINTS = 512
_EXCERPT_GAP_MARKER = "\n[…中间内容省略…]\n"
_TOOL_RESULT_REF_PREFIX = "tool-result://"


def _current_run_start(
    messages: Sequence[Mapping[str, object]],
    *,
    current_run_id: str,
) -> int | None:
    """Index of the current Run's start marker, or ``None`` if absent."""
    for index, message in enumerate(messages):
        if (
            message.get("runtime_input") == "current"
            and message.get("runtime_run_id") == current_run_id
        ):
            return index
    return None


def _truncate_tool_result_content(content: str) -> str:
    """Prune one resolved tool result to a bounded head/marker/tail excerpt."""
    if len(content) <= _EXCERPT_HEAD_CODE_POINTS + _EXCERPT_TAIL_CODE_POINTS:
        return content
    return (
        content[:_EXCERPT_HEAD_CODE_POINTS]
        + _EXCERPT_GAP_MARKER
        + content[-_EXCERPT_TAIL_CODE_POINTS:]
    )


async def _prior_run_summary(
    prior_messages: Sequence[Mapping[str, object]],
    *,
    current_run_id: str,
    resolve_tool_result: Callable[[str], Awaitable[str | None]] | None = None,
    completion_phrase: str | None = None,
    pending_lists_line: str | None = None,
) -> JsonObject | None:
    """Collapse the prior Run into one deterministic, bounded context note.

    The prior Run's own closing assistant reply is the most toxic pollution
    source (a stale ``✅ done`` summary replayed verbatim), so it is never
    copied. Only the prior goal and its produced artifacts survive, as a
    compact template note.

    With ``resolve_tool_result`` supplied, opaque ``tool-result://`` refs are
    resolved to their stored content and inlined as bounded head/marker/tail
    excerpts, replacing URIs the model cannot parse. Resolution is strictly
    best-effort: any failure falls back to the legacy raw ref so a Run's
    startup is never blocked by a missing or unreadable tool result.

    ``completion_phrase`` replaces the fixed "上一轮已完成" lead (D-9: the
    prior Run's actual phase wording); when ``None`` the legacy phrase is
    kept, backward-compatible. ``pending_lists_line`` is appended after the
    goal and artifacts when non-empty (the bounded 未决事项 pointer).

    The wording is deliberately past-tense and marked "非当前任务": a
    user-role summary that read "目标：重新编译项目" was itself mistaken for a
    new directive (the model replied "用户说…目标：重新编译项目" and recompiled).
    """
    goal = ""
    for message in reversed(prior_messages):
        if (
            message.get("runtime_input") == "current"
            and isinstance(message.get("content"), str)
        ):
            goal = message["content"].strip()
            break
    artifacts: list[str] = []
    for message in prior_messages:
        if message.get("role") != "tool":
            continue
        result_ref = message.get("result_ref")
        if isinstance(result_ref, str) and result_ref.strip():
            artifacts.append(result_ref.strip())
    # De-duplicate while preserving order; keep only the last few.
    seen: set[str] = set()
    unique: list[str] = []
    for artifact in reversed(artifacts):
        if artifact in seen:
            continue
        seen.add(artifact)
        unique.append(artifact)
    unique.reverse()
    unique = unique[-3:]

    parts = [f"历史上下文（非当前任务）：{completion_phrase or '上一轮已完成'}"]
    if goal:
        parts.append(f"任务「{goal}」")
    if unique:
        if resolve_tool_result is not None:
            excerpts: list[str] = []
            for ref in unique:
                excerpt: str | None = None
                if ref.startswith(_TOOL_RESULT_REF_PREFIX):
                    try:
                        resolved = await resolve_tool_result(ref)
                    except Exception:
                        # Fail-open bridge: the summary is best-effort
                        # context, and one unavailable tool result (storage
                        # outage, unsettled ledger, unexpected resolver error)
                        # must never block a Run from starting. Fall back to
                        # the legacy raw ref.
                        resolved = None
                    if resolved:
                        excerpt = _truncate_tool_result_content(resolved)
                excerpts.append(excerpt if excerpt is not None else ref)
            body = "\n\n".join(f"[{i}] {item}" for i, item in enumerate(excerpts, 1))
            parts.append(f"产出摘录：\n{body}")
        else:
            parts.append("产出 " + "、".join(unique))
    if pending_lists_line:
        parts.append(pending_lists_line)
    content = "，".join(parts) + "。"
    return {
        "id": f"prior-run-summary:{current_run_id}",
        "role": "user",
        "content": content,
        "runtime_input": "prior_run_summary",
    }


async def bound_current_run_window(
    messages: Sequence[Mapping[str, object]],
    *,
    current_run_id: str,
    resolve_tool_result: Callable[[str], Awaitable[str | None]] | None = None,
    completion_phrase: str | None = None,
    pending_lists_line: str | None = None,
) -> tuple[JsonObject | None, tuple[JsonObject, ...]]:
    """Bound the model window at the current Run's start marker.

    Returns ``(prior_run_summary, current_run_messages)``. When the Thread
    carries a prior Run's messages, they are collapsed into a single
    deterministic summary message instead of leaking verbatim into the window;
    ``current_run_messages`` starts exactly at the current Run's marker.
    ``resolve_tool_result`` optionally inlines resolved ``tool-result://``
    content into that summary as bounded excerpts (see ``_prior_run_summary``).
    ``completion_phrase`` and ``pending_lists_line`` pass through to the
    summary (D-9 phase-aware wording); both default ``None`` for the legacy
    backward-compatible fixed phrase and no pending pointer.

    Without a current marker (legacy single-Run threads, or non-direct Runs
    whose Thread holds only their own messages) the whole Thread is returned
    unchanged and the summary is ``None`` — a no-op.
    """
    copied = tuple(dict(message) for message in messages)
    current_start = _current_run_start(copied, current_run_id=current_run_id)
    if current_start is None or current_start == 0:
        return None, copied
    prior = copied[:current_start]
    current = copied[current_start:]
    summary = await _prior_run_summary(
        prior,
        current_run_id=current_run_id,
        resolve_tool_result=resolve_tool_result,
        completion_phrase=completion_phrase,
        pending_lists_line=pending_lists_line,
    )
    return summary, current


def summary_is_stale_for_run(
    messages: Sequence[Mapping[str, object]],
    *,
    current_run_id: str,
    summary_covered_through_message_id: str | None,
) -> bool:
    """Whether the running summary covers only history before the current Run.

    The LangGraph ``thread_summary`` channel is a cross-Run "work in progress"
    summary. When a Thread hosts multiple Runs, a summary covering only a
    *previous* Run's in-progress state (e.g. "builds still failing") poisons
    the new directive — the model anchors the new task onto the old one. That
    summary must not be fed to the model; ``_prior_run_summary`` already
    supplies the neutral "非当前任务" history note.

    The watermark alone cannot be compared positionally: compaction records
    ``summary_covered_through_message_id`` as the last compacted message's id
    and removes the whole compactable prefix in the same frame, so the
    watermark message is never present in ``messages`` again. The reliable
    signal is the current Run's marker position: if it is the first message,
    prior Runs are either absent or fully compacted away and the summary
    belongs to (or already covers) the current Run; if any message precedes
    the marker, prior-Run messages survive and the summary covers only
    pre-Run history.
    """
    if not summary_covered_through_message_id:
        return False
    current_start = _current_run_start(messages, current_run_id=current_run_id)
    if current_start is None or current_start == 0:
        return False
    return True


def model_visible_thread_messages(
    messages: Sequence[Mapping[str, object]],
    *,
    current_run_id: str,
) -> tuple[JsonObject, ...]:
    """Keep current-Run state and prior tool facts, not unpublished drafts."""
    copied = tuple(dict(message) for message in messages)
    current_start = _current_run_start(copied, current_run_id=current_run_id)
    if current_start is None:
        return copied

    visible: list[JsonObject] = []
    for index, message in enumerate(copied):
        if index >= current_start:
            visible.append(message)
            continue
        role = message.get("role")
        if role == "assistant" and not message.get("tool_calls"):
            # Accepted terminal replies are loaded from the product Session
            # snapshot. Thread-only plain assistant messages are candidates,
            # including drafts that never passed verification or delivery.
            continue
        if role == "user" and (
            message.get("runtime_intent") == "repair"
            or message.get("content") == FINISH_PROTOCOL_REMINDER
        ):
            continue
        visible.append(message)
    return tuple(visible)


__all__ = [
    "bound_current_run_window",
    "model_visible_thread_messages",
    "summary_is_stale_for_run",
]
