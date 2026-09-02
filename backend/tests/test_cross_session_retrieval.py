"""Cross-session automatic list retrieval (R3) tests.

Covers the detection signal (bare-number and historical-pronoun), the
retrieval across the current + recent sessions, the past-tense injection
framing, and the strict no-op invariant on a miss.
"""

from __future__ import annotations

import uuid

import pytest

from app.services.agent_runtime import context_builder
from app.services.agent_runtime.cross_session_retrieval import (
    CrossSessionListRetriever,
    ListReferenceSignal,
    ListRetrievalResult,
    detect_list_reference,
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


# ---------------------------------------------------------------- detection


def test_detect_number_sequence() -> None:
    signal = detect_list_reference("做 1、2、3、5")
    assert signal is not None
    assert signal.numbers == (1, 2, 3, 5)
    assert signal.historical is False


def test_detect_space_separated_numbers() -> None:
    signal = detect_list_reference("完成 3 4 5")
    assert signal is not None
    assert signal.numbers == (3, 4, 5)


def test_detect_fullwidth_digits_are_normalized() -> None:
    signal = detect_list_reference("做 １、２、３")
    assert signal is not None
    assert signal.numbers == (1, 2, 3)


def test_detect_prefixed_numbers_and_extended_verbs() -> None:
    # Focus labels (P2) and shorthand (#4 / N3 / p2) after the extended verb
    # set (执行/处理) — the 执行P2 shape observed in production on 2026-09-02.
    for message, expected in (
        ("执行P2", (2,)),
        ("执行 P2", (2,)),
        ("处理N3", (3,)),
        ("完成#4", (4,)),
        ("改 p2、N3、＃4", (2, 3, 4)),
        ("执行 3 5", (3, 5)),
    ):
        signal = detect_list_reference(message)
        assert signal is not None, message
        assert signal.numbers == expected, message
        assert signal.historical is False


def test_detect_historical_pronoun() -> None:
    signal = detect_list_reference("上一轮的清单还在吗")
    assert signal is not None
    assert signal.numbers == ()
    assert signal.historical is True


def test_detect_historical_pronoun_variants() -> None:
    for message in ("上次的优化项做完了吗", "上回那个编号呢", "上一次的列表"):
        signal = detect_list_reference(message)
        assert signal is not None, message
        assert signal.historical is True
        assert signal.numbers == ()


def test_detect_historical_pronoun_and_noun_sets_per_plan() -> None:
    # Word sets from the plan R3-1: pronouns 上一轮/上次/之前/刚才 and nouns
    # 清单/结论/优化/方案.
    for message in (
        "之前的方案是什么",
        "刚才的结论还在吗",
        "上次的优化还在吗",
        "之前的清单呢",
    ):
        signal = detect_list_reference(message)
        assert signal is not None, message
        assert signal.historical is True
        assert signal.numbers == ()


def test_detect_pronoun_with_trailing_numbers_is_historical() -> None:
    # The numbered rule requires the verb immediately before the digits; when a
    # pronoun intervenes the message is treated as a historical reference and
    # the whole list is injected (a coarser but still correct match).
    signal = detect_list_reference("完成上一轮清单的 3、4、5")
    assert signal is not None
    assert signal.numbers == ()
    assert signal.historical is True


def test_detect_no_signal() -> None:
    assert detect_list_reference("优化 app 性能") is None
    assert detect_list_reference("帮我重新编译一下项目") is None
    assert detect_list_reference("") is None
    assert detect_list_reference("给我 3 个测试用例") is None  # no action verb


def test_detect_bare_number_false_positive_is_still_a_signal() -> None:
    # D5-2: a bare-number phrase like "做 1、2、3 个测试用例" IS detected as a
    # signal; the false-positive guard is the retrieval no-op, not detection.
    signal = detect_list_reference("做 1、2、3 个测试用例")
    assert signal is not None
    assert signal.numbers == (1, 2, 3)


def test_detect_non_string_returns_none() -> None:
    assert detect_list_reference(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------- rendering


def test_render_note_is_past_tense_and_non_imperative() -> None:
    result = ListRetrievalResult(
        title="app 优化清单",
        items=(
            ListItem(number=1, title="输入精度截断", description="Calculator.kt:204 用 Float"),
            ListItem(number=3, title="无缓存解析", description="每次重读文件"),
        ),
    )
    note = render_retrieval_note(result, current_run_id="run-1")
    assert note["role"] == "user"
    assert note["runtime_input"] == "cross_session_list"
    assert note["id"] == "cross-session-list:run-1"
    content = note["content"]
    assert content.startswith("历史上下文（非当前任务）")
    assert "此前会话曾产出清单" in content
    assert "1. 输入精度截断 — Calculator.kt:204 用 Float" in content
    assert "3. 无缓存解析 — 每次重读文件" in content
    # Never an imperative or goal-style phrasing (direct-chat-run-boundary-fix).
    assert "目标：" not in content
    assert not content.lstrip().startswith("请")


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
    ) -> None:
        self._snapshot = snapshot_open_items
        self._recent = recent if recent is not None else []
        self.load_snapshot_calls: list = []
        self.recent_calls: list = []

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
async def test_retrieve_same_session_numbered_hit() -> None:
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
        signal=ListReferenceSignal(numbers=(1, 3, 5), historical=False),
    )

    assert result is not None
    assert result.title == "app 优化清单"
    assert [item.number for item in result.items] == [1, 3, 5]
    assert result.items[0].title == "输入精度截断"
    assert service.load_snapshot_calls == [(tenant_id, session_id)]
    assert service.recent_calls == [(tenant_id, agent_id, user_id, session_id, 5)]


@pytest.mark.asyncio
async def test_retrieve_cross_session_hit() -> None:
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
        signal=ListReferenceSignal(numbers=(1, 3, 5), historical=False),
    )

    assert result is not None
    assert [item.number for item in result.items] == [1, 3, 5]
    # The prior session's pointer was found; the current session was excluded.
    assert service.recent_calls == [
        (tenant_id, agent_id, user_id, current_session_id, 5)
    ]


@pytest.mark.asyncio
async def test_retrieve_historical_injects_all_items() -> None:
    list_id = uuid.uuid4()
    storage = _Storage(_list_file_content(list_id))
    service = _ContextService(snapshot_open_items=(_pointer(list_id),))
    retriever = _retriever(storage=storage, context_service=service)

    result = await retriever.retrieve(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        project=None,
        signal=ListReferenceSignal(numbers=(), historical=True),
    )

    assert result is not None
    assert [item.number for item in result.items] == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_retrieve_project_filter_narrows() -> None:
    list_id = uuid.uuid4()
    storage = _Storage(_list_file_content(list_id, project="mydome1"))
    # Pointer belongs to a different project: must not match.
    service = _ContextService(
        snapshot_open_items=(_pointer(list_id, project="other-project"),)
    )
    retriever = _retriever(storage=storage, context_service=service)

    result = await retriever.retrieve(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        project="mydome1",
        signal=ListReferenceSignal(numbers=(1,), historical=False),
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
        signal=ListReferenceSignal(numbers=(2,), historical=False),
    )

    assert result is not None
    assert result.title == "app 优化清单"
    assert [item.number for item in result.items] == [2]


@pytest.mark.asyncio
async def test_retrieve_exact_project_preferred_over_wildcard() -> None:
    # With both an exact-project pointer and a session-level (project=None)
    # pointer available, the exact match must win.
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
        signal=ListReferenceSignal(numbers=(1,), historical=False),
    )

    assert result is not None
    assert result.title == "mydome1 清单"


@pytest.mark.asyncio
async def test_retrieve_miss_when_numbers_absent() -> None:
    list_id = uuid.uuid4()
    storage = _Storage(_list_file_content(list_id))
    service = _ContextService(snapshot_open_items=(_pointer(list_id),))
    retriever = _retriever(storage=storage, context_service=service)

    result = await retriever.retrieve(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        project="mydome1",
        signal=ListReferenceSignal(numbers=(99,), historical=False),
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
        signal=ListReferenceSignal(numbers=(1, 2), historical=False),
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
        signal=ListReferenceSignal(numbers=(1,), historical=False),
    )

    assert result is None


@pytest.mark.asyncio
async def test_retrieve_prefers_most_recent_list_without_project() -> None:
    newer_list_id = uuid.uuid4()
    older_list_id = uuid.uuid4()
    content = (
        _list_file_content(older_list_id, title="旧清单")
        + _list_file_content(newer_list_id, title="新清单")
    )
    storage = _Storage(content)
    # No current-session pointer; two prior sessions, newest first. Without a
    # project filter the most recent session's list wins.
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
        signal=ListReferenceSignal(numbers=(1,), historical=False),
    )

    assert result is not None
    assert result.title == "新清单"


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


def _builder(retriever: _FakeRetriever | None) -> context_builder.ContextBuilder:
    return context_builder.ContextBuilder(
        session_context_service=object(),  # type: ignore[arg-type]
        cross_session_retriever=retriever,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_build_injects_retrieval_note_on_hit() -> None:
    run_id = str(uuid.uuid4())
    result = ListRetrievalResult(
        title="app 优化清单",
        items=(
            ListItem(number=1, title="输入精度截断", description="Calculator.kt:204 用 Float"),
            ListItem(number=3, title="无缓存解析", description="每次重读文件"),
        ),
    )
    fake = _FakeRetriever(result)
    state = _state(run_id=run_id, goal="做 1、2、3、5")
    builder = _builder(fake)

    built = await builder.build(state, _context(state))

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["signal"].numbers == (1, 2, 3, 5)
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
async def test_build_noop_when_goal_has_no_signal() -> None:
    run_id = str(uuid.uuid4())
    fake = _FakeRetriever(None)
    state = _state(run_id=run_id, goal="优化 app 性能")
    builder = _builder(fake)

    built = await builder.build(state, _context(state))

    # Detection short-circuits: the retriever is never invoked.
    assert fake.calls == []
    assert [m["id"] for m in built.recent_thread_messages] == [f"current-input-{run_id}"]


@pytest.mark.asyncio
async def test_build_noop_without_retriever_configured() -> None:
    run_id = str(uuid.uuid4())
    state = _state(run_id=run_id, goal="做 1、2、3、5")
    builder = _builder(None)

    built = await builder.build(state, _context(state))

    assert [m["id"] for m in built.recent_thread_messages] == [f"current-input-{run_id}"]


@pytest.mark.asyncio
async def test_injected_note_is_model_visible_through_prompt_messages() -> None:
    from app.services.agent_runtime.model_step_service import _prompt_messages

    run_id = str(uuid.uuid4())
    result = ListRetrievalResult(
        title="app 优化清单",
        items=(
            ListItem(number=1, title="输入精度截断", description="Calculator.kt:204 用 Float"),
            ListItem(number=3, title="无缓存解析", description="每次重读文件"),
        ),
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
        content for message in messages if message.role == "user"
        if isinstance((content := message.content), str)
    ]
    assert any(
        "历史上下文（非当前任务）" in content
        and "1. 输入精度截断 — Calculator.kt:204 用 Float" in content
        and "3. 无缓存解析 — 每次重读文件" in content
        for content in user_contents
    )
