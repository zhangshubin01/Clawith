"""Deterministic numbered-list persistence (R1) and numbering contract (R5) tests."""

from __future__ import annotations

from collections import deque
from dataclasses import replace
import json
import uuid

import pytest

from app.services.agent_runtime.command_worker import (
    CheckpointObservation,
    RuntimeRunRecord,
)
from app.services.agent_runtime.context_builder import RuntimeContextBuild
from app.services.agent_runtime.list_persistence import (
    LIST_FILE_PATH,
    LIST_NUMBERING_CONTRACT,
    WAITING_STATUSES,
    ListPersistenceCompletionHandler,
    extract_list_title,
    extract_workspace_project,
    merge_list_items,
    parse_list_file,
    parse_numbered_list,
    render_list_file,
)
from app.services.agent_runtime.model_step_service import _prompt_messages
from app.services.agent_runtime.session_context_service import (
    SessionContextCandidate,
    SessionContextConflict,
    SessionContextSnapshot,
)
from app.services.agent_runtime.state import (
    RunInputSnapshots,
    RunRegistrySnapshot,
    RuntimeGraphState,
)


# ---------------------------------------------------------------- pure functions


def test_parse_numbered_list_accepts_em_dash_items() -> None:
    text = (
        "这是结论：\n\n"
        "1. 输入精度截断 — Calculator.kt:204 用 Float\n"
        "2. 超大指数上限 — power() 无上限\n"
        "3. 无缓存解析 — 每次重读文件\n"
    )
    parsed = parse_numbered_list(text)
    assert parsed is not None
    assert [item.title for item in parsed.items] == [
        "输入精度截断",
        "超大指数上限",
        "无缓存解析",
    ]
    assert parsed.items[0].description == "Calculator.kt:204 用 Float"
    assert [item.number for item in parsed.items] == [1, 2, 3]


def test_parse_numbered_list_rejects_prose() -> None:
    assert parse_numbered_list("这里没有编号，只有一段散文说明。") is None
    assert parse_numbered_list("") is None
    assert parse_numbered_list("完成。") is None


def test_parse_numbered_list_rejects_table_rows() -> None:
    text = (
        "| 编号 | 标题 | 说明 |\n"
        "|---|---|---|\n"
        "| 1 | 输入精度截断 | Calculator.kt:204 |\n"
        "| 2 | 超大指数上限 | power() |\n"
    )
    assert parse_numbered_list(text) is None


def test_parse_numbered_list_requires_two_items() -> None:
    assert parse_numbered_list("1. 单个条目 — 只出现一次的编号不算清单") is None


def test_parse_numbered_list_accepts_numbering_variants() -> None:
    text = "1、输入精度截断 — Calculator.kt:204\n2）超大指数上限 — power() 无上限\n"
    parsed = parse_numbered_list(text)
    assert parsed is not None
    assert [item.title for item in parsed.items] == ["输入精度截断", "超大指数上限"]


def test_parse_numbered_list_skips_lines_without_em_dash_separator() -> None:
    text = (
        "1. 输入精度截断 — Calculator.kt:204 用 Float\n"
        "   补充说明的第二行，没有编号\n"
        "2. 超大指数上限 — power() 无上限\n"
    )
    parsed = parse_numbered_list(text)
    assert parsed is not None
    assert len(parsed.items) == 2
    assert parsed.items[1].title == "超大指数上限"


def test_extract_list_title_prefers_heading_before_first_item() -> None:
    text = "结论如下：\n\n## app 优化清单\n\n1. A — a\n2. B — b\n"
    assert extract_list_title(text, fallback="goal") == "app 优化清单"


def test_extract_list_title_falls_back_to_goal() -> None:
    text = "1. A — a\n2. B — b\n"
    assert extract_list_title(text, fallback="修复 app 性能问题") == "修复 app 性能问题"
    long_goal = "很长的目标" * 20
    assert len(extract_list_title(text, fallback=long_goal)) == 40


def test_extract_workspace_project_most_frequent_segment() -> None:
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "c1",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps({"path": "workspace/mydome1/src/A.kt"}),
                    },
                },
                {
                    "id": "c2",
                    "function": {
                        "name": "edit_file",
                        "arguments": json.dumps({"path": "workspace/mydome1/src/B.kt"}),
                    },
                },
                {
                    "id": "c3",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": "workspace/other/README.md"}),
                    },
                },
            ],
        },
    ]
    assert extract_workspace_project(messages) == "mydome1"


def test_extract_workspace_project_breaks_ties_by_first_seen() -> None:
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "c1",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": "workspace/alpha/x"}),
                    },
                },
                {
                    "id": "c2",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": "workspace/beta/y"}),
                    },
                },
            ],
        },
    ]
    assert extract_workspace_project(messages) == "alpha"


def test_extract_workspace_project_none_without_workspace_paths() -> None:
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "c1",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps({"path": "memory/memory.md"}),
                    },
                },
            ],
        },
    ]
    assert extract_workspace_project(messages) is None
    assert extract_workspace_project([]) is None
    assert (
        extract_workspace_project(
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "bad",
                            "function": {
                                "name": "write_file",
                                "arguments": "not json",
                            },
                        },
                    ],
                },
            ]
        )
        is None
    )


def test_merge_list_items_continues_numbering_without_reordering() -> None:
    existing = parse_numbered_list("1. A — 甲\n2. B — 乙\n").items
    incoming = parse_numbered_list("1. A — 甲\n2. B — 乙\n3. C — 丙\n").items
    merged = merge_list_items(existing, incoming)
    assert [item.number for item in merged] == [1, 2, 3]
    assert [item.title for item in merged] == ["A", "B", "C"]


def test_merge_list_items_dedupes_by_full_text_for_idempotent_replay() -> None:
    existing = parse_numbered_list("1. A — 甲\n2. B — 乙\n").items
    incoming = parse_numbered_list("3. C — 丙\n4. D — 丁\n").items
    first = merge_list_items(existing, incoming)
    assert [item.number for item in first] == [1, 2, 3, 4]
    # Replaying the same terminal handler over the same file must not duplicate.
    replay = merge_list_items(first, incoming)
    assert replay == first


def test_parse_list_file_roundtrip_preserves_foreign_content() -> None:
    content = (
        "# 手写备忘\n"
        "这是 agent 自己写的内容。\n\n"
        "## list:00000000-0000-0000-0000-000000000001 | project: mydome1 | "
        "标题：app 优化清单 | 2026-09-01 18:00\n"
        "1. 输入精度截断 — Calculator.kt:204\n"
        "2. 超大指数上限 — power()\n\n"
        "尾部注释\n"
    )
    parsed_file = parse_list_file(content)
    assert len(parsed_file.sections) == 1
    section = parsed_file.sections[0]
    assert str(section.list_id) == "00000000-0000-0000-0000-000000000001"
    assert section.project == "mydome1"
    assert section.title == "app 优化清单"
    assert [item.title for item in section.items] == ["输入精度截断", "超大指数上限"]
    assert render_list_file(parsed_file) == content


def test_parse_list_file_treats_broken_section_headers_as_raw() -> None:
    content = "## list:not-a-uuid | whatever\n1. A — 甲\n"
    parsed_file = parse_list_file(content)
    assert parsed_file.sections == ()
    assert render_list_file(parsed_file) == content


# ---------------------------------------------------------------- R5 contract


def test_prompt_messages_inject_the_numbering_contract_into_system() -> None:
    build = RuntimeContextBuild(
        session_context_snapshot={"version": 1, "summary": "shared"},
        current_run={"goal": "Answer"},
        related_run_summaries=(),
        pending_session_messages_snapshot=(),
        recent_session_messages_snapshot=(),
        thread_running_summary=None,
        recent_thread_messages=(),
        initial_input={"message_id": "s1"},
        resume_input=None,
        omitted_tool_exchanges=(),
        retry_model=False,
        blocked=False,
        requires_confirmation=False,
    )
    messages = _prompt_messages(
        static_prompt="Static",
        dynamic_prompt="Dynamic",
        build=build,
    )
    system = messages[0].content
    assert "如产出编号清单" in system
    assert "N. 标题 — 一句话说明" in system
    assert "只增补不重排" in system
    assert "不得自行重排或猜测候选" in system
    # Stable byte prefix across turns: the contract is a constant appended once.
    again = _prompt_messages(static_prompt="Static", dynamic_prompt="Dynamic", build=build)
    assert again[0].content == system
    # Contract text lives with the parser it serves (single source of truth).
    assert "N. 标题 — 一句话说明" in LIST_NUMBERING_CONTRACT


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
    def __init__(self, *sessions: _Session) -> None:
        self.sessions = deque(sessions or [_Session()])
        self.calls = 0

    def __call__(self) -> _Session:
        self.calls += 1
        if len(self.sessions) > 1:
            return self.sessions.popleft()
        return self.sessions[0]


class _Storage:
    def __init__(self, content: str | None = None) -> None:
        self.content = content
        self.writes: list[tuple[str, str]] = []

    async def exists(self, key: str) -> bool:
        del key
        return self.content is not None

    async def is_file(self, key: str) -> bool:
        del key
        return self.content is not None

    async def read_text(self, key: str, encoding: str = "utf-8", errors: str = "replace") -> str:
        del key, encoding, errors
        assert self.content is not None
        return self.content

    async def write_text(self, key: str, content: str, encoding: str = "utf-8") -> None:
        del encoding
        self.writes.append((key, content))
        self.content = content


class _ContextService:
    def __init__(
        self,
        snapshots: list[SessionContextSnapshot],
        *,
        conflicts: int = 0,
    ) -> None:
        self.snapshots = deque(snapshots)
        self.compare_calls: list[tuple[int, SessionContextCandidate]] = []
        self._conflicts = conflicts

    async def load_snapshot(self, db, *, tenant_id, session_id):
        del db, tenant_id, session_id
        return self.snapshots.popleft()

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
    watermark: uuid.UUID | None = None,
    requirements: tuple = (),
    decisions: tuple = (),
    evidence_refs: tuple = (),
    workspace_refs: tuple = (),
    covered_through_message_id: uuid.UUID | None = None,
) -> SessionContextSnapshot:
    return SessionContextSnapshot(
        version=version,
        summary=summary,
        requirements=requirements,
        decisions=decisions,
        open_items=open_items,
        evidence_refs=evidence_refs,
        workspace_refs=workspace_refs,
        covered_through_message_id=(watermark if covered_through_message_id is None else covered_through_message_id),
    )


def _state(
    *,
    status: str = "completed",
    final_answer: str | None = None,
    messages: list | None = None,
    session_id: str | None = None,
    waiting_request: dict | None = None,
) -> RuntimeGraphState:
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
        session_id=session_id,
    )
    lifecycle: dict = {
        "status": status,
        "next_route": "terminal",
        "final_answer": final_answer,
    }
    if waiting_request is not None:
        lifecycle["waiting_request"] = waiting_request
    if status != "completed":
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
        "messages": messages or [],
        "lifecycle": lifecycle,
    }  # type: ignore[typeddict-item]


def _records(
    *,
    status: str = "completed",
    final_answer: str | None = ("1. 输入精度截断 — Calculator.kt:204 用 Float\n2. 超大指数上限 — power() 无上限\n"),
    messages: list | None = None,
    session_id: str | None = None,
    waiting_request: dict | None = None,
) -> tuple[RuntimeRunRecord, CheckpointObservation]:
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    run_id = uuid.uuid4()
    if session_id is None:
        session_id = str(uuid.uuid4())
    run = RuntimeRunRecord(
        tenant_id=tenant_id,
        run_id=run_id,
        thread_id=str(run_id),
        runtime_type="langgraph",
        goal="优化 app",
        run_kind="chat",
        source_type="chat",
        model_id=str(uuid.uuid4()),
        graph_name="runtime_graph",
        graph_version="v1",
        agent_id=str(agent_id),
        session_id=session_id,
    )
    state = _state(
        status=status,
        final_answer=final_answer,
        messages=messages,
        session_id=session_id,
        waiting_request=waiting_request,
    )
    checkpoint = CheckpointObservation(
        checkpoint_id="checkpoint-terminal",
        state=state,
    )
    return run, checkpoint


def _workspace_messages(project: str = "mydome1") -> list:
    return [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "c1",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps({"path": f"workspace/{project}/src/A.kt"}),
                    },
                },
            ],
        },
    ]


def _handler(
    *,
    storage: _Storage,
    context_service: _ContextService | None = None,
    snapshots: list | None = None,
    conflicts: int = 0,
    trigger_statuses: tuple[str, ...] = ("completed",),
) -> tuple[ListPersistenceCompletionHandler, _ContextService]:
    service = context_service or _ContextService(
        snapshots if snapshots is not None else [_snapshot()],
        conflicts=conflicts,
    )
    handler = ListPersistenceCompletionHandler(
        session_factory=_SessionFactory(),  # type: ignore[arg-type]
        context_service=service,  # type: ignore[arg-type]
        storage=storage,  # type: ignore[arg-type]
        trigger_statuses=trigger_statuses,
    )
    return handler, service


# ---------------------------------------------------------------- handler


@pytest.mark.asyncio
async def test_handler_writes_list_file_and_replaces_pointer() -> None:
    storage = _Storage()
    run, checkpoint = _records(messages=_workspace_messages())
    handler, service = _handler(storage=storage, snapshots=[_snapshot()])

    await handler.handle(run=run, checkpoint=checkpoint)

    assert storage.writes, "expected a file write"
    key, content = storage.writes[0]
    assert key.endswith(LIST_FILE_PATH)
    assert content.startswith("## list:")
    assert "| project: mydome1 |" in content
    assert "1. 输入精度截断 — Calculator.kt:204 用 Float" in content
    assert "2. 超大指数上限 — power() 无上限" in content
    # Pointer replaced via a full-array CAS write.
    assert len(service.compare_calls) == 1
    _, candidate = service.compare_calls[0]
    assert len(candidate.open_items) == 1
    pointer = candidate.open_items[0]
    assert pointer["list_ref"] == LIST_FILE_PATH
    assert pointer["project"] == "mydome1"
    assert isinstance(pointer["list_id"], str)
    assert pointer["list_id"] in content


@pytest.mark.asyncio
async def test_handler_noop_without_numbered_list() -> None:
    storage = _Storage()
    run, checkpoint = _records(
        final_answer="已完成优化，没有编号清单。",
        messages=_workspace_messages(),
    )
    handler, service = _handler(storage=storage, snapshots=[_snapshot()])

    await handler.handle(run=run, checkpoint=checkpoint)

    assert storage.writes == []
    assert service.compare_calls == []


@pytest.mark.asyncio
async def test_handler_noop_when_not_completed() -> None:
    storage = _Storage()
    run, checkpoint = _records(status="failed", final_answer=None)
    handler, service = _handler(storage=storage, snapshots=[_snapshot()])

    await handler.handle(run=run, checkpoint=checkpoint)

    assert storage.writes == []
    assert service.compare_calls == []


@pytest.mark.asyncio
async def test_handler_merges_same_project_reask_continuing_numbering() -> None:
    existing_content = (
        "## list:{list_id} | project: mydome1 | 标题：app 优化清单 | 2026-09-01 18:00\n"
        "1. 输入精度截断 — Calculator.kt:204 用 Float\n"
        "2. 超大指数上限 — power() 无上限\n"
    )
    list_id = uuid.uuid4()
    existing_content = existing_content.format(list_id=list_id)
    storage = _Storage(existing_content)
    final_answer = (
        "## app 优化清单\n\n"
        "1. 输入精度截断 — Calculator.kt:204 用 Float\n"
        "2. 超大指数上限 — power() 无上限\n"
        "3. 无缓存解析 — 每次重读文件\n"
    )
    run, checkpoint = _records(final_answer=final_answer, messages=_workspace_messages())
    old_pointer = {
        "list_ref": LIST_FILE_PATH,
        "list_id": str(list_id),
        "project": "mydome1",
    }
    unrelated = {"some": "other open item"}
    handler, service = _handler(
        storage=storage,
        snapshots=[_snapshot(open_items=(old_pointer, unrelated))],
    )

    await handler.handle(run=run, checkpoint=checkpoint)

    key, content = storage.writes[0]
    assert key.endswith(LIST_FILE_PATH)
    assert f"list:{list_id}" in content  # same list continued
    assert content.count("## list:") == 1  # one latest version
    assert "1. 输入精度截断" in content
    assert "2. 超大指数上限" in content
    assert "3. 无缓存解析 — 每次重读文件" in content
    # Pointer row replaced, unrelated open items preserved.
    _, candidate = service.compare_calls[0]
    assert len(candidate.open_items) == 2
    pointers = [item for item in candidate.open_items if isinstance(item, dict) and "list_ref" in item]
    assert len(pointers) == 1
    assert pointers[0]["list_id"] == str(list_id)
    assert unrelated in candidate.open_items


@pytest.mark.asyncio
async def test_handler_falls_back_to_session_pointer_without_workspace_paths() -> None:
    list_id = uuid.uuid4()
    storage = _Storage(
        "## list:{0} | project: - | 标题：app 优化清单 | 2026-09-01 18:00\n"
        "1. 输入精度截断 — Calculator.kt:204 用 Float\n"
        "2. 超大指数上限 — power() 无上限\n".format(list_id)
    )
    final_answer = (
        "1. 输入精度截断 — Calculator.kt:204 用 Float\n2. 超大指数上限 — power() 无上限\n3. 无缓存解析 — 每次重读文件\n"
    )
    run, checkpoint = _records(final_answer=final_answer, messages=[])
    pointer = {"list_ref": LIST_FILE_PATH, "list_id": str(list_id), "project": None}
    handler, service = _handler(
        storage=storage,
        snapshots=[
            _snapshot(open_items=(pointer,)),
            _snapshot(open_items=(pointer,)),
        ],
    )

    await handler.handle(run=run, checkpoint=checkpoint)

    content = storage.writes[0][1]
    assert f"list:{list_id}" in content
    assert "3. 无缓存解析 — 每次重读文件" in content
    _, candidate = service.compare_calls[0]
    pointers = [item for item in candidate.open_items if isinstance(item, dict) and "list_ref" in item]
    assert pointers[0]["list_id"] == str(list_id)


@pytest.mark.asyncio
async def test_handler_skips_pointer_without_session() -> None:
    storage = _Storage()
    run, checkpoint = _records(messages=_workspace_messages(), session_id=None)
    run = replace(run, session_id=None)
    handler, service = _handler(storage=storage, snapshots=[_snapshot()])

    await handler.handle(run=run, checkpoint=checkpoint)

    assert storage.writes, "file must still be written without a session"
    assert service.compare_calls == []


@pytest.mark.asyncio
async def test_handler_retries_pointer_cas_on_conflict() -> None:
    storage = _Storage()
    run, checkpoint = _records(messages=_workspace_messages())
    handler, service = _handler(
        storage=storage,
        snapshots=[_snapshot(), _snapshot()],
        conflicts=1,
    )

    await handler.handle(run=run, checkpoint=checkpoint)

    assert len(service.compare_calls) == 1
    assert storage.writes, "file write must still happen"


@pytest.mark.asyncio
async def test_handler_keeps_only_latest_version_of_one_list() -> None:
    list_id = uuid.uuid4()
    storage = _Storage(
        "## list:{0} | project: mydome1 | 标题：app 优化清单 | 2026-08-30 10:00\n"
        "1. 输入精度截断 — Calculator.kt:204 用 Float\n\n"
        "## list:{0} | project: mydome1 | 标题：app 优化清单 | 2026-09-01 18:00\n"
        "1. 输入精度截断 — Calculator.kt:204 用 Float\n"
        "2. 超大指数上限 — power() 无上限\n".format(list_id)
    )
    final_answer = (
        "1. 输入精度截断 — Calculator.kt:204 用 Float\n2. 超大指数上限 — power() 无上限\n3. 无缓存解析 — 每次重读文件\n"
    )
    run, checkpoint = _records(final_answer=final_answer, messages=_workspace_messages())
    handler, _ = _handler(storage=storage, snapshots=[_snapshot()])

    await handler.handle(run=run, checkpoint=checkpoint)

    content = storage.writes[0][1]
    assert content.count("## list:") == 1
    assert "3. 无缓存解析 — 每次重读文件" in content
    assert "2026-08-30 10:00" not in content


@pytest.mark.asyncio
async def test_handler_noop_when_merged_list_unchanged() -> None:
    list_id = uuid.uuid4()
    storage = _Storage(
        "## list:{0} | project: mydome1 | 标题：app 优化清单 | 2026-09-01 18:00\n"
        "1. 输入精度截断 — Calculator.kt:204 用 Float\n"
        "2. 超大指数上限 — power() 无上限\n".format(list_id)
    )
    final_answer = "1. 输入精度截断 — Calculator.kt:204 用 Float\n2. 超大指数上限 — power() 无上限\n"
    run, checkpoint = _records(final_answer=final_answer, messages=_workspace_messages())
    handler, service = _handler(
        storage=storage,
        snapshots=[
            _snapshot(
                open_items=({"list_ref": LIST_FILE_PATH, "list_id": str(list_id), "project": "mydome1"},),
            )
        ],
    )

    await handler.handle(run=run, checkpoint=checkpoint)

    assert storage.writes == []
    assert service.compare_calls == []


# ---------------------------------------------------------------- waiting trigger (ticket 06)


def _waiting_request(question: str) -> dict:
    return {
        "waiting_type": "user",
        "correlation_id": str(uuid.uuid4()),
        "question": question,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("status", WAITING_STATUSES)
async def test_handler_waiting_trigger_persists_numbered_list(status: str) -> None:
    storage = _Storage()
    run, checkpoint = _records(
        status=status,
        final_answer=None,
        waiting_request=_waiting_request(
            "你想优化哪些？\n\n"
            "1. 输入精度截断 — Calculator.kt:204 用 Float\n"
            "2. 超大指数上限 — power() 无上限\n"
        ),
        messages=_workspace_messages(),
    )
    handler, service = _handler(
        storage=storage,
        snapshots=[_snapshot()],
        trigger_statuses=WAITING_STATUSES,
    )

    await handler.handle(run=run, checkpoint=checkpoint)

    assert storage.writes, "expected a file write for a waiting closing list"
    key, content = storage.writes[0]
    assert key.endswith(LIST_FILE_PATH)
    assert "1. 输入精度截断 — Calculator.kt:204 用 Float" in content
    assert "2. 超大指数上限 — power() 无上限" in content
    assert len(service.compare_calls) == 1
    _, candidate = service.compare_calls[0]
    assert len(candidate.open_items) == 1
    assert candidate.open_items[0]["list_ref"] == LIST_FILE_PATH


@pytest.mark.asyncio
async def test_handler_waiting_trigger_noop_without_numbered_list() -> None:
    storage = _Storage()
    run, checkpoint = _records(
        status="waiting_user",
        final_answer=None,
        waiting_request=_waiting_request("请确认是否继续优化？"),
    )
    handler, service = _handler(
        storage=storage,
        snapshots=[_snapshot()],
        trigger_statuses=WAITING_STATUSES,
    )

    await handler.handle(run=run, checkpoint=checkpoint)

    assert storage.writes == []
    assert service.compare_calls == []


@pytest.mark.asyncio
async def test_handler_default_trigger_ignores_waiting_status() -> None:
    storage = _Storage()
    run, checkpoint = _records(
        status="waiting_user",
        final_answer=None,
        waiting_request=_waiting_request(
            "1. 输入精度截断 — Calculator.kt:204 用 Float\n"
            "2. 超大指数上限 — power() 无上限\n"
        ),
        messages=_workspace_messages(),
    )
    handler, service = _handler(storage=storage, snapshots=[_snapshot()])

    await handler.handle(run=run, checkpoint=checkpoint)

    assert storage.writes == []
    assert service.compare_calls == []


@pytest.mark.asyncio
async def test_handler_waiting_replay_is_idempotent() -> None:
    storage = _Storage()
    run, checkpoint = _records(
        status="waiting_user",
        final_answer=None,
        waiting_request=_waiting_request(
            "1. 输入精度截断 — Calculator.kt:204 用 Float\n"
            "2. 超大指数上限 — power() 无上限\n"
        ),
        messages=_workspace_messages(),
    )
    handler, _ = _handler(
        storage=storage,
        snapshots=[_snapshot()],
        trigger_statuses=WAITING_STATUSES,
    )

    await handler.handle(run=run, checkpoint=checkpoint)
    assert len(storage.writes) == 1
    # Replaying the same waiting closing over the now-merged file must not
    # write again (merged list is unchanged).
    await handler.handle(run=run, checkpoint=checkpoint)
    assert len(storage.writes) == 1
