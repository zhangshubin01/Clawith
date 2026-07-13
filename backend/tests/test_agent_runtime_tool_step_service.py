"""Receipt-backed Runtime tool-step tests."""

from contextlib import asynccontextmanager
from collections import deque
import uuid

import pytest

from app.models.agent import Agent
from app.models.agent_tool_execution import AgentToolExecution
from app.services.agent_runtime import tool_step_service
from app.services.agent_runtime.a2a_runtime import A2ARuntimeToolResult
from app.services.agent_runtime.node_executor import CancelSignal
from app.services.agent_runtime.state import (
    RunInputSnapshots,
    RunRegistrySnapshot,
    RuntimeContext,
    RuntimeGraphState,
)
from app.services.agent_runtime.tool_execution import (
    ToolExecutionOutcome,
    ToolExecutionReservation,
)


class _Result:
    def __init__(self, value=None) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Begin:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _DB:
    def __init__(self, agent: Agent) -> None:
        self.agent = agent

    async def execute(self, statement):
        del statement
        return _Result(self.agent)

    def begin(self):
        return _Begin()


def _session_factory(agent: Agent):
    @asynccontextmanager
    async def factory():
        yield _DB(agent)

    return factory


class _CancelSource:
    def __init__(self, *signals: CancelSignal | None) -> None:
        self.signals = deque(signals)

    async def get_cancel(self, state, context):
        del state, context
        return self.signals.popleft() if self.signals else None


class _A2AService:
    def __init__(self, result: A2ARuntimeToolResult) -> None:
        self.result = result
        self.calls: list[dict] = []

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _agent(tenant_id: uuid.UUID, *, access_mode: str = "company") -> Agent:
    return Agent(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        creator_id=uuid.uuid4(),
        name="Tool Agent",
        status="idle",
        is_expired=False,
        access_mode=access_mode,
    )


def _call(call_id: str, name: str) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


def _a2a_call(call_id: str, *, mode: str) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "send_message_to_agent",
            "arguments": (
                '{"agent_name":"Researcher","message":"Check the facts",'
                f'"msg_type":"{mode}"}}'
            ),
        },
    }


def _state(
    tenant_id: uuid.UUID,
    agent: Agent,
    calls: tuple[dict, ...],
    *,
    source_type: str = "chat",
) -> RuntimeGraphState:
    run_id = uuid.uuid4()
    return {
        "registry": RunRegistrySnapshot(
            tenant_id=str(tenant_id),
            run_id=str(run_id),
            goal="Use tools",
            run_kind="foreground",
            source_type=source_type,
            model_id=str(uuid.uuid4()),
            graph_name="runtime",
            graph_version="v1",
            agent_id=str(agent.id),
            session_id=str(uuid.uuid4()),
        ),
        "snapshots": RunInputSnapshots(
            session_context={"version": 0},
            session_context_version=0,
            recent_session_messages=(),
            related_run_summaries=(),
            initial_input={},
        ),
        "lifecycle": {
            "status": "running",
            "next_route": "tool",
            "run_messages": [
                {
                    "id": "assistant-message-1",
                    "role": "assistant",
                    "content": "",
                    "tool_calls": list(calls),
                }
            ],
            "pending_tool_calls": list(calls),
        },
    }


def _context(state: RuntimeGraphState) -> RuntimeContext:
    return RuntimeContext(
        tenant_id=state["registry"].tenant_id,
        run_id=state["registry"].run_id,
        command_id="command-1",
        executor=object(),  # type: ignore[arg-type]
        actor_user_id=str(uuid.uuid4()),
    )


async def _tools(agent_id: uuid.UUID) -> list[dict]:
    del agent_id
    return [
        {"type": "function", "function": {"name": "read_file"}},
        {"type": "function", "function": {"name": "write_file"}},
        {"type": "function", "function": {"name": "plaza_get_new_posts"}},
        {"type": "function", "function": {"name": "plaza_create_post"}},
        {"type": "function", "function": {"name": "plaza_add_comment"}},
        {"type": "function", "function": {"name": "send_message_to_agent"}},
    ]


def _execution(
    tenant_id: uuid.UUID,
    run_id: uuid.UUID,
    call_id: str,
    tool_name: str,
) -> AgentToolExecution:
    return AgentToolExecution(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        run_id=run_id,
        tool_call_id=call_id,
        tool_name=tool_name,
        assistant_message_id="assistant-message-1",
        arguments_hash="hash",
        sanitized_arguments={},
        status="started",
        lease_owner=f"runtime:command-1:{call_id}",
    )


def _reservation(
    execution: AgentToolExecution,
    *,
    reusable: ToolExecutionOutcome | None = None,
    prior_failure: ToolExecutionOutcome | None = None,
    blocked: bool = False,
    requires_confirmation: bool = False,
    error_code: str | None = None,
) -> ToolExecutionReservation:
    return ToolExecutionReservation(
        execution=execution,
        created=not blocked and reusable is None,
        retrying=False,
        reusable_result=reusable,
        prior_failure=prior_failure,
        blocked=blocked,
        reconciliation_required=blocked and prior_failure is None,
        requires_confirmation=requires_confirmation,
        error_code=error_code,
    )


def _service(
    agent: Agent,
    cancel_source: _CancelSource,
    executor,
    *,
    a2a_service=None,
) -> tool_step_service.RuntimeToolStepService:
    return tool_step_service.RuntimeToolStepService(
        session_factory=_session_factory(agent),
        cancel_source=cancel_source,
        tool_provider=_tools,
        tool_executor=executor,
        a2a_service=a2a_service,
    )


@pytest.mark.asyncio
async def test_success_is_reserved_before_execution_and_settled_afterwards(
    monkeypatch,
) -> None:
    tenant_id = uuid.uuid4()
    agent = _agent(tenant_id)
    call = _call("call-1", "read_file")
    state = _state(tenant_id, agent, (call,))
    execution = _execution(
        tenant_id,
        uuid.UUID(state["registry"].run_id),
        "call-1",
        "read_file",
    )
    order = []

    async def reserve(db, **kwargs):
        del db
        order.append(("reserve", kwargs))
        return _reservation(execution)

    async def execute(name, arguments, agent_id, user_id, session_id="", on_output=None):
        del arguments, agent_id, user_id, session_id, on_output
        order.append(("execute", name))
        return "file contents"

    async def mark(db, **kwargs):
        del db, kwargs
        order.append(("mark", "succeeded"))
        execution.status = "succeeded"
        execution.result_summary = "file contents"
        return execution

    monkeypatch.setattr(tool_step_service, "reserve_tool_execution", reserve)
    monkeypatch.setattr(tool_step_service, "mark_tool_execution_succeeded", mark)

    result = await _service(agent, _CancelSource(None), execute).execute_pending(
        state,
        _context(state),
        (call,),
    )

    assert [item[0] for item in order] == ["reserve", "execute", "mark"]
    assert order[0][1]["side_effect_classification"] == "read"
    assert order[0][1]["retry_policy"] == "safe"
    assert result.error is None
    assert result.waiting_request is None
    assert result.pending_tool_calls == ()
    assert result.messages == (
        {
            "id": str(
                uuid.uuid5(
                    uuid.UUID(state["registry"].run_id),
                    "tool-result:call-1",
                )
            ),
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "read_file",
            "content": "file contents",
            "execution_status": "succeeded",
            "result_ref": None,
        },
    )


@pytest.mark.asyncio
async def test_succeeded_receipt_is_reused_without_executing_tool(
    monkeypatch,
) -> None:
    tenant_id = uuid.uuid4()
    agent = _agent(tenant_id)
    call = _call("call-reuse", "read_file")
    state = _state(tenant_id, agent, (call,))
    execution = _execution(
        tenant_id,
        uuid.UUID(state["registry"].run_id),
        "call-reuse",
        "read_file",
    )
    reusable = ToolExecutionOutcome(
        status="succeeded",
        result_summary="cached result",
        result_ref=None,
    )

    async def reserve(db, **kwargs):
        del db, kwargs
        return _reservation(execution, reusable=reusable)

    async def forbidden(*args, **kwargs):
        raise AssertionError(f"reused tool executed: {args}, {kwargs}")

    monkeypatch.setattr(tool_step_service, "reserve_tool_execution", reserve)

    result = await _service(agent, _CancelSource(None), forbidden).execute_pending(
        state,
        _context(state),
        (call,),
    )

    assert result.messages[0]["content"] == "cached result"
    assert result.messages[0]["execution_status"] == "succeeded"


@pytest.mark.asyncio
async def test_read_failure_is_known_and_returned_to_model_without_retry(
    monkeypatch,
) -> None:
    tenant_id = uuid.uuid4()
    agent = _agent(tenant_id)
    call = _call("call-read-fail", "read_file")
    state = _state(tenant_id, agent, (call,))
    execution = _execution(
        tenant_id,
        uuid.UUID(state["registry"].run_id),
        "call-read-fail",
        "read_file",
    )

    async def reserve(db, **kwargs):
        del db, kwargs
        return _reservation(execution)

    async def execute(*args, **kwargs):
        del args, kwargs
        raise FileNotFoundError("secret path")

    async def mark_failed(db, **kwargs):
        del db
        execution.status = "failed"
        execution.result_summary = kwargs["result_summary"]
        return execution

    monkeypatch.setattr(tool_step_service, "reserve_tool_execution", reserve)
    monkeypatch.setattr(tool_step_service, "mark_tool_execution_failed", mark_failed)

    result = await _service(agent, _CancelSource(None), execute).execute_pending(
        state,
        _context(state),
        (call,),
    )

    assert result.error is None
    assert result.waiting_request is None
    assert result.messages[0]["execution_status"] == "failed"
    assert result.messages[0]["content"] == "FileNotFoundError: tool execution failed"
    assert "secret path" not in str(result.messages[0])


@pytest.mark.asyncio
async def test_write_exception_is_unknown_and_preserves_the_unresolved_batch(
    monkeypatch,
) -> None:
    tenant_id = uuid.uuid4()
    agent = _agent(tenant_id)
    first = _call("call-write", "write_file")
    second = _call("call-after", "read_file")
    state = _state(tenant_id, agent, (first, second))
    execution = _execution(
        tenant_id,
        uuid.UUID(state["registry"].run_id),
        "call-write",
        "write_file",
    )

    async def reserve(db, **kwargs):
        del db
        assert kwargs["side_effect_classification"] == "external_write"
        assert kwargs["retry_policy"] == "never"
        return _reservation(execution)

    async def execute(*args, **kwargs):
        del args, kwargs
        raise TimeoutError("outcome unknown")

    async def mark_unknown(db, **kwargs):
        del db
        execution.status = "unknown"
        execution.result_summary = kwargs["result_summary"]
        return execution

    monkeypatch.setattr(tool_step_service, "reserve_tool_execution", reserve)
    monkeypatch.setattr(tool_step_service, "mark_tool_execution_unknown", mark_unknown)

    result = await _service(agent, _CancelSource(None), execute).execute_pending(
        state,
        _context(state),
        (first, second),
    )

    assert result.messages == ()
    assert result.error is None
    assert result.waiting_request is not None
    assert result.waiting_request["waiting_type"] == "user"
    assert result.waiting_request["reason"] == "tool_outcome_unknown"
    assert result.pending_tool_calls == (first, second)


@pytest.mark.asyncio
async def test_cancel_between_calls_stops_before_reserving_the_next_tool(
    monkeypatch,
) -> None:
    tenant_id = uuid.uuid4()
    agent = _agent(tenant_id)
    first = _call("call-first", "read_file")
    second = _call("call-second", "read_file")
    state = _state(tenant_id, agent, (first, second))
    reserved = []

    async def reserve(db, **kwargs):
        del db
        reserved.append(kwargs["tool_call_id"])
        return _reservation(
            _execution(
                tenant_id,
                uuid.UUID(state["registry"].run_id),
                kwargs["tool_call_id"],
                kwargs["tool_name"],
            )
        )

    async def execute(*args, **kwargs):
        del args, kwargs
        return "done"

    async def mark(db, **kwargs):
        del db
        execution = _execution(
            tenant_id,
            uuid.UUID(state["registry"].run_id),
            "call-first",
            "read_file",
        )
        execution.id = kwargs["execution_id"]
        execution.status = "succeeded"
        execution.result_summary = "done"
        return execution

    monkeypatch.setattr(tool_step_service, "reserve_tool_execution", reserve)
    monkeypatch.setattr(tool_step_service, "mark_tool_execution_succeeded", mark)
    cancel = CancelSignal(command_id="cancel-command", reason="user_abort")

    result = await _service(
        agent,
        _CancelSource(None, cancel),
        execute,
    ).execute_pending(state, _context(state), (first, second))

    assert reserved == ["call-first"]
    assert len(result.messages) == 1
    assert result.cancel_signal == cancel
    assert result.pending_tool_calls == ()


@pytest.mark.asyncio
async def test_started_receipt_waits_for_reconciliation_and_keeps_pending_call(
    monkeypatch,
) -> None:
    tenant_id = uuid.uuid4()
    agent = _agent(tenant_id)
    call = _call("call-started", "write_file")
    state = _state(tenant_id, agent, (call,))
    execution = _execution(
        tenant_id,
        uuid.UUID(state["registry"].run_id),
        "call-started",
        "write_file",
    )

    async def reserve(db, **kwargs):
        del db, kwargs
        return _reservation(
            execution,
            blocked=True,
            error_code="tool_execution_started",
        )

    async def forbidden(*args, **kwargs):
        raise AssertionError(f"started tool executed: {args}, {kwargs}")

    monkeypatch.setattr(tool_step_service, "reserve_tool_execution", reserve)

    result = await _service(agent, _CancelSource(None), forbidden).execute_pending(
        state,
        _context(state),
        (call,),
    )

    assert result.waiting_request is not None
    assert result.waiting_request["waiting_type"] == "external"
    assert result.pending_tool_calls == (call,)


@pytest.mark.asyncio
async def test_duplicate_call_ids_fail_before_any_reservation_or_execution() -> None:
    tenant_id = uuid.uuid4()
    agent = _agent(tenant_id)
    first = _call("duplicate", "read_file")
    second = _call("duplicate", "write_file")
    state = _state(tenant_id, agent, (first, second))

    async def forbidden(*args, **kwargs):
        raise AssertionError(f"duplicate call executed: {args}, {kwargs}")

    result = await _service(
        agent,
        _CancelSource(),
        forbidden,
    ).execute_pending(state, _context(state), (first, second))

    assert result.error is not None
    assert result.error["code"] == "invalid_tool_call"
    assert result.messages == ()


@pytest.mark.asyncio
async def test_private_heartbeat_plaza_call_is_receipted_without_execution(
    monkeypatch,
) -> None:
    tenant_id = uuid.uuid4()
    agent = _agent(tenant_id, access_mode="private")
    call = _call("private-plaza", "plaza_get_new_posts")
    state = _state(tenant_id, agent, (call,), source_type="heartbeat")
    execution = _execution(
        tenant_id,
        uuid.UUID(state["registry"].run_id),
        "private-plaza",
        "plaza_get_new_posts",
    )

    async def reserve(db, **kwargs):
        del db, kwargs
        return _reservation(execution)

    async def mark_failed(db, **kwargs):
        del db
        execution.status = "failed"
        execution.result_summary = kwargs["result_summary"]
        return execution

    async def forbidden(*args, **kwargs):
        raise AssertionError(f"private Plaza tool executed: {args}, {kwargs}")

    monkeypatch.setattr(tool_step_service, "reserve_tool_execution", reserve)
    monkeypatch.setattr(
        tool_step_service,
        "mark_tool_execution_failed",
        mark_failed,
    )

    result = await _service(agent, _CancelSource(None), forbidden).execute_pending(
        state,
        _context(state),
        (call,),
    )

    assert result.error is None
    assert result.messages[0]["execution_status"] == "failed"
    assert result.messages[0]["content"] == (
        "[BLOCKED] Private heartbeat Agents cannot use Agent Plaza."
    )


@pytest.mark.asyncio
async def test_public_heartbeat_comment_limit_counts_successful_receipts(
    monkeypatch,
) -> None:
    tenant_id = uuid.uuid4()
    agent = _agent(tenant_id)
    calls = tuple(
        _call(f"comment-{index}", "plaza_add_comment")
        for index in range(1, 4)
    )
    state = _state(tenant_id, agent, calls, source_type="heartbeat")
    run_id = uuid.UUID(state["registry"].run_id)
    executions: dict[uuid.UUID, AgentToolExecution] = {}
    successful_counts = deque([0, 1, 2])
    executed: list[str] = []

    async def reserve(db, **kwargs):
        del db
        execution = _execution(
            tenant_id,
            run_id,
            kwargs["tool_call_id"],
            kwargs["tool_name"],
        )
        executions[execution.id] = execution
        return _reservation(execution)

    async def successful_count(**kwargs):
        assert kwargs["tenant_id"] == tenant_id
        assert kwargs["run_id"] == run_id
        assert kwargs["tool_name"] == "plaza_add_comment"
        return successful_counts.popleft()

    async def execute(name, arguments, agent_id, user_id, session_id="", on_output=None):
        del arguments, agent_id, user_id, session_id, on_output
        executed.append(name)
        return "comment added"

    async def mark_succeeded(db, **kwargs):
        del db
        execution = executions[kwargs["execution_id"]]
        execution.status = "succeeded"
        execution.result_summary = kwargs["result_summary"]
        return execution

    async def mark_failed(db, **kwargs):
        del db
        execution = executions[kwargs["execution_id"]]
        execution.status = "failed"
        execution.result_summary = kwargs["result_summary"]
        return execution

    monkeypatch.setattr(tool_step_service, "reserve_tool_execution", reserve)
    monkeypatch.setattr(
        tool_step_service,
        "mark_tool_execution_succeeded",
        mark_succeeded,
    )
    monkeypatch.setattr(
        tool_step_service,
        "mark_tool_execution_failed",
        mark_failed,
    )
    service = _service(agent, _CancelSource(None, None, None), execute)
    monkeypatch.setattr(service, "_successful_tool_count", successful_count)

    result = await service.execute_pending(state, _context(state), calls)

    assert executed == ["plaza_add_comment", "plaza_add_comment"]
    assert [message["execution_status"] for message in result.messages] == [
        "succeeded",
        "succeeded",
        "failed",
    ]
    assert result.messages[2]["content"] == (
        "[BLOCKED] Heartbeat limit reached for plaza_add_comment (maximum 2)."
    )


@pytest.mark.asyncio
async def test_replayed_heartbeat_plaza_call_reuses_receipt_before_limit_check(
    monkeypatch,
) -> None:
    tenant_id = uuid.uuid4()
    agent = _agent(tenant_id)
    call = _call("replayed-post", "plaza_create_post")
    state = _state(tenant_id, agent, (call,), source_type="heartbeat")
    execution = _execution(
        tenant_id,
        uuid.UUID(state["registry"].run_id),
        "replayed-post",
        "plaza_create_post",
    )
    reusable = ToolExecutionOutcome(
        status="succeeded",
        result_summary="original post",
        result_ref=None,
    )

    async def reserve(db, **kwargs):
        del db, kwargs
        return _reservation(execution, reusable=reusable)

    async def forbidden(*args, **kwargs):
        raise AssertionError(f"replayed Plaza tool executed: {args}, {kwargs}")

    async def forbidden_count(**kwargs):
        raise AssertionError(f"replayed receipt was counted again: {kwargs}")

    monkeypatch.setattr(tool_step_service, "reserve_tool_execution", reserve)
    service = _service(agent, _CancelSource(None), forbidden)
    monkeypatch.setattr(service, "_successful_tool_count", forbidden_count)

    result = await service.execute_pending(state, _context(state), (call,))

    assert result.messages[0]["execution_status"] == "succeeded"
    assert result.messages[0]["content"] == "original post"


@pytest.mark.asyncio
async def test_runtime_a2a_request_interrupts_source_after_durable_target_acceptance(
    monkeypatch,
) -> None:
    tenant_id = uuid.uuid4()
    agent = _agent(tenant_id)
    call = _a2a_call("delegate-1", mode="task_delegate")
    state = _state(tenant_id, agent, (call,))
    run_id = uuid.UUID(state["registry"].run_id)
    execution = _execution(
        tenant_id,
        run_id,
        "delegate-1",
        "send_message_to_agent",
    )
    target_run_id = uuid.uuid4()
    correlation_id = f"a2a:task_delegate:{uuid.uuid4()}"
    a2a_service = _A2AService(
        A2ARuntimeToolResult(
            outcome=ToolExecutionOutcome(
                status="succeeded",
                result_summary="delegation accepted",
                result_ref=f"agent-run:{target_run_id}",
            ),
            target_run_id=target_run_id,
            waiting_request={
                "waiting_type": "agent",
                "correlation_id": correlation_id,
                "reason": "waiting_for_task_delegate",
                "target_run_id": str(target_run_id),
            },
        )
    )

    async def reserve(db, **kwargs):
        del db, kwargs
        return _reservation(execution)

    async def forbidden(*args, **kwargs):
        raise AssertionError(f"legacy A2A executor called: {args}, {kwargs}")

    monkeypatch.setattr(tool_step_service, "reserve_tool_execution", reserve)
    result = await _service(
        agent,
        _CancelSource(None),
        forbidden,
        a2a_service=a2a_service,
    ).execute_pending(state, _context(state), (call,))

    assert len(a2a_service.calls) == 1
    assert a2a_service.calls[0]["source_run_id"] == run_id
    assert result.error is None
    assert result.waiting_request is not None
    assert result.waiting_request["waiting_type"] == "agent"
    assert result.waiting_request["correlation_id"] == correlation_id
    assert result.pending_tool_calls == ()
    assert result.messages[0]["execution_status"] == "succeeded"
    assert result.messages[0]["result_ref"] == f"agent-run:{target_run_id}"


@pytest.mark.asyncio
async def test_replayed_runtime_a2a_request_rebuilds_same_interrupt_from_receipt(
    monkeypatch,
) -> None:
    tenant_id = uuid.uuid4()
    agent = _agent(tenant_id)
    call = _a2a_call("consult-1", mode="consult")
    state = _state(tenant_id, agent, (call,))
    run_id = uuid.UUID(state["registry"].run_id)
    target_run_id = uuid.uuid4()
    execution = _execution(
        tenant_id,
        run_id,
        "consult-1",
        "send_message_to_agent",
    )
    reusable = ToolExecutionOutcome(
        status="succeeded",
        result_summary="consultation accepted",
        result_ref=f"agent-run:{target_run_id}",
    )

    async def reserve(db, **kwargs):
        del db, kwargs
        return _reservation(execution, reusable=reusable)

    async def forbidden(*args, **kwargs):
        raise AssertionError(f"replayed A2A executed: {args}, {kwargs}")

    a2a_service = _A2AService(
        A2ARuntimeToolResult(
            outcome=reusable,
            target_run_id=target_run_id,
        )
    )
    monkeypatch.setattr(tool_step_service, "reserve_tool_execution", reserve)
    result = await _service(
        agent,
        _CancelSource(None),
        forbidden,
        a2a_service=a2a_service,
    ).execute_pending(state, _context(state), (call,))

    assert a2a_service.calls == []
    assert result.waiting_request == {
        "waiting_type": "agent",
        "correlation_id": (
            f"a2a:consult:{uuid.uuid5(run_id, 'a2a-result:consult-1')}"
        ),
        "reason": "waiting_for_consult",
        "target_run_id": str(target_run_id),
    }
    assert result.messages[0]["content"] == "consultation accepted"


@pytest.mark.asyncio
async def test_runtime_a2a_notify_continues_without_waiting(monkeypatch) -> None:
    tenant_id = uuid.uuid4()
    agent = _agent(tenant_id)
    call = _a2a_call("notify-1", mode="notify")
    state = _state(tenant_id, agent, (call,))
    execution = _execution(
        tenant_id,
        uuid.UUID(state["registry"].run_id),
        "notify-1",
        "send_message_to_agent",
    )
    target_run_id = uuid.uuid4()
    a2a_service = _A2AService(
        A2ARuntimeToolResult(
            outcome=ToolExecutionOutcome(
                status="succeeded",
                result_summary="notification accepted",
                result_ref=f"agent-run:{target_run_id}",
            ),
            target_run_id=target_run_id,
        )
    )

    async def reserve(db, **kwargs):
        del db, kwargs
        return _reservation(execution)

    async def forbidden(*args, **kwargs):
        raise AssertionError(f"legacy notify executor called: {args}, {kwargs}")

    monkeypatch.setattr(tool_step_service, "reserve_tool_execution", reserve)
    result = await _service(
        agent,
        _CancelSource(None),
        forbidden,
        a2a_service=a2a_service,
    ).execute_pending(state, _context(state), (call,))

    assert result.waiting_request is None
    assert result.pending_tool_calls == ()
    assert result.messages[0]["content"] == "notification accepted"
