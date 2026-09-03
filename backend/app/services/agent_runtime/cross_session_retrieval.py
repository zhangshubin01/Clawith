"""Cross-session open-list title injection (R3 of Run-context inheritance).

While an open ``memory/清单.md`` pointer exists in the current session's
``session_context_states.open_items`` or in recent sessions, the Runtime
injects a bounded **title index** of those lists into the model window as a
past-tense "非当前任务" note. There is no intent detection: the note is
unconditional session-level standing context, so the model can align a goal's
number references (``那执行 1→2→3→4（P1）``) to list entries without any
regex parsing of user wording — no extraction means no mis-truncation.

Nothing resolves (no pointers, unreadable file, absent section) → strict
no-op. The wording mirrors ``_prior_run_summary`` and
``render_task_state_note`` so the note is never mistaken for a new directive;
the list-count and title bounds are shared with ``session_task_state`` so the
pointer line and this title index carry the same boundaries.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import logging
import uuid

from app.services.agent_runtime.command_worker import RuntimeSessionFactory
from app.services.agent_runtime.list_persistence import (
    LIST_FILE_PATH,
    ListItem,
    parse_list_file,
)
from app.services.agent_runtime.session_context_service import SessionContextService
from app.services.agent_runtime.session_task_state import (
    _MAX_PENDING_LIST_NOTES,
    _bounded_pending_title,
)
from app.services.agent_runtime.state import JsonObject, JsonValue
from app.services.storage import get_storage_backend, normalize_storage_key
from app.services.storage_runtime.base import StorageBackend

logger = logging.getLogger(__name__)

# Scope constants: how many past sessions to project over, and how many item
# titles one list may contribute to the window (bounded so a long list cannot
# blow up the model budget).
MAX_SESSIONS_DEFAULT = 5
MAX_INJECTED_ITEMS = 20

# ponytail: the private import below is deliberate — the three-list bound and
# the 30-char title bound are single-sourced from
# session_task_state.render_pending_lists_line so the pointer line and this
# title index never diverge. Promotion of those names to a shared module is
# the upgrade path when a third consumer appears.


@dataclass(frozen=True, slots=True)
class RetrievedListSection:
    """One open list selected for injection.

    ``items`` is the bounded title index (at most ``MAX_INJECTED_ITEMS``
    entries, number+title each); ``total_count`` is the list's true item count
    so the note can mark truncation instead of silently dropping entries.
    """

    title: str
    items: tuple[ListItem, ...]
    total_count: int


@dataclass(frozen=True, slots=True)
class ListRetrievalResult:
    """The open-list sections selected for injection into the model window.

    ``sections`` holds at most ``_MAX_PENDING_LIST_NOTES`` lists in pointer
    order (current session first, then recent sessions newest-first);
    ``total_lists`` is the number of resolved lists before the bound, so a
    value larger than ``len(sections)`` renders the trailing 等 marker.
    """

    sections: tuple[RetrievedListSection, ...]
    total_lists: int


def render_retrieval_note(
    result: ListRetrievalResult,
    *,
    current_run_id: str,
) -> JsonObject:
    """Render the injected title index as a past-tense, non-imperative note.

    Wording shares the "非当前任务" frame with ``_prior_run_summary`` and
    ``render_task_state_note``; descriptions never enter the note (the pointer
    line and ``read_file`` are the paths to full text), and truncation is
    explicit rather than silent.
    """
    paragraphs = ["历史上下文（非当前任务）：此前已确认、尚未完结的清单："]
    for section in result.sections:
        lines = [f"清单「{section.title}」（{section.total_count} 项）："]
        for item in section.items:
            lines.append(f"{item.number}. {item.title}")
        if section.total_count > len(section.items):
            lines.append(f"（仅列出前 {len(section.items)} 项；完整内容见 {LIST_FILE_PATH}）")
        paragraphs.append("\n".join(lines))
    content = "\n\n".join(paragraphs)
    if result.total_lists > len(result.sections):
        content += "等"
    return {
        "id": f"cross-session-list:{current_run_id}",
        "role": "user",
        "content": content,
        "runtime_input": "cross_session_list",
    }


def _collect_list_pointer_ids(
    open_items: Sequence[JsonValue],
    *,
    project: str | None,
) -> tuple[uuid.UUID, ...]:
    """Extract ordered, de-duplicated list pointer ids from one open_items array.

    Only rows whose ``list_ref`` is ``memory/清单.md`` participate. When
    ``project`` is known, exact-project rows come first, then session-level
    (``project=None``, D1 degradation) rows as a wildcard fallback — a list
    persisted without a resolvable project stays retrievable. When ``project``
    is unknown no project filtering applies at all.
    """
    exact: list[uuid.UUID] = []
    wildcard: list[uuid.UUID] = []
    for item in open_items:
        if not isinstance(item, Mapping):
            continue
        if item.get("list_ref") != LIST_FILE_PATH:
            continue
        raw_list_id = item.get("list_id")
        if not isinstance(raw_list_id, str):
            continue
        try:
            list_id = uuid.UUID(raw_list_id)
        except ValueError:
            continue
        row_project = item.get("project")
        if project is None or row_project == project:
            bucket = exact
        elif row_project is None:
            bucket = wildcard
        else:
            continue
        if list_id not in bucket:
            bucket.append(list_id)
    ids: list[uuid.UUID] = []
    for list_id in (*exact, *wildcard):
        if list_id not in ids:
            ids.append(list_id)
    return tuple(ids)


class CrossSessionListRetriever:
    """Retrieve the open-list title index across the current and recent sessions.

    Best-effort by design: any read failure degrades to ``None`` so a Run's
    startup is never blocked by a missing list file or an unavailable store.
    """

    def __init__(
        self,
        *,
        session_factory: RuntimeSessionFactory,
        context_service: SessionContextService | None = None,
        storage: StorageBackend | None = None,
        max_sessions: int = MAX_SESSIONS_DEFAULT,
    ) -> None:
        if max_sessions <= 0:
            raise ValueError("max_sessions must be positive")
        self._session_factory = session_factory
        self._context_service = context_service or SessionContextService()
        self._storage = storage
        self._max_sessions = max_sessions

    def _storage_backend(self) -> StorageBackend:
        return self._storage or get_storage_backend()

    async def _load_file(self, key: str) -> str:
        storage = self._storage_backend()
        try:
            if not await storage.exists(key) or not await storage.is_file(key):
                return ""
            return await storage.read_text(key, encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            logger.warning(
                "Failed to read list file %s for cross-session retrieval",
                key,
                exc_info=True,
            )
            return ""

    async def retrieve(
        self,
        *,
        tenant_id: uuid.UUID,
        agent_id: uuid.UUID,
        user_id: uuid.UUID | None,
        session_id: uuid.UUID | None,
        project: str | None,
    ) -> ListRetrievalResult | None:
        """Return the open-list title index, or ``None`` on a miss (no-op).

        Unconditional: whichever open-list pointers resolve against the list
        file are injected, without inspecting the Run's goal wording.
        """
        candidates: list[uuid.UUID] = []
        async with self._session_factory() as db:
            if session_id is not None:
                snapshot = await self._context_service.load_snapshot(
                    db,
                    tenant_id=tenant_id,
                    session_id=session_id,
                )
                for list_id in _collect_list_pointer_ids(snapshot.open_items, project=project):
                    if list_id not in candidates:
                        candidates.append(list_id)
            if user_id is not None:
                recent = await self._context_service.load_recent_sessions_open_items(
                    db,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    user_id=user_id,
                    exclude_session_id=session_id,
                    limit=self._max_sessions,
                )
                for _session_id, open_items in recent:
                    for list_id in _collect_list_pointer_ids(open_items, project=project):
                        if list_id not in candidates:
                            candidates.append(list_id)
        if not candidates:
            return None

        storage_key = normalize_storage_key(f"{agent_id}/{LIST_FILE_PATH}")
        content = await self._load_file(storage_key)
        if not content:
            return None
        sections_by_id = {section.list_id: section for section in parse_list_file(content).sections}

        resolved: list[RetrievedListSection] = []
        for list_id in candidates:
            section = sections_by_id.get(list_id)
            if section is None or not section.items:
                continue
            resolved.append(
                RetrievedListSection(
                    title=_bounded_pending_title(section.title),
                    items=tuple(section.items[:MAX_INJECTED_ITEMS]),
                    total_count=len(section.items),
                )
            )
        if not resolved:
            return None
        return ListRetrievalResult(
            sections=tuple(resolved[:_MAX_PENDING_LIST_NOTES]),
            total_lists=len(resolved),
        )


__all__ = [
    "MAX_INJECTED_ITEMS",
    "MAX_SESSIONS_DEFAULT",
    "CrossSessionListRetriever",
    "ListRetrievalResult",
    "RetrievedListSection",
    "render_retrieval_note",
]
