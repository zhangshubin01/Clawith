"""Cross-session automatic numbered-list retrieval (R3 of Run-context inheritance).

When a Run's opening message references a prior numbered list — bare numbers
after an action verb (``做 1、2、3、5``), or a prior-session list by pronoun
(``上一轮的清单``) — the Runtime retrieves the matching ``memory/清单.md``
entries through the ``session_context_states.open_items`` pointer rows and
injects them into the model window as a past-tense "非当前任务" note.

A hit injects; a miss is a strict no-op (no injection, no waiting decision).
This mirrors the existing ``_prior_run_summary`` past-tense framing so the
note is never mistaken for a new directive.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import logging
import re
import uuid

from app.services.agent_runtime.command_worker import RuntimeSessionFactory
from app.services.agent_runtime.list_persistence import (
    LIST_FILE_PATH,
    ListItem,
    parse_list_file,
)
from app.services.agent_runtime.session_context_service import SessionContextService
from app.services.agent_runtime.state import JsonObject, JsonValue
from app.services.storage import get_storage_backend, normalize_storage_key
from app.services.storage_runtime.base import StorageBackend

logger = logging.getLogger(__name__)

# Scope constants: how many past sessions to project over, and how many items
# may be injected into one window (bounded so a long list cannot blow up the
# model budget).
MAX_SESSIONS_DEFAULT = 5
MAX_INJECTED_ITEMS = 20

# Detection: a number reference after an action verb. Numbers may carry a
# letter/symbol prefix (P2 / N3 / #4), matching how Focus labels and common
# shorthand cite prior lists. Full-width digits are normalized to half-width.
# The verb set is intentionally loose: a stray match (e.g. 执行 2 个测试) only
# reaches the retrieval layer, which no-ops when no list carries those
# numbers (the D5-2 false-positive guard).
_NUMBER_REFERENCE = re.compile(
    r"(?:做|完成|实现|改|执行|处理)\s*"
    r"(?P<numbers>(?:[PpNn][-—]?|[#＃])?\s*[0-9０-９]+"
    r"(?:[、，,\s]+(?:[PpNn][-—]?|[#＃])?\s*[0-9０-９]+)*)"
)
_FULLWIDTH_TO_HALFWIDTH = str.maketrans(
    {chr(0xFF10 + i): str(i) for i in range(10)}
)
# Word sets per plan R3-1: pronouns「上一轮/上次/之前/刚才」(上一轮/上一次/上回
# kept as synonyms) and nouns「清单/结论/优化/方案」(列表/编号 kept as synonyms).
_HISTORICAL_PRONOUNS = ("上一轮", "上一次", "上次", "上回", "之前", "刚才")
_LIST_NOUNS = ("清单", "列表", "优化", "结论", "方案", "编号")


@dataclass(frozen=True, slots=True)
class ListReferenceSignal:
    """A detected reference to a prior numbered list.

    ``numbers`` is non-empty for a bare-number reference (``做 1、2、3、5``)
    and empty for a pronoun-only historical reference (``上一轮的清单``).
    """

    numbers: tuple[int, ...]
    historical: bool


@dataclass(frozen=True, slots=True)
class ListRetrievalResult:
    """The list entries selected for injection into the model window."""

    title: str
    items: tuple[ListItem, ...]


def _normalize_digits(raw: str) -> tuple[int, ...]:
    numbers: list[int] = []
    for chunk in re.findall(r"[0-9０-９]+", raw):
        numbers.append(int(chunk.translate(_FULLWIDTH_TO_HALFWIDTH)))
    return tuple(numbers)


def detect_list_reference(message: str) -> ListReferenceSignal | None:
    """Detect a numbered or pronoun list reference in one user message.

    Pure function: no I/O, no state. A number reference (``做 1、2、3、5``,
    ``执行P2``, ``处理N3``, ``完成#4``) wins over the pronoun rule, so
    ``完成上一轮清单的 3、4、5`` extracts no numbers and falls through to the
    historical branch. A message with no verb-and-number sequence and no
    pronoun+noun pair returns ``None``.
    """
    if not isinstance(message, str):
        return None
    text = message.strip()
    if not text:
        return None
    match = _NUMBER_REFERENCE.search(text)
    if match is not None:
        numbers = _normalize_digits(match.group("numbers"))
        if numbers:
            return ListReferenceSignal(numbers=numbers, historical=False)
    historical = any(pronoun in text for pronoun in _HISTORICAL_PRONOUNS) and any(
        noun in text for noun in _LIST_NOUNS
    )
    if historical:
        return ListReferenceSignal(numbers=(), historical=True)
    return None


def render_retrieval_note(
    result: ListRetrievalResult,
    *,
    current_run_id: str,
) -> JsonObject:
    """Render the retrieved entries as a past-tense, non-imperative user note.

    Wording mirrors ``_prior_run_summary``: past tense (``曾产出``) and marked
    "非当前任务" so the model never reads it as a new directive.
    """
    body = "\n".join(
        f"{item.number}. {item.title} — {item.description}" for item in result.items
    )
    content = f"历史上下文（非当前任务）：此前会话曾产出清单「{result.title}」，条目：\n{body}"
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
    """Retrieve referenced list entries across the current and recent sessions.

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
        signal: ListReferenceSignal,
    ) -> ListRetrievalResult | None:
        """Return the referenced entries, or ``None`` on a miss (no-op)."""
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
        parsed = parse_list_file(content)
        sections_by_id = {section.list_id: section for section in parsed.sections}

        numbers = signal.numbers
        for list_id in candidates:
            section = sections_by_id.get(list_id)
            if section is None:
                continue
            if numbers:
                selected = [item for item in section.items if item.number in numbers]
            else:
                selected = list(section.items)
            if selected:
                return ListRetrievalResult(
                    title=section.title,
                    items=tuple(selected[:MAX_INJECTED_ITEMS]),
                )
        return None


__all__ = [
    "MAX_INJECTED_ITEMS",
    "MAX_SESSIONS_DEFAULT",
    "CrossSessionListRetriever",
    "ListReferenceSignal",
    "ListRetrievalResult",
    "detect_list_reference",
    "render_retrieval_note",
]
