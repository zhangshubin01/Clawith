"""Deterministic numbered-list persistence from terminal Run closing answers.

R1 of the Run-context-inheritance fix: when a Run completes, the numbered list
in its closing answer is extracted by pure-function rules (never an LLM), then
merged into the agent's ``memory/清单.md`` with continued numbering, and an
index pointer row is written into ``session_context_states.open_items``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import json
import logging
import re
import uuid

from app.services.agent_runtime.command_worker import (
    CheckpointObservation,
    RuntimeRunRecord,
    RuntimeSessionFactory,
)
from app.services.agent_runtime.session_context_service import (
    SessionContextCandidate,
    SessionContextConflict,
    SessionContextService,
)
from app.services.agent_runtime.state import (
    JsonObject,
    JsonValue,
    runtime_messages_as_json,
)
from app.services.storage import get_storage_backend, normalize_storage_key
from app.services.storage_runtime.base import StorageBackend

logger = logging.getLogger(__name__)

LIST_FILE_PATH = "memory/清单.md"

# R5 numbering contract, injected once per Run into the system prompt. Stable
# wording: changes here are a model-visible contract change (backend/AGENTS.md).
LIST_NUMBERING_CONTRACT = (
    "\n\n# Numbered Lists\n\n如产出编号清单，必须每行 `N. 标题 — 一句话说明`；同一清单重问时延续原编号、只增补不重排；用户以编号引用清单时，以上下文/历史检索注入的清单条目为准执行，不得自行重排或猜测候选。"
)

_ITEM_LINE = re.compile(r"^\s*(\d+)\s*[.、)）]\s*(.+?)\s*$")
_HEADING_LINE = re.compile(r"^\s*#+\s+(.+?)\s*$")
_BOLD_LINE = re.compile(r"^\s*\*\*(.+?)\*\*\s*$")
_HEADER_LINE = re.compile(r"^## list:([0-9a-fA-F-]{36}) \| project: (.+?) \| 标题：(.+?) \| (.+?)\s*$")
_WORKSPACE_SEGMENT = re.compile(r"^workspace/([^/]+)/")
_TITLE_MAX_CHARS = 40


class ListPersistenceError(RuntimeError):
    """A terminal list cannot be persisted without violating its own rules."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ListItem:
    """One numbered entry of a persisted list."""

    number: int
    title: str
    description: str


@dataclass(frozen=True, slots=True)
class ParsedList:
    """The numbered list extracted from one closing answer."""

    items: tuple[ListItem, ...]


@dataclass(frozen=True, slots=True)
class ListSection:
    """One ``## list:...`` section of ``memory/清单.md``."""

    list_id: uuid.UUID
    project: str | None
    title: str
    timestamp: str
    items: tuple[ListItem, ...]


@dataclass(frozen=True, slots=True)
class ListFile:
    """Parsed ``memory/清单.md``; foreign content is preserved verbatim."""

    blocks: tuple[str | ListSection, ...]

    @property
    def sections(self) -> tuple[ListSection, ...]:
        return tuple(block for block in self.blocks if isinstance(block, ListSection))


def _split_title_description(body: str) -> tuple[str, str] | None:
    if " — " not in body:
        return None
    title, _, description = body.partition(" — ")
    title = title.strip()
    description = description.strip()
    if not title or not description:
        return None
    return title, description


def parse_numbered_list(text: str) -> ParsedList | None:
    """Extract ``N. 标题 — 说明`` lines; prose and table rows are rejected.

    Table rows (leading ``|``) and numbered lines without the em-dash
    separator are ignored. A list needs at least two entries — a lone numbered
    line inside prose is not a list.
    """
    items: list[ListItem] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") or stripped.endswith("|"):
            continue
        match = _ITEM_LINE.match(line)
        if match is None:
            continue
        body = _split_title_description(match.group(2))
        if body is None:
            continue
        number = int(match.group(1))
        title, description = body
        items.append(ListItem(number=number, title=title, description=description))
    if len(items) < 2:
        return None
    return ParsedList(items=tuple(items))


def extract_list_title(text: str, *, fallback: str) -> str:
    """The nearest heading before the first item line, else the goal truncated."""
    fallback_title = fallback.strip()[:_TITLE_MAX_CHARS]
    if not fallback_title:
        fallback_title = "清单"
    pending: str | None = None
    for line in text.splitlines():
        heading = _HEADING_LINE.match(line)
        if heading is not None:
            candidate = heading.group(1).strip()
            if candidate:
                pending = candidate
                continue
        bold = _BOLD_LINE.match(line)
        if bold is not None:
            candidate = bold.group(1).strip()
            if candidate:
                pending = candidate
                continue
        item = _ITEM_LINE.match(line)
        if item is not None and _split_title_description(item.group(2)) is not None:
            return pending if pending is not None else fallback_title
    return fallback_title


def extract_workspace_project(messages: Sequence[JsonObject]) -> str | None:
    """Most frequent ``workspace/<name>`` first segment across tool-call paths.

    Only paths carrying the explicit ``workspace/`` prefix participate (D1);
    ties break by first occurrence, which keeps the result deterministic.
    Returns None when no such path exists — the caller degrades to a
    session-level merge key.
    """
    counts: Counter[str] = Counter()
    for message in messages:
        raw_calls = message.get("tool_calls")
        if not isinstance(raw_calls, list):
            continue
        for raw_call in raw_calls:
            if not isinstance(raw_call, Mapping):
                continue
            function = raw_call.get("function")
            if not isinstance(function, Mapping):
                continue
            raw_arguments = function.get("arguments")
            if isinstance(raw_arguments, str):
                try:
                    raw_arguments = json.loads(raw_arguments)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
            if not isinstance(raw_arguments, Mapping):
                continue
            path = raw_arguments.get("path")
            if not isinstance(path, str):
                continue
            normalized = path.replace("\\", "/").strip()
            match = _WORKSPACE_SEGMENT.match(normalized)
            if match is None:
                continue
            name = match.group(1).strip()
            if name:
                counts[name] += 1
    if not counts:
        return None
    return counts.most_common(1)[0][0]


def _item_text(item: ListItem) -> str:
    return f"{item.title} — {item.description}"


def merge_list_items(
    existing: Sequence[ListItem],
    incoming: Sequence[ListItem],
) -> tuple[ListItem, ...]:
    """Continue numbering, append only, never reorder (D1 versioned merge).

    Incoming numbers are ignored: entries already present by full text are
    dropped, so replaying the same terminal handler is idempotent.
    """
    merged = list(existing)
    seen = {_item_text(item) for item in merged}
    next_number = max((item.number for item in merged), default=0)
    for item in incoming:
        text = _item_text(item)
        if text in seen:
            continue
        seen.add(text)
        next_number += 1
        merged.append(ListItem(number=next_number, title=item.title, description=item.description))
    return tuple(merged)


def _render_header(section: ListSection) -> str:
    project = section.project if section.project else "-"
    return f"## list:{section.list_id} | project: {project} | 标题：{section.title} | {section.timestamp}\n"


def _render_section(section: ListSection) -> str:
    lines = [_render_header(section)]
    for item in section.items:
        lines.append(f"{item.number}. {_item_text(item)}\n")
    return "".join(lines)


def render_list_file(parsed: ListFile) -> str:
    """Serialize back; raw blocks round-trip byte-exact."""
    parts: list[str] = []
    for block in parsed.blocks:
        if isinstance(block, str):
            parts.append(block)
        else:
            if parts and not parts[-1].endswith("\n"):
                parts.append("\n")
            parts.append(_render_section(block))
    return "".join(parts)


def _parse_section_lines(lines: list[str]) -> ListSection | None:
    header = _HEADER_LINE.match(lines[0])
    if header is None:
        return None
    try:
        list_id = uuid.UUID(header.group(1))
    except ValueError:
        return None
    project_text = header.group(2).strip()
    items: list[ListItem] = []
    for line in lines[1:]:
        if not line.strip():
            continue
        match = _ITEM_LINE.match(line)
        if match is None:
            return None
        body = _split_title_description(match.group(2))
        if body is None:
            return None
        title, description = body
        items.append(ListItem(number=int(match.group(1)), title=title, description=description))
    return ListSection(
        list_id=list_id,
        project=None if project_text == "-" else project_text,
        title=header.group(3).strip(),
        timestamp=header.group(4).strip(),
        items=tuple(items),
    )


def parse_list_file(content: str) -> ListFile:
    """Split ``memory/清单.md`` into list sections and verbatim raw blocks.

    A ``## list:`` block that cannot be parsed completely (bad UUID, stray
    non-item line) is kept as raw text instead of being rewritten — foreign or
    hand-edited content is never dropped.
    """
    raw_lines: list[str] = []
    blocks: list[str | ListSection] = []
    section_lines: list[str] | None = None

    def flush_raw() -> None:
        if raw_lines:
            blocks.append("".join(raw_lines))
            raw_lines.clear()

    def flush_section() -> None:
        nonlocal section_lines
        if section_lines is None:
            return
        trailing: list[str] = []
        while section_lines and not section_lines[-1].strip():
            trailing.append(section_lines.pop())
        section = _parse_section_lines(section_lines)
        trailing.reverse()
        if section is None:
            raw_lines.extend([*section_lines, *trailing])
        else:
            flush_raw()
            blocks.append(section)
            raw_lines.extend(trailing)
        section_lines = None

    for line in content.splitlines(keepends=True):
        if _HEADER_LINE.match(line) is not None or (line.startswith("## list:") and section_lines is None):
            flush_section()
            section_lines = [line]
            continue
        if section_lines is not None:
            if not line.strip():
                section_lines.append(line)
                continue
            match = _ITEM_LINE.match(line)
            if match is not None and _split_title_description(match.group(2)) is not None:
                section_lines.append(line)
                continue
            flush_section()
            raw_lines.append(line)
            continue
        raw_lines.append(line)
    flush_section()
    flush_raw()
    return ListFile(blocks=tuple(blocks))


def _closing_content(checkpoint: CheckpointObservation) -> str | None:
    lifecycle = checkpoint.state["lifecycle"]
    answer = lifecycle.get("final_answer")
    if isinstance(answer, str) and answer.strip():
        return answer.strip()
    request = lifecycle.get("delivery_request")
    if isinstance(request, Mapping):
        content = request.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return None


def _is_pointer_for(open_item: JsonValue, list_id: uuid.UUID) -> bool:
    return (
        isinstance(open_item, Mapping)
        and open_item.get("list_ref") == LIST_FILE_PATH
        and open_item.get("list_id") == str(list_id)
    )


class ListPersistenceCompletionHandler:
    """Persist the closing answer's numbered list at the terminal Run boundary.

    Terminal-handler hook (RuntimeCheckpointSideEffects.terminal_handlers):
    covers the gap where open_items had no writer when a Run finishes without
    compaction. Best-effort idempotent by design — replay merges to the same
    file content and pointer row.
    """

    def __init__(
        self,
        *,
        session_factory: RuntimeSessionFactory,
        context_service: SessionContextService | None = None,
        storage: StorageBackend | None = None,
        max_conflict_retries: int = 3,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_conflict_retries <= 0:
            raise ValueError("max_conflict_retries must be positive")
        self._session_factory = session_factory
        self._context_service = context_service or SessionContextService()
        self._storage = storage
        self._max_conflict_retries = max_conflict_retries
        self._clock = clock or (lambda: datetime.now(UTC))

    def _storage_backend(self) -> StorageBackend:
        return self._storage or get_storage_backend()

    def _now(self) -> datetime:
        return self._clock()

    async def _load_file(self, key: str) -> str:
        storage = self._storage_backend()
        try:
            if not await storage.exists(key) or not await storage.is_file(key):
                return ""
            return await storage.read_text(key, encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            # Local agent-file storage failure or invalid key: start empty.
            logger.warning("Failed to read list file %s; starting empty", key, exc_info=True)
            return ""

    async def _write_file(self, key: str, content: str) -> None:
        await self._storage_backend().write_text(key, content, encoding="utf-8")

    async def _load_session_pointer_list_id(
        self,
        *,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
    ) -> uuid.UUID | None:
        async with self._session_factory() as db:
            snapshot = await self._context_service.load_snapshot(
                db,
                tenant_id=tenant_id,
                session_id=session_id,
            )
        for open_item in snapshot.open_items:
            if not isinstance(open_item, Mapping):
                continue
            if open_item.get("list_ref") != LIST_FILE_PATH:
                continue
            raw_list_id = open_item.get("list_id")
            if not isinstance(raw_list_id, str):
                continue
            try:
                return uuid.UUID(raw_list_id)
            except ValueError:
                continue
        return None

    async def _replace_pointer(
        self,
        *,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        pointer: JsonObject,
    ) -> None:
        raw_list_id = pointer["list_id"]
        if not isinstance(raw_list_id, str):
            raise ListPersistenceError(
                "invalid_list_pointer",
                "list pointer list_id must be a string",
            )
        list_id = uuid.UUID(raw_list_id)
        for _attempt in range(self._max_conflict_retries):
            async with self._session_factory() as db:
                async with db.begin():
                    snapshot = await self._context_service.load_snapshot(
                        db,
                        tenant_id=tenant_id,
                        session_id=session_id,
                    )
                    open_items = [value for value in snapshot.open_items if not _is_pointer_for(value, list_id)]
                    open_items.append(pointer)
                    candidate = SessionContextCandidate(
                        summary=snapshot.summary,
                        requirements=snapshot.requirements,
                        decisions=snapshot.decisions,
                        open_items=tuple(open_items),
                        evidence_refs=snapshot.evidence_refs,
                        workspace_refs=snapshot.workspace_refs,
                        covered_through_message_id=snapshot.covered_through_message_id,
                    )
                    try:
                        await self._context_service.compare_and_swap(
                            db,
                            tenant_id=tenant_id,
                            session_id=session_id,
                            expected_version=snapshot.version,
                            expected_covered_through_message_id=(snapshot.covered_through_message_id),
                            candidate=candidate,
                        )
                        return
                    except SessionContextConflict:
                        continue
        raise ListPersistenceError(
            "list_pointer_conflict_limit",
            "Session Context kept changing while the list pointer was replaced",
        )

    async def handle(
        self,
        *,
        run: RuntimeRunRecord,
        checkpoint: CheckpointObservation,
    ) -> None:
        if checkpoint.state["lifecycle"].get("status") != "completed":
            return
        if run.agent_id is None:
            return
        closing = _closing_content(checkpoint)
        if closing is None:
            return
        parsed = parse_numbered_list(closing)
        if parsed is None:
            return
        try:
            messages = runtime_messages_as_json(checkpoint.state)
        except (TypeError, ValueError):
            messages = ()
        project = extract_workspace_project(messages)
        title = extract_list_title(closing, fallback=run.goal)

        storage_key = normalize_storage_key(f"{run.agent_id}/{LIST_FILE_PATH}")
        content = await self._load_file(storage_key)
        parsed_file = parse_list_file(content)

        section: ListSection | None = None
        if project is not None:
            # D1 merge key: (agent, workspace project, list kind). The only kind
            # this channel writes is the numbered list, so the project alone
            # identifies the list; the title is a display field, not identity.
            for candidate in parsed_file.sections:
                if candidate.project == project:
                    section = candidate
        elif run.session_id is not None:
            try:
                session_id = uuid.UUID(run.session_id)
            except ValueError:
                session_id = None
            if session_id is not None:
                pointer_list_id = await self._load_session_pointer_list_id(
                    tenant_id=run.tenant_id,
                    session_id=session_id,
                )
                if pointer_list_id is not None:
                    for candidate in parsed_file.sections:
                        if candidate.list_id == pointer_list_id:
                            section = candidate

        existing_items = section.items if section is not None else ()
        merged_items = merge_list_items(existing_items, parsed.items)
        if section is not None and merged_items == section.items:
            return
        if section is None:
            section = ListSection(
                list_id=uuid.uuid4(),
                project=project,
                title=title,
                timestamp=self._now().strftime("%Y-%m-%d %H:%M"),
                items=merged_items,
            )
            blocks = [*parsed_file.blocks, section]
        else:
            updated = replace(
                section,
                title=title,
                timestamp=self._now().strftime("%Y-%m-%d %H:%M"),
                items=merged_items,
            )
            # One list keeps only its latest version (R1): a hand-copied older
            # section with the same list_id is dropped, never renumbered.
            blocks = [
                updated if block is section else block
                for block in parsed_file.blocks
                if not (isinstance(block, ListSection) and block is not section and block.list_id == section.list_id)
            ]
        await self._write_file(storage_key, render_list_file(ListFile(blocks=tuple(blocks))))

        if run.session_id is None:
            return
        try:
            session_id = uuid.UUID(run.session_id)
        except ValueError:
            return
        await self._replace_pointer(
            tenant_id=run.tenant_id,
            session_id=session_id,
            pointer={
                "list_ref": LIST_FILE_PATH,
                "list_id": str(section.list_id),
                "project": project,
            },
        )


__all__ = [
    "LIST_FILE_PATH",
    "LIST_NUMBERING_CONTRACT",
    "ListItem",
    "ListFile",
    "ListPersistenceCompletionHandler",
    "ListPersistenceError",
    "ListSection",
    "ParsedList",
    "extract_list_title",
    "extract_workspace_project",
    "merge_list_items",
    "parse_list_file",
    "parse_numbered_list",
    "render_list_file",
]
