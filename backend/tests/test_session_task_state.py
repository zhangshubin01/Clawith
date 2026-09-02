"""Session task-state persistence (票 04) pure-function and handler tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import uuid

import pytest

from app.services.agent_runtime.command_worker import (
    CheckpointObservation,
    RuntimeRunRecord,
)
from app.services.agent_runtime.list_persistence import LIST_FILE_PATH
from app.services.agent_runtime.session_context_service import (
    SessionContextCandidate,
    SessionContextConflict,
    SessionContextSnapshot,
)
from app.services.agent_runtime.session_task_state import (
    PHASE_ACTIVE,
    PHASE_BLOCKED,
    PHASE_COMPLETE,
    PHASE_PAUSED,
    TASK_STATE_FILE_PATH,
    PendingListRef,
    SessionTaskStateLoader,
    SessionTaskStateTerminalHandler,
    SessionTaskStateWaitingHandler,
    TaskSection,
    completion_phrase_for,
    map_phase,
    parse_task_file,
    render_pending_lists_line,
    render_task_file,
    render_task_state_note,
)
from app.services.agent_runtime.state import (
    RunInputSnapshots,
    RunRegistrySnapshot,
    RuntimeGraphState,
)


# ---------------------------------------------------------------- phase mapping


def test_map_phase_waiting_statuses_pause() -> None:
    assert map_phase("waiting_user", has_open_list_items=False) == PHASE_PAUSED
    assert map_phase("waiting_external", has_open_list_items=True) == PHASE_PAUSED
    assert map_phase("waiting_agent", has_open_list_items=False) == PHASE_PAUSED


def test_map_phase_failed_and_cancelled_block() -> None:
    assert map_phase("failed", has_open_list_items=False) == PHASE_BLOCKED
    assert map_phase("failed", has_open_list_items=True) == PHASE_BLOCKED
    assert map_phase("cancelled", has_open_list_items=False) == PHASE_BLOCKED


def test_map_phase_completed_without_list_is_complete() -> None:
    assert map_phase("completed", has_open_list_items=False) == PHASE_COMPLETE


def test_map_phase_completed_with_list_is_active() -> None:
    assert map_phase("completed", has_open_list_items=True) == PHASE_ACTIVE


def test_map_phase_rejects_unknown_status() -> None:
    with pytest.raises(ValueError, match="running"):
        map_phase("running", has_open_list_items=False)


# ---------------------------------------------------------------- file parse/render


def _section(
    *,
    session_id: str,
    phase: str = PHASE_ACTIVE,
    ended: str = "completed",
    goal: str = "优化 app",
    run_id: str | None = None,
    timestamp: str = "2026-09-01 20:00",
    pending: tuple[PendingListRef, ...] = (),
) -> TaskSection:
    return TaskSection(
        session_id=uuid.UUID(session_id),
        phase=phase,
        ended=ended,
        goal=goal,
        run_id=uuid.UUID(run_id) if run_id else uuid.uuid4(),
        timestamp=timestamp,
        pending_lists=pending,
    )


def test_task_file_roundtrip_preserves_foreign_content() -> None:
    list_id = "00000000-0000-0000-0000-000000000001"
    section = _section(
        session_id="00000000-0000-0000-0000-0000000000aa",
        run_id="00000000-0000-0000-0000-0000000000bb",
        pending=(
            PendingListRef(list_id=uuid.UUID(list_id), title="app 优化清单", item_count=2),
        ),
    )
    content = (
        "# 手写备忘\n"
        "这是 agent 自己写的内容。\n\n"
        f"## task:{section.session_id} | phase: active | ended: completed | "
        f"目标：优化 app | run:{section.run_id} | 2026-09-01 20:00\n"
        f"未决事项：list:{list_id} 清单「app 优化清单」（2 项）\n\n"
        "尾部注释\n"
    )
    parsed = parse_task_file(content)
    assert len(parsed.sections) == 1
    got = parsed.sections[0]
    assert got.session_id == section.session_id
    assert got.phase == PHASE_ACTIVE
    assert got.ended == "completed"
    assert got.goal == "优化 app"
    assert got.run_id == section.run_id
    assert got.timestamp == "2026-09-01 20:00"
    assert len(got.pending_lists) == 1
    assert got.pending_lists[0].list_id == uuid.UUID(list_id)
    assert got.pending_lists[0].title == "app 优化清单"
    assert got.pending_lists[0].item_count == 2
    assert render_task_file(parsed) == content


def test_task_file_treats_broken_section_headers_as_raw() -> None:
    content = "## task:not-a-uuid | phase: active | whatever\n未决事项：list:xyz 清单「x」（1 项）\n"
    parsed = parse_task_file(content)
    assert parsed.sections == ()
    assert render_task_file(parsed) == content


def test_task_file_stray_body_line_is_preserved_verbatim() -> None:
    # A stray non-未决事项 line is preserved as raw content (never dropped),
    # while the parseable header still yields a section with no pending lists.
    content = (
        "## task:00000000-0000-0000-0000-0000000000aa | phase: active | ended: completed | "
        "目标：优化 app | run:00000000-0000-0000-0000-0000000000bb | 2026-09-01 20:00\n"
        "这是一行不属于未决事项的内容\n"
    )
    parsed = parse_task_file(content)
    assert len(parsed.sections) == 1
    assert parsed.sections[0].pending_lists == ()
    assert render_task_file(parsed) == content


def test_task_file_multiple_sessions_render_roundtrip() -> None:
    a = _section(session_id="00000000-0000-0000-0000-0000000000a1", run_id="00000000-0000-0000-0000-0000000000b1")
    b = _section(
        session_id="00000000-0000-0000-0000-0000000000a2",
        run_id="00000000-0000-0000-0000-0000000000b2",
        phase=PHASE_BLOCKED,
        ended="failed",
    )
    content = (
        f"## task:{a.session_id} | phase: active | ended: completed | 目标：优化 app | run:{a.run_id} | 2026-09-01 20:00\n"
        f"## task:{b.session_id} | phase: blocked | ended: failed | 目标：优化 app | run:{b.run_id} | 2026-09-01 21:00\n"
    )
    parsed = parse_task_file(content)
    assert len(parsed.sections) == 2
    assert render_task_file(parsed) == content


# ---------------------------------------------------------------- handler fakes


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Session:
    def __init__(self) -> None:
        self.flushes = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def begin(self) -> _Transaction:
        return _Transaction()

    async def flush(self) -> None:
        self.flushes += 1


class _SessionFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> _Session:
        self.calls += 1
        return _Session()


class _Storage:
    """Key-aware fake: 清单.md and 任务状态.md live under separate keys."""

    def __init__(
        self,
        list_content: str | None = None,
        task_content: str | None = None,
    ) -> None:
        self.list_content = list_content
        self.task_content = task_content
        self.writes: list[tuple[str, str]] = []

    def _read(self, key: str) -> str | None:
        if key.endswith(LIST_FILE_PATH):
            return self.list_content
        if key.endswith(TASK_STATE_FILE_PATH):
            return self.task_content
        raise AssertionError(f"unexpected storage key: {key}")

    async def exists(self, key: str) -> bool:
        return self._read(key) is not None

    async def is_file(self, key: str) -> bool:
        return self._read(key) is not None

    async def read_text(self, key: str, encoding: str = "utf-8", errors: str = "replace") -> str:
        del encoding, errors
        content = self._read(key)
        assert content is not None
        return content

    async def write_text(self, key: str, content: str, encoding: str = "utf-8") -> None:
        del encoding
        assert key.endswith(TASK_STATE_FILE_PATH), f"unexpected write key: {key}"
        self.writes.append((key, content))
        self.task_content = content


class _ContextService:
    def __init__(
        self,
        snapshot: SessionContextSnapshot,
        *,
        conflicts: int = 0,
    ) -> None:
        self.snapshot = snapshot
        self.load_count = 0
        self.compare_calls: list[tuple[int, SessionContextCandidate]] = []
        self._conflicts = conflicts

    async def load_snapshot(self, db, *, tenant_id, session_id):
        del db, tenant_id, session_id
        self.load_count += 1
        return self.snapshot

    async def compare_and_swap(
        self,
        db,
        *,
        tenant_id,
        session_id,
        expected_version,
        expected_covered_through_message_id,
        candidate,
    ):
        del db, tenant_id, session_id, expected_covered_through_message_id
        if self._conflicts > 0:
            self._conflicts -= 1
            raise SessionContextConflict()
        self.compare_calls.append((expected_version, candidate))
        return _snapshot(
            version=expected_version + 1,
            summary=candidate.summary,
            requirements=tuple(candidate.requirements),
            decisions=tuple(candidate.decisions),
            open_items=tuple(candidate.open_items),
            evidence_refs=tuple(candidate.evidence_refs),
            workspace_refs=tuple(candidate.workspace_refs),
            covered_through_message_id=candidate.covered_through_message_id,
        )


def _snapshot(
    *,
    version: int = 1,
    summary: str = "old",
    open_items: tuple = (),
    covered_through_message_id: uuid.UUID | None = None,
    requirements: tuple = (),
    decisions: tuple = (),
    evidence_refs: tuple = (),
    workspace_refs: tuple = (),
) -> SessionContextSnapshot:
    return SessionContextSnapshot(
        version=version,
        summary=summary,
        requirements=requirements,
        decisions=decisions,
        open_items=open_items,
        evidence_refs=evidence_refs,
        workspace_refs=workspace_refs,
        covered_through_message_id=covered_through_message_id,
    )


def _state(*, status: str) -> RuntimeGraphState:
    registry = RunRegistrySnapshot(
        tenant_id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
        goal="优化 app",
        run_kind="chat",
        source_type="chat",
        model_id=str(uuid.uuid4()),
        graph_name="runtime_graph",
        graph_version="v1",
        agent_id="agent",
    )
    lifecycle: dict = {"status": status, "next_route": "terminal"}
    if status in ("failed", "cancelled"):
        lifecycle["error"] = {"code": "runtime_failed"}
    return {
        "registry": registry,
        "snapshots": RunInputSnapshots(
            session_context={},
            session_context_version=0,
            recent_session_messages=(),
            related_run_summaries=(),
            initial_input={"input_content": "优化 app"},
        ),
        "messages": [],
        "lifecycle": lifecycle,
    }  # type: ignore[typeddict-item]


def _records(
    *,
    status: str = "completed",
    goal: str = "优化 app",
    session_id: str | None = None,
    direct_chat: bool = True,
) -> tuple[RuntimeRunRecord, CheckpointObservation]:
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    run_id = uuid.uuid4()
    if session_id is None:
        session_id = str(uuid.uuid4())
    run = RuntimeRunRecord(
        tenant_id=tenant_id,
        run_id=run_id,
        thread_id=session_id if direct_chat else str(run_id),
        runtime_type="langgraph",
        goal=goal,
        run_kind="chat",
        source_type="chat",
        model_id=str(uuid.uuid4()),
        graph_name="runtime_graph",
        graph_version="v1",
        agent_id=str(agent_id),
        session_id=session_id,
    )
    checkpoint = CheckpointObservation(
        checkpoint_id="checkpoint-terminal",
        state=_state(status=status),
    )
    return run, checkpoint


def _waiting_handler(
    *,
    storage: _Storage,
    snapshot: SessionContextSnapshot,
    conflicts: int = 0,
    clock=None,
) -> tuple[SessionTaskStateWaitingHandler, _ContextService]:
    service = _ContextService(snapshot, conflicts=conflicts)
    handler = SessionTaskStateWaitingHandler(
        session_factory=_SessionFactory(),  # type: ignore[arg-type]
        context_service=service,  # type: ignore[arg-type]
        storage=storage,  # type: ignore[arg-type]
        clock=clock,
    )
    return handler, service


def _terminal_handler(
    *,
    storage: _Storage,
    snapshot: SessionContextSnapshot,
    conflicts: int = 0,
    clock=None,
) -> tuple[SessionTaskStateTerminalHandler, _ContextService]:
    service = _ContextService(snapshot, conflicts=conflicts)
    handler = SessionTaskStateTerminalHandler(
        session_factory=_SessionFactory(),  # type: ignore[arg-type]
        context_service=service,  # type: ignore[arg-type]
        storage=storage,  # type: ignore[arg-type]
        clock=clock,
    )
    return handler, service


def _list_pointer(list_id: uuid.UUID, *, project: str | None = None) -> dict:
    return {"list_ref": LIST_FILE_PATH, "list_id": str(list_id), "project": project}


# ---------------------------------------------------------------- waiting handler


@pytest.mark.asyncio
async def test_waiting_handler_writes_paused_section_and_pointer() -> None:
    storage = _Storage()
    run, checkpoint = _records(status="waiting_user")
    handler, service = _waiting_handler(storage=storage, snapshot=_snapshot())

    await handler.handle(run=run, checkpoint=checkpoint)

    assert storage.writes, "expected a file write"
    key, content = storage.writes[0]
    assert key.endswith(TASK_STATE_FILE_PATH)
    assert content.startswith("## task:")
    assert f"task:{run.session_id} | phase: paused | ended: waiting_user |" in content
    assert f"run:{run.run_id} |" in content
    assert "未决事项：" not in content  # no open list pointers
    assert len(service.compare_calls) == 1
    _, candidate = service.compare_calls[0]
    pointer = [item for item in candidate.open_items if isinstance(item, dict) and "task_state_ref" in item]
    assert pointer == [{"task_state_ref": TASK_STATE_FILE_PATH}]


@pytest.mark.asyncio
async def test_waiting_handler_noop_for_non_waiting_statuses() -> None:
    for status in ("completed", "failed", "cancelled", "running"):
        storage = _Storage()
        run, checkpoint = _records(status=status)
        handler, service = _waiting_handler(storage=storage, snapshot=_snapshot())

        await handler.handle(run=run, checkpoint=checkpoint)

        assert storage.writes == []
        assert service.compare_calls == []


@pytest.mark.asyncio
async def test_waiting_handler_noop_when_not_direct_chat() -> None:
    storage = _Storage()
    run, checkpoint = _records(status="waiting_user", direct_chat=False)
    handler, service = _waiting_handler(storage=storage, snapshot=_snapshot())

    await handler.handle(run=run, checkpoint=checkpoint)

    assert storage.writes == []
    assert service.compare_calls == []


@pytest.mark.asyncio
async def test_waiting_handler_replay_is_idempotent() -> None:
    storage = _Storage()
    run, checkpoint = _records(status="waiting_external")
    fixed_clock = datetime(2026, 9, 1, 20, 0, tzinfo=UTC)
    handler, service = _waiting_handler(
        storage=storage,
        snapshot=_snapshot(),
        clock=lambda: fixed_clock,
    )

    await handler.handle(run=run, checkpoint=checkpoint)
    first = storage.task_content
    await handler.handle(run=run, checkpoint=checkpoint)

    assert storage.task_content == first  # byte-identical replay
    assert first.count(f"task:{run.session_id}") == 1  # one section, no duplicate
    assert len(service.compare_calls) == 2
    for _, candidate in service.compare_calls:
        pointers = [item for item in candidate.open_items if isinstance(item, dict) and "task_state_ref" in item]
        assert len(pointers) == 1  # pointer deduplicated on every write


# ---------------------------------------------------------------- terminal handler


@pytest.mark.asyncio
async def test_terminal_handler_completed_without_list_is_complete() -> None:
    storage = _Storage()
    run, checkpoint = _records(status="completed")
    handler, service = _terminal_handler(storage=storage, snapshot=_snapshot())

    await handler.handle(run=run, checkpoint=checkpoint)

    key, content = storage.writes[0]
    assert key.endswith(TASK_STATE_FILE_PATH)
    assert "phase: complete | ended: completed |" in content
    assert "未决事项：" not in content
    assert len(service.compare_calls) == 1


@pytest.mark.asyncio
async def test_terminal_handler_completed_with_list_is_active() -> None:
    list_id = uuid.uuid4()
    list_content = (
        f"## list:{list_id} | project: mydome1 | 标题：app 优化清单 | 2026-09-01 18:00\n"
        "1. 输入精度截断 — Calculator.kt:204\n"
        "2. 超大指数上限 — power()\n"
    )
    storage = _Storage(list_content=list_content)
    run, checkpoint = _records(status="completed")
    handler, service = _terminal_handler(
        storage=storage,
        snapshot=_snapshot(open_items=(_list_pointer(list_id, project="mydome1"),)),
    )

    await handler.handle(run=run, checkpoint=checkpoint)

    key, content = storage.writes[0]  # only the task-state file is written
    assert key.endswith(TASK_STATE_FILE_PATH)
    assert "phase: active | ended: completed |" in content
    assert f"未决事项：list:{list_id} 清单「app 优化清单」（2 项）" in content
    assert len(service.compare_calls) == 1


@pytest.mark.asyncio
async def test_terminal_handler_failed_is_blocked() -> None:
    storage = _Storage()
    run, checkpoint = _records(status="failed")
    handler, service = _terminal_handler(storage=storage, snapshot=_snapshot())

    await handler.handle(run=run, checkpoint=checkpoint)

    key, content = storage.writes[0]
    assert key.endswith(TASK_STATE_FILE_PATH)
    assert "phase: blocked | ended: failed |" in content
    assert len(service.compare_calls) == 1


@pytest.mark.asyncio
async def test_terminal_handler_cancelled_is_blocked() -> None:
    storage = _Storage()
    run, checkpoint = _records(status="cancelled")
    handler, service = _terminal_handler(storage=storage, snapshot=_snapshot())

    await handler.handle(run=run, checkpoint=checkpoint)

    key, content = storage.writes[0]
    assert key.endswith(TASK_STATE_FILE_PATH)
    assert "phase: blocked | ended: cancelled |" in content
    assert len(service.compare_calls) == 1


@pytest.mark.asyncio
async def test_terminal_handler_noop_for_non_terminal_statuses() -> None:
    for status in ("running", "waiting_user", "waiting_external"):
        storage = _Storage()
        run, checkpoint = _records(status=status)
        handler, service = _terminal_handler(storage=storage, snapshot=_snapshot())

        await handler.handle(run=run, checkpoint=checkpoint)

        assert storage.writes == []
        assert service.compare_calls == []


@pytest.mark.asyncio
async def test_terminal_handler_noop_when_not_direct_chat() -> None:
    storage = _Storage()
    run, checkpoint = _records(status="completed", direct_chat=False)
    handler, service = _terminal_handler(storage=storage, snapshot=_snapshot())

    await handler.handle(run=run, checkpoint=checkpoint)

    assert storage.writes == []
    assert service.compare_calls == []


@pytest.mark.asyncio
async def test_terminal_handler_replaces_same_session_section_and_preserves_foreign() -> None:
    session_id = uuid.uuid4()
    run_id = uuid.uuid4()
    existing = (
        "# 备忘\n"
        f"## task:{session_id} | phase: active | ended: completed | 目标：优化 app | run:{run_id} | 2026-09-01 10:00\n"
        "未决事项：list:00000000-0000-0000-0000-000000000001 清单「旧清单」（1 项）\n\n"
        "尾部注释\n"
    )
    storage = _Storage(task_content=existing)
    run, checkpoint = _records(status="failed", session_id=str(session_id))
    run = replace(run, run_id=run_id)
    handler, _ = _terminal_handler(storage=storage, snapshot=_snapshot())

    await handler.handle(run=run, checkpoint=checkpoint)

    content = storage.writes[0][1]
    assert content.count("## task:") == 1  # same session replaced, no duplicate
    assert "phase: blocked | ended: failed |" in content
    assert "# 备忘" in content  # foreign content preserved
    assert "尾部注释" in content
    assert "旧清单" not in content  # replaced section dropped its old pending line


@pytest.mark.asyncio
async def test_terminal_handler_replay_is_idempotent() -> None:
    storage = _Storage()
    run, checkpoint = _records(status="completed")
    fixed_clock = datetime(2026, 9, 1, 20, 0, tzinfo=UTC)
    handler, service = _terminal_handler(
        storage=storage,
        snapshot=_snapshot(),
        clock=lambda: fixed_clock,
    )

    await handler.handle(run=run, checkpoint=checkpoint)
    first = storage.task_content
    await handler.handle(run=run, checkpoint=checkpoint)

    assert storage.task_content == first  # byte-identical replay
    assert first.count(f"task:{run.session_id}") == 1
    assert len(service.compare_calls) == 2
    for _, candidate in service.compare_calls:
        pointers = [item for item in candidate.open_items if isinstance(item, dict) and "task_state_ref" in item]
        assert len(pointers) == 1


@pytest.mark.asyncio
async def test_terminal_handler_preserves_unrelated_open_items_on_pointer_upsert() -> None:
    storage = _Storage()
    run, checkpoint = _records(status="completed")
    unrelated = {"some": "other open item"}
    old_task_pointer = {"task_state_ref": TASK_STATE_FILE_PATH}
    handler, service = _terminal_handler(
        storage=storage,
        snapshot=_snapshot(open_items=(old_task_pointer, unrelated)),
    )

    await handler.handle(run=run, checkpoint=checkpoint)

    _, candidate = service.compare_calls[0]
    assert unrelated in candidate.open_items
    pointers = [item for item in candidate.open_items if isinstance(item, dict) and "task_state_ref" in item]
    assert pointers == [{"task_state_ref": TASK_STATE_FILE_PATH}]
    assert len(candidate.open_items) == 2


@pytest.mark.asyncio
async def test_handlers_survive_storage_failure_without_raising(caplog) -> None:
    class BrokenStorage:
        async def exists(self, key: str) -> bool:
            raise OSError("storage down")

        async def is_file(self, key: str) -> bool:
            raise OSError("storage down")

        async def read_text(self, key: str, encoding: str = "utf-8", errors: str = "replace") -> str:
            raise OSError("storage down")

        async def write_text(self, key: str, content: str, encoding: str = "utf-8") -> None:
            raise OSError("storage down")

    run, checkpoint = _records(status="failed")
    handler = SessionTaskStateTerminalHandler(
        session_factory=_SessionFactory(),  # type: ignore[arg-type]
        context_service=_ContextService(_snapshot()),  # type: ignore[arg-type]
        storage=BrokenStorage(),  # type: ignore[arg-type]
    )

    # Must not raise: best-effort persistence never blocks terminal settlement.
    await handler.handle(run=run, checkpoint=checkpoint)


@pytest.mark.asyncio
async def test_terminal_handler_truncates_goal_to_100_chars() -> None:
    long_goal = "甲" * 150
    storage = _Storage()
    run, checkpoint = _records(status="completed", goal=long_goal)
    handler, _ = _terminal_handler(storage=storage, snapshot=_snapshot())

    await handler.handle(run=run, checkpoint=checkpoint)

    content = storage.writes[0][1]
    assert "目标：" + long_goal[:100] + " |" in content
    assert "目标：" + long_goal[:101] not in content  # truncated, no 101st char


# ---------------------------------------------------------------- completion phrases (D-11)


def test_completion_phrase_for_all_phases() -> None:
    assert completion_phrase_for(PHASE_COMPLETE, "completed") == "上一轮已完成"
    assert completion_phrase_for(PHASE_ACTIVE, "completed") == "上一轮任务已交付，仍有未决事项"
    assert completion_phrase_for(PHASE_PAUSED, "waiting_user") == "上一轮任务暂停，等待你的回复"
    assert completion_phrase_for(PHASE_BLOCKED, "failed") == "上一轮任务未完成（未成功）"
    assert completion_phrase_for(PHASE_BLOCKED, "cancelled") == "上一轮任务未完成（已取消）"


def test_completion_phrase_for_blocked_falls_back_without_known_ended() -> None:
    assert completion_phrase_for(PHASE_BLOCKED, None) == "上一轮任务未完成"
    assert completion_phrase_for(PHASE_BLOCKED, "weird") == "上一轮任务未完成"


def test_completion_phrase_for_unknown_phase_defaults_to_complete() -> None:
    assert completion_phrase_for("bogus", "completed") == "上一轮已完成"


# ---------------------------------------------------------------- pending-lists line / note


def _pending(list_id: str, title: str, item_count: int) -> PendingListRef:
    return PendingListRef(list_id=uuid.UUID(list_id), title=title, item_count=item_count)


def test_render_pending_lists_line_empty_is_none() -> None:
    section = _section(session_id="00000000-0000-0000-0000-0000000000aa")
    assert render_pending_lists_line(section) is None


def test_render_pending_lists_line_single() -> None:
    section = _section(
        session_id="00000000-0000-0000-0000-0000000000aa",
        pending=(_pending("00000000-0000-0000-0000-000000000001", "app 优化清单", 2),),
    )
    assert render_pending_lists_line(section) == (
        "未决事项：清单「app 优化清单」（2 项，见 memory/清单.md）"
    )


def test_render_pending_lists_line_joins_multiple_and_is_bounded_to_three() -> None:
    section = _section(
        session_id="00000000-0000-0000-0000-0000000000aa",
        pending=tuple(
            _pending(f"00000000-0000-0000-0000-00000000000{i}", f"清单{i}", i)
            for i in range(1, 6)
        ),
    )
    line = render_pending_lists_line(section)
    assert line.startswith("未决事项：")
    assert line.count("；") == 2  # three items → two separators
    assert "清单5" not in line  # the 5th item is dropped
    assert "清单1" in line and "清单3" in line


def test_render_task_state_note_uses_phrase_goal_and_past_tense_frame() -> None:
    section = _section(
        session_id="00000000-0000-0000-0000-0000000000aa",
        phase=PHASE_PAUSED,
        ended="waiting_user",
        goal="优化 app",
    )
    note = render_task_state_note(section, current_run_id="run-1")
    assert note["id"] == "session-task-state:run-1"
    assert note["role"] == "user"
    assert note["runtime_input"] == "session_task_state"
    assert note["content"] == (
        "历史上下文（非当前任务）：上一轮任务暂停，等待你的回复。任务「优化 app」。"
    )
    assert "目标：" not in note["content"]  # never imperative


def test_render_task_state_note_appends_pending_line_when_present() -> None:
    section = _section(
        session_id="00000000-0000-0000-0000-0000000000aa",
        phase=PHASE_ACTIVE,
        ended="completed",
        goal="优化 app",
        pending=(_pending("00000000-0000-0000-0000-000000000001", "app 优化清单", 2),),
    )
    note = render_task_state_note(section, current_run_id="run-1")
    assert "上一轮任务已交付，仍有未决事项" in note["content"]
    assert note["content"].endswith("未决事项：清单「app 优化清单」（2 项，见 memory/清单.md）")


def test_render_pending_lists_line_truncates_long_titles_to_30_chars() -> None:
    section = _section(
        session_id="00000000-0000-0000-0000-0000000000aa",
        pending=(_pending("00000000-0000-0000-0000-000000000001", "甲" * 40, 2),),
    )
    line = render_pending_lists_line(section)
    assert "清单「" + "甲" * 30 + "…」（2 项，见 memory/清单.md）" in line
    assert "甲" * 31 not in line  # truncated, no 31st char


def test_render_pending_lists_line_uses_fallback_title_when_blank() -> None:
    section = _section(
        session_id="00000000-0000-0000-0000-0000000000aa",
        pending=(
            _pending("00000000-0000-0000-0000-000000000001", "", 1),
            _pending("00000000-0000-0000-0000-000000000002", "   ", 2),
        ),
    )
    line = render_pending_lists_line(section)
    assert line.startswith("未决事项：清单「清单」（1 项，见 memory/清单.md）")
    assert "清单「清单」（2 项，见 memory/清单.md）" in line


def test_render_pending_lists_line_marks_overflow_with_etc() -> None:
    section = _section(
        session_id="00000000-0000-0000-0000-0000000000aa",
        pending=tuple(
            _pending(f"00000000-0000-0000-0000-00000000000{i}", f"清单{i}", i)
            for i in range(1, 5)
        ),
    )
    line = render_pending_lists_line(section)
    assert line.endswith("清单「清单3」（3 项，见 memory/清单.md）等")
    assert "清单4" not in line  # the 4th item is only marked, not listed


def test_render_task_state_note_omits_goal_when_empty() -> None:
    section = _section(
        session_id="00000000-0000-0000-0000-0000000000aa",
        phase=PHASE_PAUSED,
        ended="waiting_user",
        goal="",
    )
    note = render_task_state_note(section, current_run_id="run-1")
    assert note["content"] == "历史上下文（非当前任务）：上一轮任务暂停，等待你的回复。"


# ---------------------------------------------------------------- loader


def _task_file_content(
    *,
    session_id: uuid.UUID,
    run_id: uuid.UUID,
    phase: str = PHASE_ACTIVE,
    ended: str = "completed",
    goal: str = "优化 app",
) -> str:
    return (
        f"## task:{session_id} | phase: {phase} | ended: {ended} | "
        f"目标：{goal} | run:{run_id} | 2026-09-01 20:00\n"
    )


def _task_state_pointer() -> dict:
    return {"task_state_ref": TASK_STATE_FILE_PATH}


def _loader(
    *,
    storage: _Storage,
    snapshot: SessionContextSnapshot,
) -> SessionTaskStateLoader:
    return SessionTaskStateLoader(
        session_factory=_SessionFactory(),  # type: ignore[arg-type]
        context_service=_ContextService(snapshot),  # type: ignore[arg-type]
        storage=storage,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_loader_returns_section_for_session() -> None:
    session_id = uuid.uuid4()
    run_id = uuid.uuid4()
    storage = _Storage(task_content=_task_file_content(session_id=session_id, run_id=run_id))
    loader = _loader(storage=storage, snapshot=_snapshot(open_items=(_task_state_pointer(),)))

    section = await loader.load(
        tenant_id=uuid.uuid4(),
        session_id=session_id,
        agent_id=uuid.uuid4(),
        current_run_id=str(uuid.uuid4()),
    )

    assert section is not None
    assert section.session_id == session_id
    assert section.run_id == run_id
    assert section.phase == PHASE_ACTIVE


@pytest.mark.asyncio
async def test_loader_returns_none_without_pointer_row() -> None:
    session_id = uuid.uuid4()
    storage = _Storage(
        task_content=_task_file_content(session_id=session_id, run_id=uuid.uuid4())
    )
    loader = _loader(storage=storage, snapshot=_snapshot(open_items=()))

    section = await loader.load(
        tenant_id=uuid.uuid4(),
        session_id=session_id,
        agent_id=uuid.uuid4(),
        current_run_id=str(uuid.uuid4()),
    )

    assert section is None


@pytest.mark.asyncio
async def test_loader_returns_none_when_file_missing() -> None:
    session_id = uuid.uuid4()
    storage = _Storage()  # task_content is None → file does not exist
    loader = _loader(storage=storage, snapshot=_snapshot(open_items=(_task_state_pointer(),)))

    section = await loader.load(
        tenant_id=uuid.uuid4(),
        session_id=session_id,
        agent_id=uuid.uuid4(),
        current_run_id=str(uuid.uuid4()),
    )

    assert section is None


@pytest.mark.asyncio
async def test_loader_fails_open_when_section_cannot_be_parsed() -> None:
    session_id = uuid.uuid4()
    other = uuid.uuid4()
    # A file whose only parseable section belongs to another session plus a
    # broken header — neither yields this session's section (fail-open).
    storage = _Storage(
        task_content=(
            f"## task:{other} | phase: active | ended: completed | "
            f"目标：别的 | run:{uuid.uuid4()} | 2026-09-01 20:00\n"
            "## task:not-a-uuid | phase: active | whatever\n"
        )
    )
    loader = _loader(storage=storage, snapshot=_snapshot(open_items=(_task_state_pointer(),)))

    section = await loader.load(
        tenant_id=uuid.uuid4(),
        session_id=session_id,
        agent_id=uuid.uuid4(),
        current_run_id=str(uuid.uuid4()),
    )

    assert section is None


@pytest.mark.asyncio
async def test_loader_self_run_guard_returns_none() -> None:
    session_id = uuid.uuid4()
    run_id = uuid.uuid4()
    storage = _Storage(task_content=_task_file_content(session_id=session_id, run_id=run_id))
    loader = _loader(storage=storage, snapshot=_snapshot(open_items=(_task_state_pointer(),)))

    section = await loader.load(
        tenant_id=uuid.uuid4(),
        session_id=session_id,
        agent_id=uuid.uuid4(),
        current_run_id=str(run_id),  # same run → must not self-annotate
    )

    assert section is None


@pytest.mark.asyncio
async def test_loader_fails_open_when_pointer_read_raises() -> None:
    session_id = uuid.uuid4()
    run_id = uuid.uuid4()
    storage = _Storage(task_content=_task_file_content(session_id=session_id, run_id=run_id))

    class BrokenContextService:
        async def load_snapshot(self, db, *, tenant_id, session_id):
            del db, tenant_id, session_id
            raise RuntimeError("db down")

    loader = SessionTaskStateLoader(
        session_factory=_SessionFactory(),  # type: ignore[arg-type]
        context_service=BrokenContextService(),  # type: ignore[arg-type]
        storage=storage,  # type: ignore[arg-type]
    )

    section = await loader.load(
        tenant_id=uuid.uuid4(),
        session_id=session_id,
        agent_id=uuid.uuid4(),
        current_run_id=str(run_id),
    )

    assert section is None
