"""Deterministic session-level task-state persistence at Run boundaries.

票 04 of the Run-context-inheritance fix: when a Run settles — including the
``waiting_*`` non-terminal states that no terminal handler ever sees — the
session's task state is written deterministically to ``memory/任务状态.md``
(file-authoritative, one section per session, latest wins, foreign content
preserved verbatim) plus a pure index pointer row
``{"task_state_ref": "memory/任务状态.md"}`` in
``session_context_states.open_items`` (zero schema migration).

This closes the waiting gap: R1 (``list_persistence``) only persists at
``completed``, so a Run parked in ``waiting_user`` left no task state behind.
Phase is a deterministic projection (D-8), never an LLM judgment.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import logging
import re
import uuid

from app.services.agent_runtime.command_worker import (
    CheckpointObservation,
    RuntimeRunRecord,
    RuntimeSessionFactory,
)
from app.services.agent_runtime.list_persistence import (
    LIST_FILE_PATH,
    parse_list_file,
)
from app.services.agent_runtime.session_context_service import (
    SessionContextCandidate,
    SessionContextConflict,
    SessionContextService,
)
from app.services.agent_runtime.state import JsonObject, JsonValue
from app.services.storage import get_storage_backend, normalize_storage_key
from app.services.storage_runtime.base import StorageBackend

logger = logging.getLogger(__name__)

TASK_STATE_FILE_PATH = "memory/任务状态.md"
TASK_STATE_REF_KEY = "task_state_ref"

# dsh GoalPhase four-value enum, the only phases this projection produces.
PHASE_PAUSED = "paused"
PHASE_BLOCKED = "blocked"
PHASE_COMPLETE = "complete"
PHASE_ACTIVE = "active"

# D-11 model-visible contract: stable past-tense completion phrases keyed by
# phase. ``blocked`` refines further by the Run's terminal ``ended`` status
# (see ``completion_phrase_for``), while the phase enum itself stays the dsh
# four-value GoalPhase. All wording is past-tense and marked 非当前任务; a
# change here is a model-visible contract change (backend/AGENTS.md).
PHASE_COMPLETION_PHRASES: dict[str, str] = {
    PHASE_COMPLETE: "上一轮已完成",
    PHASE_ACTIVE: "上一轮任务已交付，仍有未决事项",
    PHASE_PAUSED: "上一轮任务暂停，等待你的回复",
    PHASE_BLOCKED: "上一轮任务未完成",
}
_BLOCKED_ENDED_PHRASES: dict[str, str] = {
    "failed": "上一轮任务未完成（未成功）",
    "cancelled": "上一轮任务未完成（已取消）",
}

_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_GOAL_MAX_CHARS = 100
_PENDING_FALLBACK_TITLE = "清单"
_MAX_PENDING_LIST_NOTES = 3
_MAX_PENDING_TITLE_CHARS = 30

_HEADER_LINE = re.compile(
    r"^## task:(?P<session>[0-9a-fA-F-]{36}) \| phase: (?P<phase>[^|]+?) \| "
    r"ended: (?P<ended>[^|]+?) \| 目标：(?P<goal>.*?) \| "
    r"run:(?P<run>[0-9a-fA-F-]{36}) \| (?P<ts>[^|]+?)\s*$"
)
_PENDING_LINE = re.compile(
    r"^未决事项：list:([0-9a-fA-F-]{36}) 清单「(.+?)」（(\d+) 项）\s*$"
)


class TaskStatePersistenceError(RuntimeError):
    """A session task state cannot be persisted without violating its own rules."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def map_phase(status: str, *, has_open_list_items: bool) -> str:
    """Deterministic phase projection (D-8); no LLM, no detection.

    ``waiting_*`` → paused (parked, awaiting a reply/approval);
    ``failed``/``cancelled`` → blocked (interrupted, not done);
    ``completed`` → active when the session's open_items still hold a
    ``memory/清单.md`` pointer (a delivered list with unexecuted entries),
    otherwise complete.
    """
    if status.startswith("waiting_"):
        return PHASE_PAUSED
    if status in ("failed", "cancelled"):
        return PHASE_BLOCKED
    if status == "completed":
        return PHASE_ACTIVE if has_open_list_items else PHASE_COMPLETE
    raise ValueError(f"unsupported task-state status: {status}")


def completion_phrase_for(phase: str, ended: str | None) -> str:
    """D-11 stable completion phrase for one persisted task state.

    ``blocked`` is the only phase whose wording refines by the Run's terminal
    ``ended`` status (failed vs cancelled read differently); any other or
    unknown phase falls back to the legacy complete phrase — the same default
    ``_prior_run_summary`` uses when no state exists at all.
    """
    if phase == PHASE_BLOCKED:
        return _BLOCKED_ENDED_PHRASES.get(ended or "", PHASE_COMPLETION_PHRASES[PHASE_BLOCKED])
    return PHASE_COMPLETION_PHRASES.get(phase, PHASE_COMPLETION_PHRASES[PHASE_COMPLETE])


def _sanitize_goal(goal: str) -> str:
    """Single-line, 100-char-bounded goal text for the section header."""
    return goal.replace("\r", " ").replace("\n", " ").strip()[:_GOAL_MAX_CHARS]


@dataclass(frozen=True, slots=True)
class PendingListRef:
    """One 未决事项 list pointer captured from open_items at the Run boundary."""

    list_id: uuid.UUID
    title: str
    item_count: int


@dataclass(frozen=True, slots=True)
class TaskSection:
    """One ``## task:...`` section of ``memory/任务状态.md``."""

    session_id: uuid.UUID
    phase: str
    ended: str
    goal: str
    run_id: uuid.UUID
    timestamp: str
    pending_lists: tuple[PendingListRef, ...]


@dataclass(frozen=True, slots=True)
class TaskFile:
    """Parsed ``memory/任务状态.md``; foreign content is preserved verbatim."""

    blocks: tuple[str | TaskSection, ...]

    @property
    def sections(self) -> tuple[TaskSection, ...]:
        return tuple(block for block in self.blocks if isinstance(block, TaskSection))


def _render_header(section: TaskSection) -> str:
    return (
        f"## task:{section.session_id} | phase: {section.phase} | ended: {section.ended} | "
        f"目标：{section.goal} | run:{section.run_id} | {section.timestamp}\n"
    )


def _render_section(section: TaskSection) -> str:
    lines = [_render_header(section)]
    for pending in section.pending_lists:
        lines.append(
            f"未决事项：list:{pending.list_id} 清单「{pending.title}」（{pending.item_count} 项）\n"
        )
    return "".join(lines)


def render_task_file(parsed: TaskFile) -> str:
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


def _parse_section_lines(lines: list[str]) -> TaskSection | None:
    header = _HEADER_LINE.match(lines[0])
    if header is None:
        return None
    try:
        session_id = uuid.UUID(header.group("session"))
        run_id = uuid.UUID(header.group("run"))
    except ValueError:
        return None
    pending_lists: list[PendingListRef] = []
    for line in lines[1:]:
        if not line.strip():
            continue
        match = _PENDING_LINE.match(line)
        if match is None:
            return None
        try:
            list_id = uuid.UUID(match.group(1))
        except ValueError:
            return None
        pending_lists.append(
            PendingListRef(
                list_id=list_id,
                title=match.group(2),
                item_count=int(match.group(3)),
            )
        )
    return TaskSection(
        session_id=session_id,
        phase=header.group("phase").strip(),
        ended=header.group("ended").strip(),
        goal=header.group("goal").strip(),
        run_id=run_id,
        timestamp=header.group("ts").strip(),
        pending_lists=tuple(pending_lists),
    )


def parse_task_file(content: str) -> TaskFile:
    """Split ``memory/任务状态.md`` into task sections and verbatim raw blocks.

    A ``## task:`` block that cannot be parsed completely (bad UUID, stray
    non-未决事项 line) is kept as raw text instead of being rewritten — foreign
    or hand-edited content is never dropped.
    """
    raw_lines: list[str] = []
    blocks: list[str | TaskSection] = []
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
        if line.startswith("## task:"):
            flush_section()
            section_lines = [line]
            continue
        if section_lines is not None:
            if not line.strip():
                section_lines.append(line)
                continue
            if _PENDING_LINE.match(line) is not None:
                section_lines.append(line)
                continue
            flush_section()
            raw_lines.append(line)
            continue
        raw_lines.append(line)
    flush_section()
    flush_raw()
    return TaskFile(blocks=tuple(blocks))


def _is_task_state_pointer(open_item: JsonValue) -> bool:
    return (
        isinstance(open_item, Mapping)
        and open_item.get(TASK_STATE_REF_KEY) == TASK_STATE_FILE_PATH
    )


class _SessionTaskStatePersistence:
    """Shared file + pointer persistence behind the two Runtime handler hooks."""

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

    @staticmethod
    def _is_direct_chat(run: RuntimeRunRecord) -> bool:
        return run.thread_id == run.session_id

    async def _load_file(self, key: str) -> str:
        storage = self._storage_backend()
        try:
            if not await storage.exists(key) or not await storage.is_file(key):
                return ""
            return await storage.read_text(key, encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            # Local agent-file storage failure or invalid key: start empty.
            logger.warning("Failed to read task-state file %s; starting empty", key, exc_info=True)
            return ""

    async def _write_file(self, key: str, content: str) -> None:
        await self._storage_backend().write_text(key, content, encoding="utf-8")

    async def _load_list_titles_and_counts(
        self,
        *,
        run: RuntimeRunRecord,
        list_ids: Sequence[uuid.UUID],
    ) -> dict[uuid.UUID, tuple[str, int]]:
        """Snapshot title + item count for each list pointer from ``清单.md``."""
        if run.agent_id is None:
            return {}
        wanted = set(list_ids)
        storage_key = normalize_storage_key(f"{run.agent_id}/{LIST_FILE_PATH}")
        content = await self._load_file(storage_key)
        parsed = parse_list_file(content)
        return {
            section.list_id: (section.title, len(section.items))
            for section in parsed.sections
            if section.list_id in wanted
        }

    async def _load_pending_lists(self, *, run: RuntimeRunRecord) -> tuple[PendingListRef, ...]:
        """Collect 未决事项 list pointers from open_items, resolved against 清单.md."""
        if run.session_id is None:
            return ()
        try:
            session_id = uuid.UUID(run.session_id)
        except ValueError:
            return ()
        async with self._session_factory() as db:
            snapshot = await self._context_service.load_snapshot(
                db,
                tenant_id=run.tenant_id,
                session_id=session_id,
            )
        list_ids: list[uuid.UUID] = []
        for open_item in snapshot.open_items:
            if not isinstance(open_item, Mapping):
                continue
            if open_item.get("list_ref") != LIST_FILE_PATH:
                continue
            raw_list_id = open_item.get("list_id")
            if not isinstance(raw_list_id, str):
                continue
            try:
                list_ids.append(uuid.UUID(raw_list_id))
            except ValueError:
                continue
        if not list_ids:
            return ()
        titles_and_counts = await self._load_list_titles_and_counts(run=run, list_ids=list_ids)
        return tuple(
            PendingListRef(
                list_id=list_id,
                title=titles_and_counts.get(list_id, (_PENDING_FALLBACK_TITLE, 0))[0],
                item_count=titles_and_counts.get(list_id, (_PENDING_FALLBACK_TITLE, 0))[1],
            )
            for list_id in list_ids
        )

    async def _replace_task_state_pointer(
        self,
        *,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
    ) -> None:
        pointer: JsonObject = {TASK_STATE_REF_KEY: TASK_STATE_FILE_PATH}
        for _attempt in range(self._max_conflict_retries):
            async with self._session_factory() as db:
                async with db.begin():
                    snapshot = await self._context_service.load_snapshot(
                        db,
                        tenant_id=tenant_id,
                        session_id=session_id,
                    )
                    open_items = [
                        value for value in snapshot.open_items if not _is_task_state_pointer(value)
                    ]
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
                            expected_covered_through_message_id=snapshot.covered_through_message_id,
                            candidate=candidate,
                        )
                        return
                    except SessionContextConflict:
                        continue
        raise TaskStatePersistenceError(
            "task_state_pointer_conflict_limit",
            "Session Context kept changing while the task-state pointer was replaced",
        )

    async def _write_state(
        self,
        *,
        run: RuntimeRunRecord,
        phase: str,
        ended: str,
        pending_lists: Sequence[PendingListRef],
    ) -> None:
        if run.agent_id is None:
            return
        if run.session_id is None:
            return
        try:
            session_id = uuid.UUID(run.session_id)
        except ValueError:
            return
        section = TaskSection(
            session_id=session_id,
            phase=phase,
            ended=ended,
            goal=_sanitize_goal(run.goal),
            run_id=run.run_id,
            timestamp=self._now().strftime("%Y-%m-%d %H:%M"),
            pending_lists=tuple(pending_lists),
        )
        storage_key = normalize_storage_key(f"{run.agent_id}/{TASK_STATE_FILE_PATH}")
        content = await self._load_file(storage_key)
        parsed_file = parse_task_file(content)
        existing: TaskSection | None = None
        for candidate in parsed_file.sections:
            if candidate.session_id == session_id:
                existing = candidate
                break
        if existing is None:
            blocks = [*parsed_file.blocks, section]
        else:
            # Same-session section replaced wholesale; a hand-copied duplicate
            # section with the same session_id is dropped, never duplicated.
            blocks = [
                section if block is existing else block
                for block in parsed_file.blocks
                if not (
                    isinstance(block, TaskSection)
                    and block is not existing
                    and block.session_id == session_id
                )
            ]
        await self._write_file(storage_key, render_task_file(TaskFile(blocks=tuple(blocks))))
        await self._replace_task_state_pointer(
            tenant_id=run.tenant_id,
            session_id=session_id,
        )


class SessionTaskStateWaitingHandler(_SessionTaskStatePersistence):
    """Persist ``phase=paused`` when a Run parks in a ``waiting_*`` state.

    Checkpoint-handler hook (``RuntimeCheckpointSideEffects.checkpoint_handlers``)
    fires on every committed checkpoint, so this closes the waiting gap that
    terminal handlers never observe. Best-effort idempotent: replay converges to
    the same section and a single pointer row.
    """

    async def handle(
        self,
        *,
        run: RuntimeRunRecord,
        checkpoint: CheckpointObservation,
    ) -> None:
        status = checkpoint.state["lifecycle"].get("status")
        if not isinstance(status, str) or not status.startswith("waiting_"):
            return
        if not self._is_direct_chat(run):
            return
        try:
            pending = await self._load_pending_lists(run=run)
            await self._write_state(run=run, phase=PHASE_PAUSED, ended=status, pending_lists=pending)
        except Exception:
            # D-10: best-effort persistence must never block Run settlement.
            logger.warning(
                "Session task-state waiting persistence failed for run %s; continuing",
                run.run_id,
                exc_info=True,
            )


class SessionTaskStateTerminalHandler(_SessionTaskStatePersistence):
    """Persist the mapped phase at the terminal Run boundary.

    Terminal-handler hook registered last so it runs after
    ``SessionContextCompletionHandler`` / ``ListPersistenceCompletionHandler``
    and reads the merged open_items to decide active/complete. Best-effort
    idempotent: replay converges to the same section and a single pointer row.
    """

    async def handle(
        self,
        *,
        run: RuntimeRunRecord,
        checkpoint: CheckpointObservation,
    ) -> None:
        status = checkpoint.state["lifecycle"].get("status")
        if status not in _TERMINAL_STATUSES:
            return
        if not self._is_direct_chat(run):
            return
        try:
            pending = await self._load_pending_lists(run=run)
            phase = map_phase(status, has_open_list_items=bool(pending))
            await self._write_state(run=run, phase=phase, ended=status, pending_lists=pending)
        except Exception:
            # D-10: best-effort persistence must never block Run settlement.
            logger.warning(
                "Session task-state terminal persistence failed for run %s; continuing",
                run.run_id,
                exc_info=True,
            )


def _bounded_pending_title(title: str) -> str:
    """Strip and bound one 清单 title for a model-visible pointer line.

    Blank titles fall back to ``清单``; titles longer than 30 characters are
    truncated with a trailing ``…`` so one runaway list title cannot blow up
    the model window.
    """
    cleaned = title.strip()
    if not cleaned:
        return _PENDING_FALLBACK_TITLE
    if len(cleaned) > _MAX_PENDING_TITLE_CHARS:
        return cleaned[:_MAX_PENDING_TITLE_CHARS] + "…"
    return cleaned


def render_pending_lists_line(section: TaskSection) -> str | None:
    """Render the bounded 未决事项 pointer line, or ``None`` when empty.

    At most three pending lists are noted (bounded so a long open-items tail
    cannot blow up the model window); each entry carries the list title
    (stripped, truncated to 30 characters, ``清单`` when blank), the item
    count, and the authoritative ``memory/清单.md`` path the model can read
    for the full text. A trailing ``等`` marks lists dropped past the bound.
    """
    if not section.pending_lists:
        return None
    notes = [
        f"清单「{_bounded_pending_title(pending.title)}」（{pending.item_count} 项，见 memory/清单.md）"
        for pending in section.pending_lists[:_MAX_PENDING_LIST_NOTES]
    ]
    line = "未决事项：" + "；".join(notes)
    if len(section.pending_lists) > _MAX_PENDING_LIST_NOTES:
        line += "等"
    return line


def render_task_state_note(section: TaskSection, *, current_run_id: str) -> JsonObject:
    """Render the bridge-inactive standalone task-state note (D-9).

    Same past-tense "非当前任务" frame as ``render_retrieval_note``: the prior
    task's phase wording, bounded pending-list pointer, and (when non-empty)
    goal are injected when the prior Run was compacted out of the window and
    its phase is not ``complete``. The id embeds the current Run id so the
    note is per-Run.
    """
    phrase = completion_phrase_for(section.phase, section.ended)
    content = f"历史上下文（非当前任务）：{phrase}。"
    if section.goal:
        content += f"任务「{section.goal}」。"
    pending_line = render_pending_lists_line(section)
    if pending_line:
        content += pending_line
    return {
        "id": f"session-task-state:{current_run_id}",
        "role": "user",
        "content": content,
        "runtime_input": "session_task_state",
    }


class SessionTaskStateLoader:
    """Load the prior Run's persisted task state for one Session at Run startup.

    R3-retriever-shaped best-effort read: resolve the session pointer row,
    read ``memory/任务状态.md``, parse, and return the section for the
    session. Any failure or absence degrades to ``None`` (log + fail-open) so
    a Run's startup is never blocked by a missing file or an unavailable
    store. The self-Run guard returns ``None`` when the section was written by
    this same Run (a resume/replay must not self-annotate).
    """

    def __init__(
        self,
        *,
        session_factory: RuntimeSessionFactory,
        context_service: SessionContextService | None = None,
        storage: StorageBackend | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._context_service = context_service or SessionContextService()
        self._storage = storage

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
                "Failed to read task-state file %s for session task-state load",
                key,
                exc_info=True,
            )
            return ""

    async def load(
        self,
        *,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        agent_id: uuid.UUID,
        current_run_id: str,
    ) -> TaskSection | None:
        try:
            async with self._session_factory() as db:
                snapshot = await self._context_service.load_snapshot(
                    db,
                    tenant_id=tenant_id,
                    session_id=session_id,
                )
        except Exception:
            logger.warning(
                "Session task-state pointer read failed for session %s; skipping",
                session_id,
                exc_info=True,
            )
            return None
        if not any(_is_task_state_pointer(item) for item in snapshot.open_items):
            return None
        storage_key = normalize_storage_key(f"{agent_id}/{TASK_STATE_FILE_PATH}")
        content = await self._load_file(storage_key)
        if not content:
            return None
        section = next(
            (
                candidate
                for candidate in parse_task_file(content).sections
                if candidate.session_id == session_id
            ),
            None,
        )
        if section is None:
            return None
        if str(section.run_id) == current_run_id:
            # Self-Run guard: the current Run's own freshly-written state must
            # not annotate its resume/replay.
            return None
        return section


__all__ = [
    "PHASE_ACTIVE",
    "PHASE_BLOCKED",
    "PHASE_COMPLETE",
    "PHASE_COMPLETION_PHRASES",
    "PHASE_PAUSED",
    "PendingListRef",
    "SessionTaskStateLoader",
    "SessionTaskStateTerminalHandler",
    "SessionTaskStateWaitingHandler",
    "TASK_STATE_FILE_PATH",
    "TASK_STATE_REF_KEY",
    "TaskFile",
    "TaskSection",
    "TaskStatePersistenceError",
    "completion_phrase_for",
    "map_phase",
    "parse_task_file",
    "render_pending_lists_line",
    "render_task_file",
    "render_task_state_note",
]
