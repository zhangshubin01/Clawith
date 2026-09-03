"""Cross-session open-list title injection (R3) tests.

Covers the unconditional title-index injection (no intent detection), the
retrieval across the current + recent sessions with the shared pointer-line
bounds (≤3 lists, 30-char titles, ≤20 item titles, explicit truncation
marker), the past-tense non-imperative note framing, and the strict no-op
invariant on a miss.
"""

from __future__ import annotations

import uuid

import pytest

from app.services.agent_runtime import context_builder
from app.services.agent_runtime.cross_session_retrieval import (
    CrossSessionListRetriever,
    ListRetrievalResult,
    RetrievedListSection,
    render_retrieval_note,
)
from app.services.agent_runtime.list_persistence import (
    LIST_FILE_PATH,
    ListItem,
)
from app.services.agent_runtime.session_context_service import (
    SessionContextSnapshot,
)
from app.services.agent_runtime.state import (
    RunInputSnapshots,
    RunRegistrySnapshot,
    RuntimeContext,
    RuntimeGraphState,
)


# ---------------------------------------------------------------- rendering


def test_render_note_is_past_tense_and_non_imperative() -> None:
    section = RetrievedListSection(
        title="app 优化清单",
        items=(
            ListItem(number=1, title="输入精度截断", description="Calculator.kt:204 用 Float"),
            ListItem(number=3, title="无缓存解析", description="每次重读文件"),
        ),
        total_count=2,
    )
    note = render_retrieval_note(
        ListRetrievalResult(sections=(section,), total_lists=1),
        current_run_id="run-1",
    )
    assert note["role"] == "user"
    assert note["runtime_input"] == "cross_session_list"
    assert note["id"] == "cross-session-list:run-1"
    content = note["content"]
    assert content.startswith("历史上下文（非当前任务）：此前已确认、尚未完结的清单：")
    assert "清单「app 优化清单」（2 项）：" in content
    assert "1. 输入精度截断" in content
    assert "3. 无缓存解析" in content
    # Titles only (A2): descriptions never enter the note.
    assert "Calculator.kt:204 用 Float" not in content
    assert "每次重读文件" not in content
    # No present-tense "当前未决" wording; never imperative or goal-style
    # (direct-chat-run-boundary-fix).
    assert "当前未决" not in content
    assert "目标：" not in content
    assert not content.lstrip().startswith("请")


def test_render_note_truncation_marker_and_extra_lists() -> None:
    first = RetrievedListSection(
        title="app 优化清单",
        items=(ListItem(number=1, title="输入精度截断", description="d"),),
        total_count=25,
    )
    second = RetrievedListSection(
        title="部署清单",
        items=(ListItem(number=1, title="灰度", description="d"),),
        total_count=1,
    )
    note = render_retrieval_note(
        ListRetrievalResult(sections=(first, second), total_lists=3),
        current_run_id="run-1",
    )
    content = note["content"]
    assert "清单「app 优化清单」（25 项）：" in content
    assert "（仅列出前 1 项；完整内容见 memory/清单.md）" in content
    assert "清单「部署清单」（1 项）：" in content
    # A list dropped past the three-list bound is marked, never silent.
    assert content.endswith("等")


# ---------------------------------------------------------------- retriever fakes


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _SessionFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> _Session:
        self.calls += 1
        return _Session()


class _Storage:
    def __init__(self, content: str | None = None) -> None:
        self.content = content
        self.read_keys: list[str] = []

    async def exists(self, key: str) -> bool:
        del key
        return self.content is not None

    async def is_file(self, key: str) -> bool:
        del key
        return self.content is not None

    async def read_text(self, key: str, encoding: str = "utf-8", errors: str = "replace") -> str:
        del encoding, errors
        self.read_keys.append(key)
        return self.content if self.content is not None else ""


class _ContextService:
    def __init__(
        self,
        snapshot_open_items: tuple = (),
        recent: list | None = None,
        trigger_recent: list | None = None,
    ) -> None:
        self._snapshot = snapshot_open_items
        self._recent = recent if recent is not None else []
        self._trigger_recent = trigger_recent if trigger_recent is not None else []
        self.load_snapshot_calls: list = []
        self.recent_calls: list = []
        self.trigger_recent_calls: list = []

    async def load_snapshot(self, db, *, tenant_id, session_id):
        del db
        self.load_snapshot_calls.append((tenant_id, session_id))
        return SessionContextSnapshot(
            version=1,
            summary="",
            requirements=(),
            decisions=(),
            open_items=tuple(self._snapshot),
            evidence_refs=(),
            workspace_refs=(),
            covered_through_message_id=None,
        )

    async def load_recent_sessions_open_items(
        self,
        db,
        *,
        tenant_id,
        agent_id,
        user_id,
        exclude_session_id=None,
        limit=5,
    ):
        del db
        self.recent_calls.append((tenant_id, agent_id, user_id, exclude_session_id, limit))
        return tuple(self._recent)

    async def load_recent_agent_trigger_sessions_open_items(
        self,
        db,
        *,
        tenant_id,
        agent_id,
        exclude_session_id=None,
        limit=5,
    ):
        del db
        self.trigger_recent_calls.append((tenant_id, agent_id, exclude_session_id, limit))
        return tuple(self._trigger_recent)


def _pointer(list_id: uuid.UUID, project: str | None = "mydome1") -> dict:
    return {
        "list_ref": LIST_FILE_PATH,
        "list_id": str(list_id),
        "project": project,
    }


_DEFAULT_ITEMS = (
    (1, "输入精度截断", "Calculator.kt:204 用 Float"),
    (2, "超大指数上限", "power() 无上限"),
    (3, "无缓存解析", "每次重读文件"),
    (4, "内存泄漏", "Bitmap 未回收"),
    (5, "网络线程", "主线程 IO"),
)


def _list_file_content(
    list_id: uuid.UUID,
    *,
    project: str = "mydome1",
    title: str = "app 优化清单",
    items: tuple | None = None,
) -> str:
    lines = [f"## list:{list_id} | project: {project} | 标题：{title} | 2026-09-01 18:00"]
    for number, item_title, description in items or _DEFAULT_ITEMS:
        lines.append(f"{number}. {item_title} — {description}")
    return "\n".join(lines) + "\n"


def _retriever(
    *,
    storage: _Storage,
    context_service: _ContextService,
    max_sessions: int = 5,
) -> CrossSessionListRetriever:
    return CrossSessionListRetriever(
        session_factory=_SessionFactory(),  # type: ignore[arg-type]
        context_service=context_service,  # type: ignore[arg-type]
        storage=storage,  # type: ignore[arg-type]
        max_sessions=max_sessions,
    )


# ---------------------------------------------------------------- retriever


@pytest.mark.asyncio
async def test_retrieve_same_session_injects_all_titles() -> None:
    list_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session_id = uuid.uuid4()
    storage = _Storage(_list_file_content(list_id))
    service = _ContextService(snapshot_open_items=(_pointer(list_id),))
    retriever = _retriever(storage=storage, context_service=service)

    result = await retriever.retrieve(
        tenant_id=tenant_id,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        project="mydome1",
    )

    assert result is not None
    assert result.total_lists == 1
    assert len(result.sections) == 1
    section = result.sections[0]
    assert section.title == "app 优化清单"
    assert section.total_count == 5
    assert [item.number for item in section.items] == [1, 2, 3, 4, 5]
    assert service.load_snapshot_calls == [(tenant_id, session_id)]
    assert service.recent_calls == [(tenant_id, agent_id, user_id, session_id, 5)]


@pytest.mark.asyncio
async def test_retrieve_cross_session_injects_all_titles() -> None:
    list_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    current_session_id = uuid.uuid4()
    prior_session_id = uuid.uuid4()
    storage = _Storage(_list_file_content(list_id))
    service = _ContextService(
        snapshot_open_items=(),
        recent=[(prior_session_id, (_pointer(list_id),))],
    )
    retriever = _retriever(storage=storage, context_service=service)

    result = await retriever.retrieve(
        tenant_id=tenant_id,
        agent_id=agent_id,
        user_id=user_id,
        session_id=current_session_id,
        project=None,
    )

    assert result is not None
    assert [item.number for item in result.sections[0].items] == [1, 2, 3, 4, 5]
    # The prior session's pointer was found; the current session was excluded.
    assert service.recent_calls == [(tenant_id, agent_id, user_id, current_session_id, 5)]


@pytest.mark.asyncio
async def test_retrieve_background_run_falls_back_to_agent_trigger_sessions() -> None:
    # Regression for run 35338e16: a background run has no acting user
    # (user_id=None) and its fresh run-scoped trigger session has no pointer
    # at step time. The open-list title index must still resolve from the
    # agent's own recent trigger sessions.
    list_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    current_session_id = uuid.uuid4()
    prior_trigger_session_id = uuid.uuid4()
    storage = _Storage(_list_file_content(list_id))
    service = _ContextService(
        snapshot_open_items=(),
        trigger_recent=[(prior_trigger_session_id, (_pointer(list_id),))],
    )
    retriever = _retriever(storage=storage, context_service=service)

    result = await retriever.retrieve(
        tenant_id=tenant_id,
        agent_id=agent_id,
        user_id=None,
        session_id=current_session_id,
        project="mydome1",
    )

    assert result is not None
    assert result.total_lists == 1
    assert [item.number for item in result.sections[0].items] == [1, 2, 3, 4, 5]
    assert service.load_snapshot_calls == [(tenant_id, current_session_id)]
    assert service.trigger_recent_calls == [(tenant_id, agent_id, current_session_id, 5)]
    assert service.recent_calls == []


@pytest.mark.asyncio
async def test_retrieve_background_current_session_first_then_trigger_dedup() -> None:
    # Current-session pointers come first; the trigger-session fallback
    # contributes only pointers not already seen (dedup), after them.
    current_list_id = uuid.uuid4()
    shared_list_id = uuid.uuid4()
    prior_list_id = uuid.uuid4()
    storage = _Storage(
        _list_file_content(current_list_id, title="当前清单")
        + _list_file_content(shared_list_id, title="共享清单")
        + _list_file_content(prior_list_id, title="回退清单")
    )
    service = _ContextService(
        snapshot_open_items=(_pointer(current_list_id), _pointer(shared_list_id)),
        trigger_recent=[(uuid.uuid4(), (_pointer(shared_list_id), _pointer(prior_list_id)))],
    )
    retriever = _retriever(storage=storage, context_service=service)

    result = await retriever.retrieve(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=None,
        session_id=uuid.uuid4(),
        project=None,
    )

    assert result is not None
    assert result.total_lists == 3
    assert [section.title for section in result.sections] == ["当前清单", "共享清单", "回退清单"]


@pytest.mark.asyncio
async def test_retrieve_with_user_keeps_direct_path_only() -> None:
    # A run with an acting user keeps the direct-session path and never
    # consults the agent-internal trigger-session fallback.
    list_id = uuid.uuid4()
    user_id = uuid.uuid4()
    storage = _Storage(_list_file_content(list_id))
    service = _ContextService(
        snapshot_open_items=(),
        recent=[(uuid.uuid4(), (_pointer(list_id),))],
        trigger_recent=[(uuid.uuid4(), (_pointer(list_id),))],
    )
    retriever = _retriever(storage=storage, context_service=service)

    result = await retriever.retrieve(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=user_id,
        session_id=uuid.uuid4(),
        project=None,
    )

    assert result is not None
    assert service.trigger_recent_calls == []
    assert len(service.recent_calls) == 1


@pytest.mark.asyncio
async def test_retrieve_background_without_any_pointer_is_noop() -> None:
    storage = _Storage(None)
    service = _ContextService(snapshot_open_items=(), trigger_recent=[])
    retriever = _retriever(storage=storage, context_service=service)

    result = await retriever.retrieve(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=None,
        session_id=uuid.uuid4(),
        project=None,
    )

    assert result is None


@pytest.mark.asyncio
async def test_retrieve_background_project_mismatch_is_noop() -> None:
    # The project filter applies on the fallback path too: a pointer for a
    # different project must not resolve.
    list_id = uuid.uuid4()
    storage = _Storage(_list_file_content(list_id, project="mydome1"))
    service = _ContextService(
        snapshot_open_items=(),
        trigger_recent=[(uuid.uuid4(), (_pointer(list_id, project="other-project"),))],
    )
    retriever = _retriever(storage=storage, context_service=service)

    result = await retriever.retrieve(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=None,
        session_id=uuid.uuid4(),
        project="mydome1",
    )

    assert result is None


@pytest.mark.asyncio
async def test_retrieve_project_filter_narrows() -> None:
    list_id = uuid.uuid4()
    storage = _Storage(_list_file_content(list_id, project="mydome1"))
    # Pointer belongs to a different project: must not match.
    service = _ContextService(snapshot_open_items=(_pointer(list_id, project="other-project"),))
    retriever = _retriever(storage=storage, context_service=service)

    result = await retriever.retrieve(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        project="mydome1",
    )

    assert result is None


@pytest.mark.asyncio
async def test_retrieve_session_level_pointer_is_wildcard_for_known_project() -> None:
    # D1 degradation: a pointer persisted without a resolvable project
    # (project=None) must stay retrievable even when the querying run knows
    # its project.
    list_id = uuid.uuid4()
    storage = _Storage(_list_file_content(list_id))
    service = _ContextService(snapshot_open_items=(_pointer(list_id, project=None),))
    retriever = _retriever(storage=storage, context_service=service)

    result = await retriever.retrieve(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        project="mydome1",
    )

    assert result is not None
    assert result.sections[0].title == "app 优化清单"
    assert [item.number for item in result.sections[0].items] == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_retrieve_exact_project_list_first_then_wildcard() -> None:
    # With both an exact-project pointer and a session-level (project=None)
    # pointer available, both lists are injected; the exact match comes first
    # (same pointer order as TaskSection.pending_lists).
    exact_list_id = uuid.uuid4()
    wildcard_list_id = uuid.uuid4()
    storage = _Storage(
        _list_file_content(exact_list_id, title="mydome1 清单")
        + _list_file_content(wildcard_list_id, title="会话级清单")
    )
    service = _ContextService(
        snapshot_open_items=(
            _pointer(wildcard_list_id, project=None),
            _pointer(exact_list_id, project="mydome1"),
        )
    )
    retriever = _retriever(storage=storage, context_service=service)

    result = await retriever.retrieve(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        project="mydome1",
    )

    assert result is not None
    assert result.total_lists == 2
    assert [section.title for section in result.sections] == ["mydome1 清单", "会话级清单"]


@pytest.mark.asyncio
async def test_retrieve_most_recent_list_first_without_project() -> None:
    newer_list_id = uuid.uuid4()
    older_list_id = uuid.uuid4()
    content = _list_file_content(older_list_id, title="旧清单") + _list_file_content(newer_list_id, title="新清单")
    storage = _Storage(content)
    # No current-session pointer; two prior sessions, newest first.
    service = _ContextService(
        snapshot_open_items=(),
        recent=[
            (uuid.uuid4(), (_pointer(newer_list_id),)),
            (uuid.uuid4(), (_pointer(older_list_id),)),
        ],
    )
    retriever = _retriever(storage=storage, context_service=service)

    result = await retriever.retrieve(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        project=None,
    )

    assert result is not None
    assert [section.title for section in result.sections] == ["新清单", "旧清单"]


@pytest.mark.asyncio
async def test_retrieve_caps_sections_at_three_and_keeps_pointer_order() -> None:
    list_ids = [uuid.uuid4() for _ in range(4)]
    content = "".join(_list_file_content(list_id, title=f"清单{index}") for index, list_id in enumerate(list_ids))
    storage = _Storage(content)
    service = _ContextService(snapshot_open_items=tuple(_pointer(list_id) for list_id in list_ids))
    retriever = _retriever(storage=storage, context_service=service)

    result = await retriever.retrieve(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        project="mydome1",
    )

    assert result is not None
    assert result.total_lists == 4
    assert [section.title for section in result.sections] == ["清单0", "清单1", "清单2"]


@pytest.mark.asyncio
async def test_retrieve_caps_item_titles_at_twenty() -> None:
    list_id = uuid.uuid4()
    items = tuple((number, f"条目{number}", f"描述{number}") for number in range(1, 26))
    storage = _Storage(_list_file_content(list_id, items=items))
    service = _ContextService(snapshot_open_items=(_pointer(list_id),))
    retriever = _retriever(storage=storage, context_service=service)

    result = await retriever.retrieve(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        project=None,
    )

    assert result is not None
    section = result.sections[0]
    assert section.total_count == 25
    assert [item.number for item in section.items] == list(range(1, 21))


@pytest.mark.asyncio
async def test_retrieve_miss_when_list_section_absent() -> None:
    # The pointer exists but the list file carries no such section: no-op.
    pointer_list_id = uuid.uuid4()
    stored_list_id = uuid.uuid4()
    storage = _Storage(_list_file_content(stored_list_id))
    service = _ContextService(snapshot_open_items=(_pointer(pointer_list_id),))
    retriever = _retriever(storage=storage, context_service=service)

    result = await retriever.retrieve(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        project="mydome1",
    )

    assert result is None


@pytest.mark.asyncio
async def test_retrieve_miss_without_pointers() -> None:
    storage = _Storage(_list_file_content(uuid.uuid4()))
    service = _ContextService(snapshot_open_items=(), recent=[])
    retriever = _retriever(storage=storage, context_service=service)

    result = await retriever.retrieve(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        project=None,
    )

    assert result is None
    assert storage.read_keys == []  # no file read when no pointer exists


@pytest.mark.asyncio
async def test_retrieve_miss_when_list_file_missing() -> None:
    list_id = uuid.uuid4()
    storage = _Storage(None)
    service = _ContextService(snapshot_open_items=(_pointer(list_id),))
    retriever = _retriever(storage=storage, context_service=service)

    result = await retriever.retrieve(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        project="mydome1",
    )

    assert result is None


# ---------------------------------------------------------------- build wiring


class _FakeRetriever:
    def __init__(self, result: ListRetrievalResult | None) -> None:
        self.result = result
        self.calls: list[dict] = []

    async def retrieve(self, **kwargs) -> ListRetrievalResult | None:
        self.calls.append(kwargs)
        return self.result


def _snapshots() -> RunInputSnapshots:
    return RunInputSnapshots(
        session_context=SessionContextSnapshot.empty().to_json(),
        session_context_version=0,
        recent_session_messages=(),
        related_run_summaries=(),
        initial_input={"content": "start"},
    )


def _state(*, run_id: str, goal: str) -> RuntimeGraphState:
    return {
        "registry": RunRegistrySnapshot(
            tenant_id=str(uuid.uuid4()),
            run_id=run_id,
            goal=goal,
            run_kind="chat",
            source_type="chat",
            model_id=str(uuid.uuid4()),
            graph_name="runtime_graph",
            graph_version="v1",
            agent_id=str(uuid.uuid4()),
            session_id=str(uuid.uuid4()),
        ),
        "snapshots": _snapshots(),
        "messages": [
            {
                "id": f"current-input-{run_id}",
                "role": "user",
                "content": goal,
                "runtime_input": "current",
                "runtime_run_id": run_id,
            },
        ],
        "lifecycle": {"status": "running", "next_route": "model"},
    }


def _context(state: RuntimeGraphState, *, actor_user_id: str | None = None) -> RuntimeContext:
    registry = state["registry"]
    return RuntimeContext(
        tenant_id=registry.tenant_id,
        run_id=registry.run_id,
        command_id="command-1",
        executor=object(),  # type: ignore[arg-type]
        goal=registry.goal,
        run_kind=registry.run_kind,
        source_type=registry.source_type,
        model_id=registry.model_id,
        graph_name=registry.graph_name,
        graph_version=registry.graph_version,
        agent_id=registry.agent_id,
        session_id=registry.session_id,
        actor_user_id=actor_user_id,
    )


def _builder(
    retriever: CrossSessionListRetriever | _FakeRetriever | None,
) -> context_builder.ContextBuilder:
    return context_builder.ContextBuilder(
        session_context_service=object(),  # type: ignore[arg-type]
        cross_session_retriever=retriever,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_build_injects_retrieval_note_on_hit() -> None:
    run_id = str(uuid.uuid4())
    result = ListRetrievalResult(
        sections=(
            RetrievedListSection(
                title="app 优化清单",
                items=(
                    ListItem(number=1, title="输入精度截断", description="Calculator.kt:204 用 Float"),
                    ListItem(number=3, title="无缓存解析", description="每次重读文件"),
                ),
                total_count=2,
            ),
        ),
        total_lists=1,
    )
    fake = _FakeRetriever(result)
    state = _state(run_id=run_id, goal="那执行 1→2→3→4（P1）")
    builder = _builder(fake)

    built = await builder.build(state, _context(state))

    assert len(fake.calls) == 1
    call = fake.calls[0]
    # No intent detection: no signal is extracted from the goal anymore.
    assert "signal" not in call
    assert call["agent_id"] == uuid.UUID(state["registry"].agent_id)
    assert call["session_id"] == uuid.UUID(state["registry"].session_id)
    assert call["user_id"] is None
    assert call["project"] is None

    messages = built.recent_thread_messages
    assert messages[0]["runtime_input"] == "cross_session_list"
    assert "输入精度截断" in messages[0]["content"]
    assert "无缓存解析" in messages[0]["content"]
    assert messages[1]["id"] == f"current-input-{run_id}"


@pytest.mark.asyncio
async def test_build_noop_when_retriever_misses() -> None:
    run_id = str(uuid.uuid4())
    fake = _FakeRetriever(None)
    state = _state(run_id=run_id, goal="做 1、2、3、5")
    builder = _builder(fake)

    built = await builder.build(state, _context(state))

    assert len(fake.calls) == 1
    messages = built.recent_thread_messages
    assert [m["id"] for m in messages] == [f"current-input-{run_id}"]


@pytest.mark.asyncio
async def test_build_invokes_retriever_even_without_reference() -> None:
    # The open-list note is unconditional session standing context: a goal with
    # no list reference still retrieves (and injects nothing on a miss).
    run_id = str(uuid.uuid4())
    fake = _FakeRetriever(None)
    state = _state(run_id=run_id, goal="优化 app 性能")
    builder = _builder(fake)

    built = await builder.build(state, _context(state))

    assert len(fake.calls) == 1
    assert [m["id"] for m in built.recent_thread_messages] == [f"current-input-{run_id}"]


@pytest.mark.asyncio
async def test_build_noop_without_retriever_configured() -> None:
    run_id = str(uuid.uuid4())
    state = _state(run_id=run_id, goal="做 1、2、3、5")
    builder = _builder(None)

    built = await builder.build(state, _context(state))

    assert [m["id"] for m in built.recent_thread_messages] == [f"current-input-{run_id}"]


@pytest.mark.asyncio
async def test_build_goal_arrow_numbers_injects_all_titles() -> None:
    # Regression for the 2026-09-02 incident (run 5ad111a9): the goal
    # "那执行 1→2→3→4（P1）" used to be regex-truncated to the single number 1,
    # injecting only the first list item. With detection deleted the whole
    # title index is injected, so every number in the goal aligns to a title.
    list_id = uuid.uuid4()
    storage = _Storage(_list_file_content(list_id))
    service = _ContextService(snapshot_open_items=(_pointer(list_id),))
    retriever = _retriever(storage=storage, context_service=service)
    run_id = str(uuid.uuid4())
    state = _state(run_id=run_id, goal="那执行 1→2→3→4（P1）")
    builder = _builder(retriever)

    built = await builder.build(state, _context(state))

    notes = [
        message for message in built.recent_thread_messages if message.get("runtime_input") == "cross_session_list"
    ]
    assert len(notes) == 1
    content = notes[0]["content"]
    for number, title, _description in _DEFAULT_ITEMS:
        assert f"{number}. {title}" in content
    # The old failure mode injected only item 1; all five titles are present.
    assert "5. 网络线程" in content


@pytest.mark.asyncio
async def test_injected_note_is_model_visible_through_prompt_messages() -> None:
    from app.services.agent_runtime.model_step_service import _prompt_messages

    run_id = str(uuid.uuid4())
    result = ListRetrievalResult(
        sections=(
            RetrievedListSection(
                title="app 优化清单",
                items=(
                    ListItem(number=1, title="输入精度截断", description="Calculator.kt:204 用 Float"),
                    ListItem(number=3, title="无缓存解析", description="每次重读文件"),
                ),
                total_count=2,
            ),
        ),
        total_lists=1,
    )
    fake = _FakeRetriever(result)
    state = _state(run_id=run_id, goal="做 1、2、3、5")
    builder = _builder(fake)
    built = await builder.build(state, _context(state))

    # The note must survive the real model-message assembly (which re-applies
    # model_visible_thread_messages and re-extracts the final control message).
    messages = _prompt_messages(
        static_prompt="Static",
        dynamic_prompt="Dynamic",
        build=built,
    )
    user_contents = [
        content for message in messages if message.role == "user" if isinstance((content := message.content), str)
    ]
    assert any(
        "历史上下文（非当前任务）：此前已确认、尚未完结的清单：" in content
        and "1. 输入精度截断" in content
        and "3. 无缓存解析" in content
        and "Calculator.kt:204 用 Float" not in content
        for content in user_contents
    )
