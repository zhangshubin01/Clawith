"""Model-visible boundaries for one shared LangGraph Thread."""

from collections.abc import Mapping, Sequence

from app.services.agent_runtime.state import JsonObject
from app.services.llm.finish import FINISH_PROTOCOL_REMINDER


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


def _prior_run_summary(
    prior_messages: Sequence[Mapping[str, object]],
    *,
    current_run_id: str,
) -> JsonObject | None:
    """Collapse the prior Run into one deterministic, single-line context note.

    The prior Run's own closing assistant reply is the most toxic pollution
    source (a stale ``✅ done`` summary replayed verbatim), so it is never
    copied. Only the prior goal and its produced artifact references survive,
    as a compact template note — zero model cost, deterministic.

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

    parts = ["历史上下文（非当前任务）：上一轮已完成"]
    if goal:
        parts.append(f"任务「{goal}」")
    if unique:
        parts.append("产出 " + "、".join(unique))
    content = "，".join(parts) + "。"
    return {
        "id": f"prior-run-summary:{current_run_id}",
        "role": "user",
        "content": content,
        "runtime_input": "prior_run_summary",
    }


def bound_current_run_window(
    messages: Sequence[Mapping[str, object]],
    *,
    current_run_id: str,
) -> tuple[JsonObject | None, tuple[JsonObject, ...]]:
    """Bound the model window at the current Run's start marker.

    Returns ``(prior_run_summary, current_run_messages)``. When the Thread
    carries a prior Run's messages, they are collapsed into a single
    deterministic summary message instead of leaking verbatim into the window;
    ``current_run_messages`` starts exactly at the current Run's marker.

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
    summary = _prior_run_summary(prior, current_run_id=current_run_id)
    return summary, current


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
]
