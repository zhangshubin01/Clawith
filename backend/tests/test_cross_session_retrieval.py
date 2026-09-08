"""Cross-session open-list item injection (R3) tests.

The retriever now reads ``agent_list_items`` directly by scope (no pointer
file, no ``memory/清单.md``), so the tests cover: the bounded numbered-index
rendering (item count + character cap, past-tense non-imperative framing), the
scope resolution (project → session fallback → no-op), and the build wiring
that injects the note unconditionally.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence
import uuid

import pytest

from app.services.agent_runtime import context_builder
from app.services.agent_runtime import cross_session_retrieval as csr
from app.services.agent_runtime.cross_session_retrieval import (
    MAX_INJECTED_CHARS,
    MAX_INJECTED_ITEMS,
    CrossSessionListRetriever,
    InjectedListItem,
    ListRetrievalResult,
    render_retrieval_note,
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
    result = ListRetrievalResult(
        items=(
            InjectedListItem(sort_order=92, title="输入精度截断", key="input-precision"),
            InjectedListItem(sort_order=93, title="超大指数上限", key="power-limit"),
        ),
        total_active=2,
    )
    note = render_retrieval_note(result, current_run_id="run-1")
    assert note["role"] == "user"
    assert note["runtime_input"] == "cross_session_list"
    assert note["id"] == "cross-session-list:run-1"
    content = note["content"]
    assert content.startswith("历史上下文（非当前任务）：此前已确认、尚未完结的清单：")
    assert "92. 输入精度截断 (input-precision)" in content
    assert "93. 超大指数上限 (power-limit)" in content
    # No present-tense "当前未决" wording; never imperative or goal-style.
    assert "当前未决" not in content
    assert "目标：" not in content
    assert not content.lstrip().startswith("请")


def test_render_note_truncation_marker() -> None:
    result = ListRetrievalResult(
        items=(InjectedListItem(sort_order=1, title="输入精度截断", key="a"),),
        total_active=25,
    )
    content = render_retrieval_note(result, current_run_id="run-1")["content"]
    assert "（仅列出前 1 项；完整内容见 list_list_items）" in content


def test_render_note_character_cap_truncates() -> None:
    result = ListRetrievalResult(
        items=tuple(
            InjectedListItem(sort_order=index, title="超长条目标题" * 50, key=f"k{index}")
            for index in range(1, 6)
        ),
        total_active=5,
    )
    content = render_retrieval_note(result, current_run_id="run-1")["content"]
    assert content.endswith("...(truncated)")
    assert len(content) == MAX_INJECTED_CHARS + len("...(truncated)")


# ---------------------------------------------------------------- retriever fakes


@dataclass(frozen=True)
class _FakeItem:
    sort_order: int
    title: str | None
    key: str


class _FakeDao:
    def __init__(self, items: Sequence[_FakeItem] | None = None) -> None:
        self.items: list[_FakeItem] = list(items or [])
        self.calls: list[dict] = []

    async def list_by_project(self, *, agent_id, project, include_completed):
        self.calls.append({"agent_id": agent_id, "project": project, "include_completed": include_completed})
        return list(self.items)


def _retriever(*, max_injected_items: int = MAX_INJECTED_ITEMS) -> CrossSessionListRetriever:
    return CrossSessionListRetriever(max_injected_items=max_injected_items)


# ---------------------------------------------------------------- retriever


@pytest.mark.asyncio
async def test_retrieve_injects_active_items(monkeypatch: pytest.MonkeyPatch) -> None:
    dao = _FakeDao(
        [
            _FakeItem(sort_order=92, title="GitLab CI", key="gitlab-ci"),
            _FakeItem(sort_order=93, title="lint", key="lint"),
        ]
    )
    monkeypatch.setattr(csr, "list_dao", dao)
    agent_id = uuid.uuid4()
    result = await _retriever().retrieve(
        tenant_id=uuid.uuid4(),
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        project="mydome1",
    )
    assert result is not None
    assert result.total_active == 2
    assert [(item.sort_order, item.key) for item in result.items] == [(92, "gitlab-ci"), (93, "lint")]
    assert dao.calls == [
        {"agent_id": agent_id, "project": "mydome1", "include_completed": False}
    ]


@pytest.mark.asyncio
async def test_retrieve_title_falls_back_to_key(monkeypatch: pytest.MonkeyPatch) -> None:
    dao = _FakeDao([_FakeItem(sort_order=1, title=None, key="no-title")])
    monkeypatch.setattr(csr, "list_dao", dao)
    result = await _retriever().retrieve(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        project="p",
    )
    assert result is not None
    assert result.items[0].title == "no-title"


@pytest.mark.asyncio
async def test_retrieve_session_fallback_when_no_project(monkeypatch: pytest.MonkeyPatch) -> None:
    dao = _FakeDao([_FakeItem(sort_order=1, title="t", key="k")])
    monkeypatch.setattr(csr, "list_dao", dao)
    session_id = uuid.uuid4()
    result = await _retriever().retrieve(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id=session_id,
        project=None,
    )
    assert result is not None
    assert dao.calls[0]["project"] == f"session:{session_id}"


@pytest.mark.asyncio
async def test_retrieve_noop_without_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    dao = _FakeDao([_FakeItem(sort_order=1, title="t", key="k")])
    monkeypatch.setattr(csr, "list_dao", dao)
    result = await _retriever().retrieve(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=None,
        session_id=None,
        project=None,
    )
    assert result is None
    assert dao.calls == []


@pytest.mark.asyncio
async def test_retrieve_noop_when_no_items(monkeypatch: pytest.MonkeyPatch) -> None:
    dao = _FakeDao([])
    monkeypatch.setattr(csr, "list_dao", dao)
    result = await _retriever().retrieve(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        project="p",
    )
    assert result is None


@pytest.mark.asyncio
async def test_retrieve_caps_items_at_max_injected(monkeypatch: pytest.MonkeyPatch) -> None:
    dao = _FakeDao([_FakeItem(sort_order=index, title=f"条目{index}", key=f"k{index}") for index in range(1, 11)])
    monkeypatch.setattr(csr, "list_dao", dao)
    result = await _retriever().retrieve(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        project="p",
    )
    assert result is not None
    assert result.total_active == 10
    assert len(result.items) == MAX_INJECTED_ITEMS
    assert [item.sort_order for item in result.items] == [1, 2, 3, 4, 5]


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
        items=(
            InjectedListItem(sort_order=92, title="输入精度截断", key="input-precision"),
            InjectedListItem(sort_order=93, title="无缓存解析", key="no-cache"),
        ),
        total_active=2,
    )
    fake = _FakeRetriever(result)
    state = _state(run_id=run_id, goal="那执行 92→93")
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
async def test_injected_note_is_model_visible_through_prompt_messages() -> None:
    from app.services.agent_runtime.model_step_service import _prompt_messages

    run_id = str(uuid.uuid4())
    result = ListRetrievalResult(
        items=(
            InjectedListItem(sort_order=1, title="输入精度截断", key="input-precision"),
            InjectedListItem(sort_order=3, title="无缓存解析", key="no-cache"),
        ),
        total_active=2,
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
        and "1. 输入精度截断 (input-precision)" in content
        and "3. 无缓存解析 (no-cache)" in content
        for content in user_contents
    )
