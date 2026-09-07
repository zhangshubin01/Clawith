"""Heartbeat terminal checkpoint activity projection tests."""

from __future__ import annotations

from collections import deque
from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import uuid

import pytest

from app.models.activity_log import AgentActivityLog
from app.models.agent_run import AgentRun
from app.models.notification import Notification
from app.services.agent_runtime.command_worker import (
    CheckpointObservation,
    RuntimeRunRecord,
)
from app.services.agent_runtime.heartbeat_completion import (
    HeartbeatRuntimeCompletionError,
    HeartbeatRuntimeCompletionHandler,
    HeartbeatSeedFocusHandler,
    _classify_seed_kinds,
    _extract_seed_lines,
)
from app.services.focus_service import slugify_focus_key
from app.services.agent_runtime.state import (
    RunInputSnapshots,
    RunRegistrySnapshot,
    RuntimeGraphState,
)


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object:
        return self.value


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Session:
    def __init__(self, *results: object) -> None:
        self.results = deque(results)
        self.added: list[object] = []
        self.flushes = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def begin(self) -> _Transaction:
        return _Transaction()

    async def execute(self, _statement) -> _ScalarResult:
        return _ScalarResult(self.results.popleft())

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        self.flushes += 1


class _SessionFactory:
    def __init__(self, *sessions: _Session) -> None:
        self.sessions = deque(sessions)
        self.calls = 0

    def __call__(self) -> _Session:
        self.calls += 1
        return self.sessions.popleft()


def _records(
    *,
    source_type: str = "heartbeat",
    status: str = "completed",
    answer: str | None = "Reviewed two notifications",
    mode: str = "heartbeat",
) -> tuple[RuntimeRunRecord, CheckpointObservation, AgentRun]:
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    run_id = uuid.uuid4()
    registry = RunRegistrySnapshot(
        tenant_id=str(tenant_id),
        run_id=str(run_id),
        goal="review the environment",
        run_kind="background",
        source_type=source_type,
        model_id=str(uuid.uuid4()),
        graph_name="runtime_graph",
        graph_version="v1",
        agent_id=str(agent_id),
    )
    run = RuntimeRunRecord(
        tenant_id=tenant_id,
        run_id=run_id,
        thread_id=str(run_id),
        runtime_type="langgraph",
        goal=registry.goal,
        run_kind=registry.run_kind,
        source_type=registry.source_type,
        model_id=registry.model_id,
        graph_name=registry.graph_name,
        graph_version=registry.graph_version,
        agent_id=registry.agent_id,
        session_id=registry.session_id,
        system_role=registry.system_role,
        parent_run_id=registry.parent_run_id,
        root_run_id=registry.root_run_id,
    )
    initial_input: dict = {"background_mode": mode}
    if mode == "schedule":
        schedule_id = uuid.uuid4()
        initial_input.update(
            {
                "schedule_id": str(schedule_id),
                "schedule_instruction": "Review the weekly pipeline",
            }
        )
        source_id = str(schedule_id)
        source_execution_id = f"schedule:{schedule_id}:{uuid.uuid4()}"
    elif mode == "oneshot":
        initial_input.update(
            {
                "triggered_by_user_id": str(uuid.uuid4()),
                "agent_name": "OKR Agent",
            }
        )
        source_id = str(agent_id)
        source_execution_id = f"oneshot:{agent_id}:{uuid.uuid4()}"
    else:
        source_id = str(agent_id)
        source_execution_id = (
            f"heartbeat:{agent_id}:2026-07-13T18:45:00.000000Z"
        )
    state: RuntimeGraphState = {
        "registry": registry,
        "snapshots": RunInputSnapshots(
            session_context={},
            session_context_version=0,
            recent_session_messages=(),
            related_run_summaries=(),
            initial_input=initial_input,
        ),
        "lifecycle": {
            "status": status,
            "next_route": "terminal",
            "final_answer": answer,
            "error": (
                {"code": "model_call_failed"} if status == "failed" else None
            ),
        },  # type: ignore[typeddict-item]
    }
    checkpoint = CheckpointObservation(
        checkpoint_id="checkpoint-terminal",
        state=state,
    )
    stored_run = AgentRun(
        id=run_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        source_type="heartbeat",
        source_id=source_id,
        source_execution_id=source_execution_id,
        goal="review the environment",
        run_kind="background",
        model_id=uuid.uuid4(),
        runtime_type="langgraph",
        runtime_thread_id=str(run_id),
        graph_name="runtime_graph",
        graph_version="v1",
        lane_held=False,
        delivery_status="not_required",
    )
    return run, checkpoint, stored_run


@pytest.mark.asyncio
async def test_useful_heartbeat_result_creates_one_deterministic_activity() -> None:
    run, checkpoint, stored_run = _records()
    db = _Session(stored_run, None)
    created_at = datetime(2026, 7, 13, 19, 0, tzinfo=UTC)
    handler = HeartbeatRuntimeCompletionHandler(
        session_factory=_SessionFactory(db),  # type: ignore[arg-type]
        clock=lambda: created_at,
    )

    await handler.handle(run=run, checkpoint=checkpoint)

    assert db.flushes == 1
    assert len(db.added) == 1
    activity = db.added[0]
    assert isinstance(activity, AgentActivityLog)
    assert activity.id == uuid.uuid5(
        run.run_id,
        "heartbeat-terminal:checkpoint-terminal",
    )
    assert activity.agent_id == stored_run.agent_id
    assert activity.action_type == "heartbeat"
    assert activity.summary == "Heartbeat: Reviewed two notifications"
    assert activity.related_id == run.run_id
    assert activity.created_at == created_at


@pytest.mark.asyncio
async def test_existing_activity_receipt_makes_reconciliation_idempotent() -> None:
    run, checkpoint, stored_run = _records()
    receipt_id = uuid.uuid5(
        run.run_id,
        "heartbeat-terminal:checkpoint-terminal",
    )
    db = _Session(stored_run, receipt_id)
    handler = HeartbeatRuntimeCompletionHandler(
        session_factory=_SessionFactory(db),  # type: ignore[arg-type]
    )

    await handler.handle(run=run, checkpoint=checkpoint)

    assert db.added == []
    assert db.flushes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "answer"),
    [
        ("completed", "HEARTBEAT OK"),
        ("failed", None),
        ("cancelled", None),
    ],
)
async def test_noop_heartbeat_result_does_not_open_a_session(
    status: str,
    answer: str | None,
) -> None:
    run, checkpoint, _ = _records(status=status, answer=answer)
    factory = _SessionFactory()
    handler = HeartbeatRuntimeCompletionHandler(
        session_factory=factory,  # type: ignore[arg-type]
    )

    await handler.handle(run=run, checkpoint=checkpoint)

    assert factory.calls == 0


@pytest.mark.asyncio
async def test_completed_heartbeat_rejects_mismatched_source_identity() -> None:
    run, checkpoint, stored_run = _records()
    stored_run.source_id = str(uuid.uuid4())
    handler = HeartbeatRuntimeCompletionHandler(
        session_factory=_SessionFactory(_Session(stored_run)),  # type: ignore[arg-type]
    )

    with pytest.raises(HeartbeatRuntimeCompletionError) as raised:
        await handler.handle(run=run, checkpoint=checkpoint)

    assert raised.value.code == "heartbeat_source_mismatch"


@pytest.mark.asyncio
async def test_schedule_result_creates_schedule_activity_from_checkpoint_input() -> None:
    run, checkpoint, stored_run = _records(mode="schedule")
    db = _Session(stored_run, None)
    handler = HeartbeatRuntimeCompletionHandler(
        session_factory=_SessionFactory(db),  # type: ignore[arg-type]
    )

    await handler.handle(run=run, checkpoint=checkpoint)

    activity = db.added[0]
    assert isinstance(activity, AgentActivityLog)
    assert activity.id == uuid.uuid5(
        run.run_id,
        "schedule-terminal:checkpoint-terminal",
    )
    assert activity.action_type == "schedule_run"
    assert activity.summary == "定时任务执行: Review the weekly pipeline"
    assert activity.related_id == uuid.UUID(stored_run.source_id)


@pytest.mark.asyncio
async def test_failed_oneshot_notifies_the_triggering_user_exactly_once() -> None:
    run, checkpoint, stored_run = _records(
        mode="oneshot",
        status="failed",
        answer=None,
    )
    db = _Session(stored_run, None)
    handler = HeartbeatRuntimeCompletionHandler(
        session_factory=_SessionFactory(db),  # type: ignore[arg-type]
    )

    await handler.handle(run=run, checkpoint=checkpoint)

    notification = db.added[0]
    assert isinstance(notification, Notification)
    assert notification.id == uuid.uuid5(
        run.run_id,
        "oneshot-terminal:checkpoint-terminal",
    )
    assert notification.user_id == uuid.UUID(
        checkpoint.state["snapshots"].initial_input["triggered_by_user_id"]
    )
    assert notification.title == "OKR Agent task failed"
    assert notification.body == "任务执行未完成（model_call_failed）"


# ---------------------------------------------------------------------------
# Seed → Focus projection (C channel, platform-deterministic write side)
# ---------------------------------------------------------------------------


def _seed_reflections(*seed_lines: str, include_section: bool = True) -> str:
    parts = ["# Reflections Journal", "## Insights & Discoveries", "- 已验证的事实"]
    if include_section:
        parts.extend(
            [
                "## Next Cycle Seeds",
                "_What to explore in your next heartbeat. Keep this section short and focused._",
                "",
                *seed_lines,
            ]
        )
    parts.extend(["## Open Questions", "- 旧问题"])
    return "\n".join(parts)


def _focus_storage(content: str | None) -> SimpleNamespace:
    storage = SimpleNamespace()
    storage.exists = AsyncMock(return_value=content is not None)
    storage.is_file = AsyncMock(return_value=content is not None)
    if content is None:

        async def read_text(*_args, **_kwargs):
            raise OSError("reflections not found")

        storage.read_text = read_text
    else:
        storage.read_text = AsyncMock(return_value=content)
    return storage


def test_extract_seed_lines_returns_entries_and_skips_helper_lines() -> None:
    seeds = _extract_seed_lines(_seed_reflections("- 种子 A", "- 种子 B"))

    assert seeds == ["种子 A", "种子 B"]


def test_extract_seed_lines_missing_section_is_no_signal() -> None:
    assert _extract_seed_lines(_seed_reflections(include_section=False)) is None
    assert _extract_seed_lines("") is None
    assert _extract_seed_lines("   ") is None


def test_extract_seed_lines_empty_section_is_deliberate_clear() -> None:
    content = "## Next Cycle Seeds\n_提示行_\n## Open Questions\n- 问题"

    assert _extract_seed_lines(content) == []


@pytest.mark.asyncio
async def test_completed_heartbeat_projects_seeds_into_focus() -> None:
    run, checkpoint, _ = _records(answer="HEARTBEAT OK")
    upsert = AsyncMock(return_value={"key": "seed"})
    complete = AsyncMock(return_value={"key": "stale"})
    list_items = AsyncMock(
        return_value=[
            {"key": "stale_key", "source": "heartbeat", "status": "in_progress"},
            {"key": "user_key", "source": "user", "status": "in_progress"},
        ]
    )
    storage = _focus_storage(_seed_reflections("- 种子 A", "- 种子 B"))

    with (
        patch(
            "app.services.agent_runtime.heartbeat_completion.get_storage_backend",
            return_value=storage,
        ),
        patch(
            "app.services.agent_runtime.heartbeat_completion.upsert_focus_item",
            upsert,
        ),
        patch(
            "app.services.agent_runtime.heartbeat_completion.complete_focus_item",
            complete,
        ),
        patch(
            "app.services.agent_runtime.heartbeat_completion.list_focus_items",
            list_items,
        ),
    ):
        handler = HeartbeatSeedFocusHandler(session_factory=_SessionFactory())  # type: ignore[arg-type]
        await handler.handle(run=run, checkpoint=checkpoint)

    assert upsert.await_count == 2
    upserted = {call.kwargs["key"] for call in upsert.await_args_list}
    assert upserted == {slugify_focus_key("种子 A"), slugify_focus_key("种子 B")}
    for call in upsert.await_args_list:
        assert call.args[0] == uuid.UUID(run.agent_id)
        assert call.kwargs["source"] == "heartbeat"
        assert call.kwargs["status"] == "in_progress"
        assert call.kwargs["kind"] == "normal"
        assert call.kwargs["description"] in {"种子 A", "种子 B"}
    # 退役只打 heartbeat 源且已不在 seeds 里的项；user 源不受影响。
    complete.assert_awaited_once_with(uuid.UUID(run.agent_id), key="stale_key")


@pytest.mark.asyncio
async def test_missing_seeds_section_projects_nothing() -> None:
    run, checkpoint, _ = _records()
    upsert = AsyncMock()
    complete = AsyncMock()
    list_items = AsyncMock()
    storage = _focus_storage(_seed_reflections(include_section=False))

    with (
        patch(
            "app.services.agent_runtime.heartbeat_completion.get_storage_backend",
            return_value=storage,
        ),
        patch(
            "app.services.agent_runtime.heartbeat_completion.upsert_focus_item",
            upsert,
        ),
        patch(
            "app.services.agent_runtime.heartbeat_completion.complete_focus_item",
            complete,
        ),
        patch(
            "app.services.agent_runtime.heartbeat_completion.list_focus_items",
            list_items,
        ),
    ):
        handler = HeartbeatSeedFocusHandler(session_factory=_SessionFactory())  # type: ignore[arg-type]
        await handler.handle(run=run, checkpoint=checkpoint)

    upsert.assert_not_awaited()
    complete.assert_not_awaited()
    list_items.assert_not_awaited()


@pytest.mark.asyncio
async def test_unreadable_reflections_projects_nothing() -> None:
    run, checkpoint, _ = _records()
    upsert = AsyncMock()
    complete = AsyncMock()
    storage = _focus_storage(None)

    with (
        patch(
            "app.services.agent_runtime.heartbeat_completion.get_storage_backend",
            return_value=storage,
        ),
        patch(
            "app.services.agent_runtime.heartbeat_completion.upsert_focus_item",
            upsert,
        ),
        patch(
            "app.services.agent_runtime.heartbeat_completion.complete_focus_item",
            complete,
        ),
        patch(
            "app.services.agent_runtime.heartbeat_completion.list_focus_items",
            AsyncMock(),
        ),
    ):
        handler = HeartbeatSeedFocusHandler(session_factory=_SessionFactory())  # type: ignore[arg-type]
        await handler.handle(run=run, checkpoint=checkpoint)

    upsert.assert_not_awaited()
    complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_seeds_section_retires_all_heartbeat_items() -> None:
    run, checkpoint, _ = _records()
    upsert = AsyncMock()
    complete = AsyncMock(return_value={"key": "stale"})
    list_items = AsyncMock(
        return_value=[
            {"key": "stale_a", "source": "heartbeat", "status": "in_progress"},
            {"key": "stale_b", "source": "heartbeat", "status": "in_progress"},
            {"key": "user_key", "source": "user", "status": "in_progress"},
        ]
    )
    storage = _focus_storage(_seed_reflections())

    with (
        patch(
            "app.services.agent_runtime.heartbeat_completion.get_storage_backend",
            return_value=storage,
        ),
        patch(
            "app.services.agent_runtime.heartbeat_completion.upsert_focus_item",
            upsert,
        ),
        patch(
            "app.services.agent_runtime.heartbeat_completion.complete_focus_item",
            complete,
        ),
        patch(
            "app.services.agent_runtime.heartbeat_completion.list_focus_items",
            list_items,
        ),
    ):
        handler = HeartbeatSeedFocusHandler(session_factory=_SessionFactory())  # type: ignore[arg-type]
        await handler.handle(run=run, checkpoint=checkpoint)

    upsert.assert_not_awaited()
    assert complete.await_count == 2
    completed = {call.kwargs["key"] for call in complete.await_args_list}
    assert completed == {"stale_a", "stale_b"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_type", "status"),
    [
        ("chat", "completed"),
        ("heartbeat", "failed"),
        ("heartbeat", "cancelled"),
    ],
)
async def test_non_completed_heartbeat_never_touches_storage(
    source_type: str,
    status: str,
) -> None:
    run, checkpoint, _ = _records(source_type=source_type, status=status)
    get_storage = AsyncMock()

    with patch(
        "app.services.agent_runtime.heartbeat_completion.get_storage_backend",
        get_storage,
    ):
        handler = HeartbeatSeedFocusHandler(session_factory=_SessionFactory())  # type: ignore[arg-type]
        await handler.handle(run=run, checkpoint=checkpoint)

    get_storage.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_agent_identity_projects_nothing_without_raising() -> None:
    run, checkpoint, _ = _records()
    run = replace(run, agent_id=None)
    get_storage = AsyncMock()

    with patch(
        "app.services.agent_runtime.heartbeat_completion.get_storage_backend",
        get_storage,
    ):
        handler = HeartbeatSeedFocusHandler(session_factory=_SessionFactory())  # type: ignore[arg-type]
        await handler.handle(run=run, checkpoint=checkpoint)

    get_storage.assert_not_awaited()


# ---------------------------------------------------------------------------
# Seed classification gate (P1): task vs learning, monotonic completed
# ---------------------------------------------------------------------------


def _classification_context(handler, *, kinds, items, content):
    upsert = AsyncMock(return_value={"key": "seed"})
    complete = AsyncMock(return_value={"key": "stale"})
    list_items = AsyncMock(return_value=items)
    storage = _focus_storage(content)
    patches = [
        patch(
            "app.services.agent_runtime.heartbeat_completion.get_storage_backend",
            return_value=storage,
        ),
        patch(
            "app.services.agent_runtime.heartbeat_completion.upsert_focus_item",
            upsert,
        ),
        patch(
            "app.services.agent_runtime.heartbeat_completion.complete_focus_item",
            complete,
        ),
        patch(
            "app.services.agent_runtime.heartbeat_completion.list_focus_items",
            list_items,
        ),
        patch.object(
            handler,
            "_classifier_model",
            AsyncMock(return_value=SimpleNamespace()),
        ),
        patch(
            "app.services.agent_runtime.heartbeat_completion._classify_seed_kinds",
            AsyncMock(return_value=kinds),
        ),
    ]
    return upsert, complete, list_items, patches


@pytest.mark.asyncio
async def test_learning_seed_projects_as_completed_and_task_stays_active() -> None:
    run, checkpoint, _ = _records()
    handler = HeartbeatSeedFocusHandler(session_factory=_SessionFactory())  # type: ignore[arg-type]
    upsert, complete, list_items, patches = _classification_context(
        handler,
        kinds=["learning", "task"],
        items=[],
        content=_seed_reflections("- ✅ 已验证：探针", "- 下周期动作"),
    )

    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        await handler.handle(run=run, checkpoint=checkpoint)

    statuses = {call.kwargs["key"]: call.kwargs["status"] for call in upsert.await_args_list}
    assert statuses[slugify_focus_key("✅ 已验证：探针")] == "completed"
    assert statuses[slugify_focus_key("下周期动作")] == "in_progress"
    # 结论与动作都在 Seeds 里，故无 stale 退休。
    complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_classification_uncertainty_falls_back_to_in_progress() -> None:
    run, checkpoint, _ = _records()
    handler = HeartbeatSeedFocusHandler(session_factory=_SessionFactory())  # type: ignore[arg-type]
    upsert, _, _, patches = _classification_context(
        handler,
        kinds=None,
        items=[],
        content=_seed_reflections("- 种子 A"),
    )

    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        await handler.handle(run=run, checkpoint=checkpoint)

    assert upsert.await_count == 1
    assert upsert.await_args.kwargs["status"] == "in_progress"


@pytest.mark.asyncio
async def test_completed_heartbeat_item_is_not_reopened_by_reclassification() -> None:
    run, checkpoint, _ = _records()
    handler = HeartbeatSeedFocusHandler(session_factory=_SessionFactory())  # type: ignore[arg-type]
    seed_text = "已完成的探针动作"
    done_key = slugify_focus_key(seed_text)
    upsert, _, _, patches = _classification_context(
        handler,
        kinds=["task"],  # LLM 反悔：把已完成的项重新判为 task
        items=[{"key": done_key, "source": "heartbeat", "status": "completed"}],
        content=_seed_reflections(f"- {seed_text}"),
    )

    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        await handler.handle(run=run, checkpoint=checkpoint)

    # 单调护栏：已完成项即使被判 task 也不回退。
    assert upsert.await_count == 1
    assert upsert.await_args.kwargs["status"] == "completed"


@pytest.mark.asyncio
async def test_classify_seed_kinds_parses_structured_output() -> None:
    model = SimpleNamespace()
    step = SimpleNamespace(
        content='[{"index": 0, "kind": "learning"}, {"index": 1, "kind": "task"}]'
    )
    with patch(
        "app.services.agent_runtime.heartbeat_completion.complete_llm_once",
        AsyncMock(return_value=step),
    ) as complete:
        kinds = await _classify_seed_kinds(model, ["结论 A", "动作 B"], uuid.uuid4())

    complete.assert_awaited_once()
    assert kinds == ["learning", "task"]


@pytest.mark.asyncio
async def test_classify_seed_kinds_strips_code_fence() -> None:
    model = SimpleNamespace()
    step = SimpleNamespace(
        content='```json\n[{"index": 0, "kind": "learning"}]\n```'
    )
    with patch(
        "app.services.agent_runtime.heartbeat_completion.complete_llm_once",
        AsyncMock(return_value=step),
    ):
        kinds = await _classify_seed_kinds(model, ["结论 A"], uuid.uuid4())

    assert kinds == ["learning"]


@pytest.mark.asyncio
async def test_classify_seed_kinds_unparseable_returns_none() -> None:
    model = SimpleNamespace()
    step = SimpleNamespace(content="not json at all")
    with patch(
        "app.services.agent_runtime.heartbeat_completion.complete_llm_once",
        AsyncMock(return_value=step),
    ):
        kinds = await _classify_seed_kinds(model, ["结论 A"], uuid.uuid4())

    assert kinds is None


@pytest.mark.asyncio
async def test_classify_seed_kinds_call_failure_returns_none() -> None:
    model = SimpleNamespace()
    with patch(
        "app.services.agent_runtime.heartbeat_completion.complete_llm_once",
        AsyncMock(side_effect=RuntimeError("boom")),
    ):
        kinds = await _classify_seed_kinds(model, ["结论 A"], uuid.uuid4())

    assert kinds is None
