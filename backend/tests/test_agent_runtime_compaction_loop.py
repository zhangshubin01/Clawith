"""Compaction-amnesia loop breaker (B) — detector, threshold, terminate.

Ticket B / S5: ``detect_loop`` counts adjacent identical (prefix, tools)
fingerprint pairs separated by a REAL compaction; the model step alerts when
the count reaches the threshold; the executor terminates the Run only when the
terminate switch is on and the model is about to spin the same tools again.
"""

from __future__ import annotations

import uuid

import pytest

from app.services.agent_runtime.model_step_service import _advance_loop_detection
from app.services.agent_runtime.node_executor import (
    DeterministicRuntimeNodeExecutor,
    ModelStepResult,
    RunCompactResult,
)
from app.services.agent_runtime.run_compactor import (
    LoopFingerprintEvent,
    detect_loop,
)
from app.services.agent_runtime.state import (
    JsonObject,
    RunInputSnapshots,
    RunRegistrySnapshot,
    RuntimeContext,
    RuntimeGraphState,
)


def _event(
    prefix: str = "a",
    tools: str = "t",
    *,
    compacted: bool = False,
) -> LoopFingerprintEvent:
    return LoopFingerprintEvent(
        prefix_fp=prefix,
        tools_fp=tools,
        compaction_since_last_prefix=compacted,
    )


def test_detect_loop_cross_table() -> None:
    """Compaction-between × tool-fingerprint-match cross table."""
    # Real compaction between identical (prefix, tools) pairs: one loop.
    assert detect_loop([_event(), _event(compacted=True)]) == 1
    # No compaction between identical pairs: the same stuck observation, 0.
    assert detect_loop([_event(), _event(compacted=False)]) == 0
    # Compaction but the tool pattern changed: not the amnesia signature.
    assert detect_loop([_event(), _event(tools="t2", compacted=True)]) == 0
    # Compaction but the prefix changed: not the amnesia signature.
    assert detect_loop([_event(), _event(prefix="b", compacted=True)]) == 0
    # Empty and single-event windows never count.
    assert detect_loop([]) == 0
    assert detect_loop([_event(compacted=True)]) == 0


def test_three_occurrences_count_two_loops() -> None:
    """Each re-confirmation after a real compaction is one more loop."""
    events = [
        _event(compacted=True),
        _event(compacted=True),
        _event(compacted=True),
    ]
    assert detect_loop(events) == 2


def test_repeat_without_new_compaction_counts_once() -> None:
    """Identical repeats without intervening compaction do not inflate."""
    events = [
        _event(compacted=True),
        _event(compacted=True),
        _event(compacted=False),
        _event(compacted=False),
    ]
    assert detect_loop(events) == 1


def test_advance_loop_detection_alerts_at_threshold() -> None:
    """The rolling window consumes the compaction flag and alerts at >=1."""
    lifecycle: dict[str, object] = {}
    update, alert = _advance_loop_detection(
        lifecycle,
        prefix_fp="a",
        tools_fp="t",
        alert_threshold=1,
    )
    assert alert is None
    assert update["loop_count"] == 0
    assert update["compaction_since_last_prefix"] is False

    # The Compact node arms the flag; the next model step consumes it.
    update, alert = _advance_loop_detection(
        {"loop_detection": {**update, "compaction_since_last_prefix": True}},
        prefix_fp="a",
        tools_fp="t",
        alert_threshold=1,
    )
    assert alert == {"loop_count": 1, "prefix_fp": "a", "tools_fp": "t"}
    assert update["loop_count"] == 1
    assert update["compaction_since_last_prefix"] is False

    # A higher threshold stays silent until enough confirmations accumulate.
    lifecycle_threshold_2 = {"loop_detection": {**update, "compaction_since_last_prefix": True}}
    update2, alert2 = _advance_loop_detection(
        lifecycle_threshold_2,
        prefix_fp="a",
        tools_fp="t",
        alert_threshold=2,
    )
    assert alert2 == {"loop_count": 2, "prefix_fp": "a", "tools_fp": "t"}
    assert update2["loop_count"] == 2


def test_advance_loop_detection_window_is_bounded() -> None:
    """The fingerprint window never grows past the lifecycle limit."""
    update: JsonObject = {}
    for index in range(40):
        update, _alert = _advance_loop_detection(
            {"loop_detection": update},
            prefix_fp=f"p{index}",
            tools_fp="t",
            alert_threshold=1,
        )
    assert len(update["fingerprint_events"]) <= 16


# ---------------------------------------------------------------------------
# Executor wiring
# ---------------------------------------------------------------------------


class _ModelService:
    def __init__(self, result: ModelStepResult) -> None:
        self.result = result
        self.calls = 0

    async def complete_once(
        self,
        state: RuntimeGraphState,
        context: RuntimeContext,
    ) -> ModelStepResult:
        del state, context
        self.calls += 1
        return self.result


class _ToolService:
    async def execute_pending(
        self,
        state: RuntimeGraphState,
        context: RuntimeContext,
        tool_calls,
    ):
        raise AssertionError("the tool node must not be reached")

    async def load_run_ledger(self, context: RuntimeContext):
        raise AssertionError("no settlement in a model node")


class _CancelSource:
    async def get_cancel(
        self,
        state: RuntimeGraphState,
        context: RuntimeContext,
    ) -> None:
        del state, context
        return None


class _RunCompactor:
    def __init__(self, result: RunCompactResult) -> None:
        self.result = result

    async def compact_if_needed(
        self,
        state: RuntimeGraphState,
        context: RuntimeContext,
    ) -> RunCompactResult:
        del state, context
        return self.result


def _loop_call() -> JsonObject:
    return {
        "id": "c-1",
        "type": "function",
        "function": {"name": "edit_file", "arguments": "{}"},
    }


def _state(lifecycle: JsonObject) -> RuntimeGraphState:
    return {
        "registry": RunRegistrySnapshot(
            tenant_id="tenant-1",
            run_id="run-1",
            goal="work",
            run_kind="foreground",
            source_type="chat",
            model_id="model-1",
            graph_name="runtime_graph",
            graph_version="v1",
            agent_id="agent-1",
            session_id="session-1",
        ),
        "snapshots": RunInputSnapshots(
            session_context={"version": 0},
            session_context_version=0,
            recent_session_messages=(),
            related_run_summaries=(),
            initial_input={"message_id": "current", "input_content": "go"},
        ),
        "messages": [
            {
                "id": "current",
                "role": "user",
                "content": "go",
                "runtime_input": "current",
                "runtime_run_id": "run-1",
            }
        ],
        "lifecycle": lifecycle,  # type: ignore[typeddict-item]
    }


def _context() -> RuntimeContext:
    return RuntimeContext(
        tenant_id="tenant-1",
        run_id="run-1",
        command_id=str(uuid.uuid4()),
        executor=object(),  # type: ignore[arg-type]
        goal="work",
        run_kind="foreground",
        source_type="chat",
        model_id="model-1",
        graph_name="runtime_graph",
        graph_version="v1",
        agent_id="agent-1",
        session_id="session-1",
        system_role="assistant",
        parent_run_id=None,
        root_run_id=None,
        model_turn_limit=50,
    )


def _executor(*, terminate: bool, model_result: ModelStepResult):
    return DeterministicRuntimeNodeExecutor(
        cancel_source=_CancelSource(),
        model_service=_ModelService(model_result),
        tool_service=_ToolService(),
        run_compactor=_RunCompactor(RunCompactResult()),
        terminate_on_compaction_loop=terminate,
    )


@pytest.mark.asyncio
async def test_model_node_terminates_on_loop_alert_when_enabled() -> None:
    """loop_alert + terminate switch + spinning intent → terminal."""
    state = _state(
        {
            "status": "running",
            "next_route": "model",
            "pending_tool_calls": [],
            "model_step_count": 2,
        }
    )
    executor = _executor(
        terminate=True,
        model_result=ModelStepResult(
            intent="tool_calls",
            tool_calls=(_loop_call(),),
            loop_alert={"loop_count": 1, "prefix_fp": "a", "tools_fp": "t"},
        ),
    )
    update = await executor._model(state, _context())
    assert update["lifecycle"]["status"] == "failed"
    assert update["lifecycle"]["next_route"] == "terminal"
    assert update["lifecycle"]["reason"] == "compaction_loop_detected"
    assert update["lifecycle"]["pending_tool_calls"] == []


@pytest.mark.asyncio
async def test_model_node_continues_when_terminate_switch_off() -> None:
    """Alert without the switch: the tool route proceeds normally."""
    state = _state(
        {
            "status": "running",
            "next_route": "model",
            "pending_tool_calls": [],
            "model_step_count": 2,
        }
    )
    executor = _executor(
        terminate=False,
        model_result=ModelStepResult(
            intent="tool_calls",
            tool_calls=(_loop_call(),),
            loop_alert={"loop_count": 1, "prefix_fp": "a", "tools_fp": "t"},
        ),
    )
    update = await executor._model(state, _context())
    assert update["lifecycle"]["status"] == "running"
    assert update["lifecycle"]["next_route"] == "tool"
    assert update["lifecycle"]["pending_tool_calls"] == [_loop_call()]


@pytest.mark.asyncio
async def test_model_node_never_kills_non_spinning_intent() -> None:
    """A finish intent with a loop alert completes instead of terminating."""
    state = _state(
        {
            "status": "running",
            "next_route": "model",
            "pending_tool_calls": [],
            "model_step_count": 2,
        }
    )
    executor = _executor(
        terminate=True,
        model_result=ModelStepResult(
            intent="finish",
            finish_content="done",
            loop_alert={"loop_count": 1, "prefix_fp": "a", "tools_fp": "t"},
        ),
    )
    update = await executor._model(state, _context())
    assert update["lifecycle"]["status"] == "verifying"
    assert update["lifecycle"]["final_answer"] == "done"


@pytest.mark.asyncio
async def test_compact_node_arms_compaction_flag() -> None:
    """A real compaction arms compaction_since_last_prefix on the lifecycle."""
    state = _state(
        {
            "status": "running",
            "next_route": "compact",
            "pending_tool_calls": [],
            "loop_detection": {"fingerprint_events": [], "loop_count": 0},
        }
    )
    executor = DeterministicRuntimeNodeExecutor(
        cancel_source=_CancelSource(),
        model_service=_ModelService(ModelStepResult(intent="tool_calls", tool_calls=(_loop_call(),))),
        tool_service=_ToolService(),
        run_compactor=_RunCompactor(
            RunCompactResult(
                compacted=True,
                thread_summary={"text": "summary"},
                recent_messages=(
                    {
                        "id": "current",
                        "role": "user",
                        "content": "go",
                    },
                ),
                covered_through_message_id="t-9",
            )
        ),
    )
    update = await executor._compact(state, _context())
    assert update["lifecycle"]["loop_detection"]["compaction_since_last_prefix"] is True
    assert update["lifecycle"]["loop_detection"]["fingerprint_events"] == []
