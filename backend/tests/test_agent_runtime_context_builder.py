"""Focused immutable snapshot and Tool Pair Integrity tests for ContextBuilder."""

import inspect
import uuid

import pytest

from app.services.agent_runtime import context_builder
from app.services.agent_runtime.session_context_service import (
    SessionContextPack,
    SessionContextSnapshot,
)
from app.services.agent_runtime.state import (
    RunInputSnapshots,
    RunRegistrySnapshot,
    RuntimeGraphState,
)


class _SessionContextService:
    def __init__(self, pack: SessionContextPack):
        self.pack = pack
        self.calls = []

    async def load_context_pack(self, db, *, tenant_id, session_id):
        self.calls.append((db, tenant_id, session_id))
        return self.pack


def _snapshot(*, version: int = 3, summary: str = "session summary"):
    return SessionContextSnapshot(
        version=version,
        summary=summary,
        requirements=("keep exact wording",),
        decisions=("checkpoint owns execution",),
        open_items=(),
        evidence_refs=(),
        workspace_refs=("workspace://runtime",),
        covered_through_message_id=uuid.uuid4(),
    )


def _session_message(message_id: str, role: str = "user") -> dict:
    return {
        "id": message_id,
        "role": role,
        "content": message_id,
        "created_at": "2026-07-13T10:00:00+00:00",
    }


def _normal(message_id: str) -> dict:
    return {"id": message_id, "role": "user", "content": message_id}


def _assistant(message_id: str, call_ids: list[str]) -> dict:
    return {
        "id": message_id,
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": f"tool_{call_id}", "arguments": "{}"},
            }
            for call_id in call_ids
        ],
    }


def _tool_result(message_id: str, call_id: str) -> dict:
    return {
        "id": message_id,
        "role": "tool",
        "tool_call_id": call_id,
        "content": f"result:{call_id}",
    }


def _state(
    *,
    snapshots: RunInputSnapshots,
    run_messages: list[dict] | None = None,
    status: str = "running",
    next_route: str = "model",
) -> RuntimeGraphState:
    return {
        "registry": RunRegistrySnapshot(
            tenant_id=str(uuid.uuid4()),
            run_id=str(uuid.uuid4()),
            goal="Finish the task",
            run_kind="foreground",
            source_type="chat",
            model_id=str(uuid.uuid4()),
            graph_name="clawith_agent_runtime",
            graph_version="v1",
            agent_id=str(uuid.uuid4()),
            session_id=str(uuid.uuid4()),
        ),
        "snapshots": snapshots,
        "lifecycle": {
            "status": status,
            "next_route": next_route,
            "run_messages": run_messages or [],
            "run_summary": {"progress": ["read docs"]},
            "waiting_request": None,
            "verification_result": None,
        },
    }


@pytest.mark.asyncio
async def test_capture_new_run_freezes_latest_session_context_and_recent_messages():
    tenant_id = uuid.uuid4()
    session_id = uuid.uuid4()
    pack = SessionContextPack(
        snapshot=_snapshot(),
        recent_messages=tuple(
            _session_message(f"session-{index}", "user" if index % 2 == 0 else "assistant") for index in range(20)
        ),
    )
    session_service = _SessionContextService(pack)
    builder = context_builder.ContextBuilder(session_service)

    db = object()
    snapshots = await builder.capture_run_inputs(
        db,
        tenant_id=tenant_id,
        session_id=session_id,
        initial_input={"message_id": "session-19"},
        related_run_summaries=[{"run_id": "dependency", "result_summary": "done"}],
    )

    assert snapshots.session_context_version == 3
    assert snapshots.session_context["summary"] == "session summary"
    assert len(snapshots.recent_session_messages) == 20
    assert snapshots.related_run_summaries[0]["run_id"] == "dependency"
    assert session_service.calls == [(db, tenant_id, session_id)]


@pytest.mark.asyncio
async def test_resume_build_reuses_checkpoint_snapshot_without_refreshing_session():
    original_pack = SessionContextPack(
        snapshot=_snapshot(version=2, summary="original"),
        recent_messages=(_session_message("original-message"),),
    )
    session_service = _SessionContextService(original_pack)
    builder = context_builder.ContextBuilder(session_service)
    tenant_id = uuid.uuid4()
    session_id = uuid.uuid4()
    snapshots = await builder.capture_run_inputs(
        object(),
        tenant_id=tenant_id,
        session_id=session_id,
        initial_input={"content": "start"},
    )
    session_service.pack = SessionContextPack(
        snapshot=_snapshot(version=9, summary="new parallel work"),
        recent_messages=(_session_message("parallel-message"),),
    )

    built = await builder.build(
        _state(snapshots=snapshots, run_messages=[_normal("run-message")]),
        resume_input={"content": "continue"},
    )

    assert len(session_service.calls) == 1
    assert built.session_context_snapshot["version"] == 2
    assert built.session_context_snapshot["summary"] == "original"
    assert [message["id"] for message in built.recent_session_messages_snapshot] == ["original-message"]
    assert built.resume_input == {"content": "continue"}


@pytest.mark.asyncio
async def test_recent_20_run_messages_expand_to_keep_parallel_tool_exchange_whole():
    exchange = [
        _assistant("assistant-tools", ["call-a", "call-b"]),
        _tool_result("result-a", "call-a"),
        _tool_result("result-b", "call-b"),
    ]
    run_messages = [*exchange, *[_normal(f"recent-{index}") for index in range(19)]]
    snapshots = RunInputSnapshots(
        session_context=SessionContextSnapshot.empty().to_json(),
        session_context_version=0,
        recent_session_messages=(),
        related_run_summaries=(),
        initial_input={"content": "start"},
    )
    builder = context_builder.ContextBuilder(
        _SessionContextService(SessionContextPack(SessionContextSnapshot.empty(), ()))
    )

    built = await builder.build(_state(snapshots=snapshots, run_messages=run_messages))

    assert len(built.recent_run_messages) == 22
    assert [message["id"] for message in built.recent_run_messages[:3]] == [
        "assistant-tools",
        "result-a",
        "result-b",
    ]
    assert built.blocked is False
    assert built.omitted_tool_exchanges == ()


@pytest.mark.asyncio
async def test_incomplete_started_tool_exchange_blocks_model_context():
    snapshots = RunInputSnapshots(
        session_context=SessionContextSnapshot.empty().to_json(),
        session_context_version=0,
        recent_session_messages=(),
        related_run_summaries=(),
        initial_input={"content": "start"},
    )
    builder = context_builder.ContextBuilder(
        _SessionContextService(SessionContextPack(SessionContextSnapshot.empty(), ()))
    )

    built = await builder.build(
        _state(
            snapshots=snapshots,
            run_messages=[_assistant("assistant-pending", ["call-pending"])],
        ),
        tool_execution_ledger={"call-pending": {"status": "started"}},
    )

    assert built.recent_run_messages == ()
    assert built.blocked is True
    assert built.retry_model is False
    assert built.requires_confirmation is False


@pytest.mark.asyncio
async def test_current_run_uses_checkpoint_lifecycle_and_has_no_query_projection_input():
    snapshots = RunInputSnapshots(
        session_context=SessionContextSnapshot.empty().to_json(),
        session_context_version=0,
        recent_session_messages=(),
        related_run_summaries=(),
        initial_input={"content": "start"},
    )
    builder = context_builder.ContextBuilder(
        _SessionContextService(SessionContextPack(SessionContextSnapshot.empty(), ()))
    )
    state = _state(snapshots=snapshots, status="waiting_user", next_route="wait")
    state["lifecycle"]["waiting_request"] = {"question": "Which option?"}

    built = await builder.build(state)

    assert built.current_run["lifecycle_status"] == "waiting_user"
    assert built.current_run["waiting_request"] == {"question": "Which option?"}
    assert all(not key.startswith("projected_") for key in built.current_run)
    source = inspect.getsource(context_builder)
    assert "projected_execution_status" not in source
    assert "from app.models.agent_run" not in source


@pytest.mark.asyncio
async def test_sessionless_run_captures_an_explicit_empty_context():
    session_service = _SessionContextService(SessionContextPack(_snapshot(), (_session_message("unused"),)))
    builder = context_builder.ContextBuilder(session_service)

    snapshots = await builder.capture_run_inputs(
        object(),
        tenant_id=uuid.uuid4(),
        session_id=None,
        initial_input={"trigger": "heartbeat"},
    )

    assert snapshots.session_context_version == 0
    assert snapshots.session_context["summary"] == ""
    assert snapshots.recent_session_messages == ()
    assert session_service.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_number", [float("nan"), float("inf"), float("-inf")])
async def test_checkpoint_json_contract_rejects_non_finite_numbers(invalid_number):
    builder = context_builder.ContextBuilder(
        _SessionContextService(SessionContextPack(SessionContextSnapshot.empty(), ()))
    )

    with pytest.raises(context_builder.ContextBuildError) as exc_info:
        await builder.capture_run_inputs(
            object(),
            tenant_id=uuid.uuid4(),
            session_id=None,
            initial_input={"invalid_number": invalid_number},
        )

    assert exc_info.value.code == "invalid_runtime_context"
    assert "non-finite" in str(exc_info.value)
