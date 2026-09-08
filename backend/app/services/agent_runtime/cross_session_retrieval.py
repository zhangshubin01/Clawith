"""Cross-session open-list item injection (R3 of Run-context inheritance).

Active ``agent_list_items`` for the Run's list scope (platform-derived project,
falling back to a per-session scope) are injected into the model window as a
bounded **numbered index** in a past-tense "非当前任务" note, so the model can
align a goal's number references (``那执行 #1+#2``) to list entries without any
regex parsing of user wording — no extraction means no mis-truncation.

No active items for the scope → strict no-op. The wording mirrors
``_prior_run_summary`` and ``render_task_state_note`` so the note is never
mistaken for a new directive; the item and character bounds are code-level
constants aligned with the focus injection (``limit_active``/``max_chars``).
"""

from __future__ import annotations

from dataclasses import dataclass
import uuid

from app.dao import list_dao
from app.services.agent_runtime.state import JsonObject

# Bounded injection: at most this many active items, then a character ceiling
# (aligned with the focus injection bounds) so a long list cannot blow up the
# model budget.
MAX_INJECTED_ITEMS = 5
MAX_INJECTED_CHARS = 1500


@dataclass(frozen=True, slots=True)
class InjectedListItem:
    """One active list item selected for injection.

    ``sort_order`` is the stable visible number (the identity the user
    references); ``title`` is the display title (``key`` when no title was
    stored); ``key`` is the completion credential.
    """

    sort_order: int
    title: str
    key: str


@dataclass(frozen=True, slots=True)
class ListRetrievalResult:
    """The active items selected for injection into the model window.

    ``items`` holds at most ``MAX_INJECTED_ITEMS`` entries in stable
    ``sort_order``; ``total_active`` is the scope's true active-item count so
    the note can mark truncation instead of silently dropping entries.
    """

    items: tuple[InjectedListItem, ...]
    total_active: int


def render_retrieval_note(
    result: ListRetrievalResult,
    *,
    current_run_id: str,
) -> JsonObject:
    """Render the injected index as a past-tense, non-imperative note.

    Wording shares the "非当前任务" frame with ``_prior_run_summary`` and
    ``render_task_state_note``; descriptions never enter the note (the tools
    are the path to full text), and truncation is explicit rather than silent.
    """
    paragraphs = ["历史上下文（非当前任务）：此前已确认、尚未完结的清单："]
    lines = [f"{item.sort_order}. {item.title} ({item.key})" for item in result.items]
    if result.total_active > len(result.items):
        lines.append(f"（仅列出前 {len(result.items)} 项；完整内容见 list_list_items）")
    paragraphs.append("\n".join(lines))
    content = "\n\n".join(paragraphs)
    if len(content) > MAX_INJECTED_CHARS:
        content = content[:MAX_INJECTED_CHARS] + "...(truncated)"
    return {
        "id": f"cross-session-list:{current_run_id}",
        "role": "user",
        "content": content,
        "runtime_input": "cross_session_list",
    }


class CrossSessionListRetriever:
    """Retrieve the active-item numbered index for the Run's list scope.

    Best-effort by design: any read failure degrades to ``None`` so a Run's
    startup is never blocked by an unavailable store.
    """

    def __init__(self, *, max_injected_items: int = MAX_INJECTED_ITEMS) -> None:
        if max_injected_items <= 0:
            raise ValueError("max_injected_items must be positive")
        self._max_injected_items = max_injected_items

    async def retrieve(
        self,
        *,
        tenant_id: uuid.UUID,
        agent_id: uuid.UUID,
        user_id: uuid.UUID | None,
        session_id: uuid.UUID | None,
        project: str | None,
    ) -> ListRetrievalResult | None:
        """Return the active-item index, or ``None`` on a miss (no-op).

        Unconditional: whichever active items resolve for the scope are
        injected, without inspecting the Run's goal wording. ``tenant_id`` and
        ``user_id`` are accepted for caller-interface stability but unused —
        ``agent_list_items`` is keyed by (agent_id, project), not tenant.
        """
        scope = project or (f"session:{session_id}" if session_id else None)
        if scope is None:
            return None
        items = await list_dao.list_by_project(
            agent_id=agent_id,
            project=scope,
            include_completed=False,
        )
        if not items:
            return None
        bounded = items[: self._max_injected_items]
        return ListRetrievalResult(
            items=tuple(
                InjectedListItem(
                    sort_order=item.sort_order,
                    title=item.title or item.key,
                    key=item.key,
                )
                for item in bounded
            ),
            total_active=len(items),
        )


__all__ = [
    "MAX_INJECTED_ITEMS",
    "MAX_INJECTED_CHARS",
    "CrossSessionListRetriever",
    "InjectedListItem",
    "ListRetrievalResult",
    "render_retrieval_note",
]
