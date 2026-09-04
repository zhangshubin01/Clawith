"""Step settlement (D) integration — cache, replay, compaction interleave.

Ticket A / S4: the executor-side settlement seam behaves like a sliding-window
replacement that can never regress the provider prefix cache beyond the single
first-settlement step, replays byte-identically after a killed Run, interleaves
with real Thread Compact without double synthesis, and removes no durable
observation event that was already derived from the raw history.
"""

from __future__ import annotations

import json
import uuid

import pytest

from langchain_core.messages import RemoveMessage

from app.services.agent_runtime.checkpoint_side_effects import (
    _runtime_observation_events,
)
from app.services.agent_runtime.command_worker import (
    CheckpointObservation,
    RuntimeRunRecord,
)
from app.services.agent_runtime.node_executor import (
    DeterministicRuntimeNodeExecutor,
    ModelStepResult,
    ToolStepResult,
)
from app.services.agent_runtime.run_compactor import (
    RunCompactInputs,
    RuntimeRunCompactorService,
    settle_step_messages,
)
from app.services.agent_runtime.state import (
    JsonObject,
    RunInputSnapshots,
    RunRegistrySnapshot,
    RuntimeContext,
    RuntimeGraphState,
)
from app.services.agent_runtime.tool_exchange import (
    validate_tool_exchange_integrity,
)
from app.services.llm.single_step import LLMCompletionStep
from app.services.token_tracker import TokenUsage

_EFFECTIVE_INPUT_BUDGET = 1200
_EXCHANGE_COUNT = 10


def _assistant(message_id: str, call_id: str) -> JsonObject:
    return {
        "id": message_id,
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "edit_file", "arguments": "{}"},
            }
        ],
    }


def _tool_result(message_id: str, call_id: str, *, content: str) -> JsonObject:
    return {
        "id": message_id,
        "role": "tool",
        "tool_call_id": call_id,
        "content": content,
    }


def _exchange(index: int, *, content: str) -> tuple[JsonObject, JsonObject]:
    return (
        _assistant(f"a-{index}", f"c-{index}"),
        _tool_result(f"t-{index}", f"c-{index}", content=content),
    )


def _ledger(indexes: range | list[int]) -> dict[str, JsonObject]:
    result: dict[str, JsonObject] = {}
    for index in indexes:
        result[f"c-{index}"] = {
            "status": "succeeded",
            "tool_name": "edit_file",
            "result_summary": f"Replaced 1 occurrence(s) in src/app.py ({index}).",
            "side_effect_classification": "write",
            "retry_policy": "conditional",
            "may_have_side_effect": True,
        }
    return result


def _start_messages(run_id: uuid.UUID) -> list[JsonObject]:
    return [
        {
            "id": "current",
            "role": "user",
            "content": "EXACT CURRENT INPUT",
            "runtime_input": "current",
            "runtime_run_id": str(run_id),
        },
        *_exchange(0, content="seed result"),
    ]


def _serialized(messages) -> str:
    return json.dumps(
        list(messages),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _common_prefix_bytes(left: str, right: str) -> int:
    index = 0
    limit = min(len(left), len(right))
    while index < limit and left[index] == right[index]:
        index += 1
    return index


def _first_diff_index(previous: list[JsonObject], current: list[JsonObject]) -> int:
    index = 0
    limit = min(len(previous), len(current))
    while index < limit and previous[index] == current[index]:
        index += 1
    return index


def _run_trajectory(
    run_id: uuid.UUID,
    *,
    steps: int,
) -> tuple[list[list[JsonObject]], list[str]]:
    """Step through tool batches; settle after each completed batch."""
    outputs: list[list[JsonObject]] = []
    serialized: list[str] = []
    messages = _start_messages(run_id)
    ledger = _ledger(range(_EXCHANGE_COUNT))
    for step in range(1, steps + 1):
        messages = [*messages, *_exchange(step, content="r" * 120)]
        settlement = settle_step_messages(
            messages,
            ledger,
            effective_input_budget=_EFFECTIVE_INPUT_BUDGET,
            current_input_id="current",
            current_run_id=str(run_id),
        )
        messages = list(settlement.messages)
        outputs.append(messages)
        serialized.append(_serialized(messages))
    return outputs, serialized


def test_trajectory_cache_prefix_monotone_after_single_first_dip() -> None:
    """Settlement slides the first difference forward; cache never regresses
    after the one-time first-settlement dip."""
    run_id = uuid.uuid4()
    outputs, serialized = _run_trajectory(run_id, steps=_EXCHANGE_COUNT)

    cache_reads = [_common_prefix_bytes(serialized[step - 1], serialized[step]) for step in range(1, len(serialized))]
    first_diffs = [_first_diff_index(outputs[step - 1], outputs[step]) for step in range(1, len(outputs))]

    # Exactly one cache regression, then monotone non-decreasing (steady state).
    dips = [step for step in range(1, len(cache_reads)) if cache_reads[step] < cache_reads[step - 1]]
    assert len(dips) == 1, f"expected one cache dip, got steps {dips}"
    for step in range(dips[0] + 1, len(cache_reads)):
        assert cache_reads[step] >= cache_reads[step - 1]

    # After the dip, the first difference position moves forward monotonically
    # and everything before it is byte-identical.
    for step in range(dips[0], len(first_diffs)):
        if step > dips[0]:
            assert first_diffs[step] >= first_diffs[step - 1]
        previous, current = outputs[step], outputs[step + 1]
        for index in range(first_diffs[step]):
            assert previous[index] == current[index]

    # Multiple exchanges actually settled, and the final history is intact.
    settled_count = sum(
        1
        for messages in outputs
        for message in messages
        if isinstance(message.get("content"), dict) and "historical_tool_exchange" in message["content"]
    )
    assert settled_count >= 3
    validate_tool_exchange_integrity(tuple(outputs[-1]))


def test_trajectory_replay_after_kill_is_byte_identical() -> None:
    """A killed Run replayed from a settled checkpoint diverges in nothing."""
    run_id = uuid.uuid4()
    first_run, first_serialized = _run_trajectory(run_id, steps=_EXCHANGE_COUNT)
    second_run, second_serialized = _run_trajectory(run_id, steps=_EXCHANGE_COUNT)

    assert first_serialized == second_serialized

    # Kill at step 5 and replay from the settled checkpoint onward.
    kill_step = 5
    resumed: list[JsonObject] = list(first_run[kill_step - 1])
    resumed_outputs: list[str] = []
    ledger = _ledger(range(_EXCHANGE_COUNT))
    for step in range(kill_step + 1, _EXCHANGE_COUNT + 1):
        resumed = [*resumed, *_exchange(step, content="r" * 120)]
        settlement = settle_step_messages(
            resumed,
            ledger,
            effective_input_budget=_EFFECTIVE_INPUT_BUDGET,
            current_input_id="current",
            current_run_id=str(run_id),
        )
        resumed = list(settlement.messages)
        resumed_outputs.append(_serialized(resumed))
    assert resumed_outputs == first_serialized[kill_step:]


@pytest.mark.asyncio
async def test_settlement_and_compaction_interleave_without_double_synthesis() -> None:
    """D×compact interleave: compaction never re-wraps a settled exchange."""
    run_id = uuid.uuid4()
    messages = _start_messages(run_id)
    for step in range(1, 4):
        messages = [*messages, *_exchange(step, content="w" * 150)]
    ledger = _ledger(range(_EXCHANGE_COUNT))

    settlement = settle_step_messages(
        messages,
        ledger,
        effective_input_budget=_EFFECTIVE_INPUT_BUDGET,
        current_input_id="current",
        current_run_id=str(run_id),
    )
    first_synthetic_ids = {
        message["id"]
        for message in settlement.messages
        if isinstance(message.get("content"), dict) and "historical_tool_exchange" in message["content"]
    }
    assert first_synthetic_ids

    # Real Thread Compact over the settled history.
    state = _compact_state(run_id, list(settlement.messages))
    context = _context(run_id, state)
    payloads: list[dict] = []

    async def complete(_model, prompt, **_kwargs):
        payloads.append(json.loads(prompt[0].content))
        return _step()

    compact_result = await _compact_service(complete).compact_if_needed(
        state,
        context,
    )
    assert compact_result.compacted is True
    assert compact_result.recent_messages is not None
    covered = json.dumps(payloads, ensure_ascii=False)
    assert "historical_tool_exchange" in covered  # settled facts feed the summary

    # Settlement over the post-compact history.
    post_compact = list(compact_result.recent_messages)
    second_settlement = settle_step_messages(
        post_compact,
        ledger,
        effective_input_budget=_EFFECTIVE_INPUT_BUDGET,
        current_input_id="current",
        current_run_id=str(run_id),
    )
    synthetic_ids = {
        message["id"]
        for message in second_settlement.messages
        if isinstance(message.get("content"), dict) and "historical_tool_exchange" in message["content"]
    }
    # No new synthetic id was invented and none was double-wrapped: every
    # synthetic id in the final history comes from the first settlement pass.
    assert synthetic_ids <= first_synthetic_ids
    validate_tool_exchange_integrity(second_settlement.messages)


def test_observation_events_after_settlement_are_subset() -> None:
    """Removing settled messages removes no already-derived event's source
    without a replacement: derived events after settlement ⊆ events before."""
    run_id = uuid.uuid4()
    messages = [
        {
            "id": "current",
            "role": "user",
            "content": "go",
            "runtime_input": "current",
            "runtime_run_id": str(run_id),
        },
        {
            "id": "a-1",
            "role": "assistant",
            "content": "working on it",
            "reasoning_content": "planning",
            "runtime_run_id": str(run_id),
            "tool_calls": [
                {
                    "id": "c-1",
                    "type": "function",
                    "function": {"name": "edit_file", "arguments": "{}"},
                }
            ],
        },
        _tool_result("t-1", "c-1", content="ok"),
    ]
    ledger = _ledger([1])
    run = RuntimeRunRecord(
        tenant_id=uuid.uuid4(),
        run_id=run_id,
        thread_id="thread-1",
        runtime_type="agent",
        goal="work",
        run_kind="foreground",
        source_type="chat",
        model_id=str(uuid.uuid4()),
        graph_name="runtime_graph",
        graph_version="v1",
    )

    def _keys(state: RuntimeGraphState) -> set[str]:
        events, _calls = _runtime_observation_events(
            run,
            CheckpointObservation(checkpoint_id="cp", state=state),
        )
        return {event[3] for event in events}

    before_state = _compact_state(run_id, messages)
    before_keys = _keys(before_state)
    assert before_keys

    settlement = settle_step_messages(
        messages,
        ledger,
        effective_input_budget=200,
        current_input_id="current",
        current_run_id=str(run_id),
    )
    after_state = _compact_state(run_id, list(settlement.messages))
    after_keys = _keys(after_state)
    assert after_keys <= before_keys
    assert before_keys - after_keys  # settlement actually removed event sources


# ---------------------------------------------------------------------------
# Executor Tool-node settlement shape
# ---------------------------------------------------------------------------


class _SettlementToolService:
    def __init__(self, ledger: dict[str, JsonObject]) -> None:
        self.ledger = ledger
        self.load_calls = 0

    async def execute_pending(
        self,
        state: RuntimeGraphState,
        context: RuntimeContext,
        tool_calls: tuple[JsonObject, ...],
    ) -> ToolStepResult:
        del state, context, tool_calls
        return ToolStepResult(messages=(_tool_result("t-2", "c-2", content="done"),))

    async def load_run_ledger(
        self,
        context: RuntimeContext,
    ) -> dict[str, JsonObject]:
        del context
        self.load_calls += 1
        return self.ledger


class _ModelService:
    async def complete_once(
        self,
        state: RuntimeGraphState,
        context: RuntimeContext,
    ) -> ModelStepResult:
        del state, context
        raise AssertionError("the model service must not be called here")


class _CancelSource:
    async def get_cancel(
        self,
        state: RuntimeGraphState,
        context: RuntimeContext,
    ) -> None:
        del state, context
        return None


def _tool_state(run_id: uuid.UUID, *, budget: int | None) -> RuntimeGraphState:
    lifecycle: JsonObject = {
        "status": "running",
        "next_route": "tool",
        "pending_tool_calls": [
            {
                "id": "c-2",
                "type": "function",
                "function": {"name": "edit_file", "arguments": "{}"},
            }
        ],
    }
    if budget is not None:
        lifecycle["step_budget_profile"] = {"effective_input_budget": budget}
    return _compact_state(
        run_id,
        [
            {
                "id": "current",
                "role": "user",
                "content": "EXACT CURRENT INPUT",
                "runtime_input": "current",
                "runtime_run_id": str(run_id),
            },
            _assistant("a-1", "c-1"),
            _tool_result("t-1", "c-1", content="first"),
            _assistant("a-2", "c-2"),
        ],
        lifecycle=lifecycle,
    )


@pytest.mark.asyncio
async def test_tool_node_settlement_update_shape() -> None:
    """The Tool node prepends RemoveMessage + synthetic to its update."""
    run_id = uuid.uuid4()
    state = _tool_state(run_id, budget=200)
    context = _context(run_id, state)
    tool_service = _SettlementToolService(_ledger(range(3)))
    executor = DeterministicRuntimeNodeExecutor(
        cancel_source=_CancelSource(),
        model_service=_ModelService(),
        tool_service=tool_service,
    )
    update = await executor._tool(state, context)
    assert tool_service.load_calls == 1

    messages = update["messages"]
    removes = [entry for entry in messages if isinstance(entry, RemoveMessage)]
    assert [str(entry.id) for entry in removes] == ["a-1", "t-1"]
    regular = [entry for entry in messages if not isinstance(entry, RemoveMessage)]
    assert [message["id"] for message in regular] == ["t-1", "t-2"]
    synthetic = regular[0]
    assert synthetic["role"] == "user"
    assert "historical_tool_exchange" in synthetic["content"]


@pytest.mark.asyncio
async def test_tool_node_skips_settlement_without_budget_profile() -> None:
    """No profile (legacy model step): no ledger load, no settlement."""
    run_id = uuid.uuid4()
    state = _tool_state(run_id, budget=None)
    context = _context(run_id, state)
    tool_service = _SettlementToolService(_ledger(range(3)))
    executor = DeterministicRuntimeNodeExecutor(
        cancel_source=_CancelSource(),
        model_service=_ModelService(),
        tool_service=tool_service,
    )
    update = await executor._tool(state, context)
    assert tool_service.load_calls == 0
    assert not any(isinstance(entry, RemoveMessage) for entry in update["messages"])


@pytest.mark.asyncio
async def test_tool_node_skips_settlement_on_budget_error() -> None:
    """A budget-only settlement error is fail-soft: the batch still lands."""
    run_id = uuid.uuid4()
    state = _tool_state(run_id, budget=10)
    context = _context(run_id, state)
    tool_service = _SettlementToolService(_ledger(range(3)))
    executor = DeterministicRuntimeNodeExecutor(
        cancel_source=_CancelSource(),
        model_service=_ModelService(),
        tool_service=tool_service,
    )
    update = await executor._tool(state, context)
    assert tool_service.load_calls == 1
    assert not any(isinstance(entry, RemoveMessage) for entry in update["messages"])
    assert update["messages"][0]["id"] == "t-2"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _step() -> LLMCompletionStep:
    return LLMCompletionStep(
        content=(
            "## Primary Request and Intent\nwork\n"
            "## Key Technical Concepts\nruntime\n"
            "## Files and Code\nrun_compactor.py\n"
            "## Errors and Fixes\nnone\n"
            "## Pending Jobs\nnone\n"
            "## Current Work\nsettling\n"
            "## Next Step\nanswer\n"
            "## Critical Context\nreceipt"
        ),
        tool_calls=(),
        reasoning_content=None,
        retry_instruction=None,
        usage=TokenUsage(total_tokens=10),
    )


def _compact_state(
    run_id: uuid.UUID,
    messages: list[JsonObject],
    *,
    lifecycle: JsonObject | None = None,
    keys: set[str] | None = None,
) -> RuntimeGraphState:
    del keys
    registry = RunRegistrySnapshot(
        tenant_id=str(uuid.uuid4()),
        run_id=str(run_id),
        goal="Complete the work",
        run_kind="foreground",
        source_type="chat",
        model_id=str(uuid.uuid4()),
        graph_name="runtime_graph",
        graph_version="v1",
        agent_id=str(uuid.uuid4()),
    )
    return {
        "registry": registry,
        "snapshots": RunInputSnapshots(
            session_context={"version": 0},
            session_context_version=0,
            recent_session_messages=(),
            related_run_summaries=(),
            initial_input={
                "message_id": "current",
                "input_content": "EXACT CURRENT INPUT",
            },
        ),
        "messages": messages,  # type: ignore[typeddict-item]
        "lifecycle": lifecycle
        or {
            "status": "running",
            "next_route": "model",
            "pending_tool_calls": [],
        },  # type: ignore[typeddict-item]
    }


def _context(
    run_id: uuid.UUID,
    state: RuntimeGraphState,
) -> RuntimeContext:
    registry = state["registry"]
    return RuntimeContext(
        tenant_id=registry.tenant_id,
        run_id=str(run_id),
        command_id=str(uuid.uuid4()),
        executor=object(),  # type: ignore[arg-type]
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
        model_turn_limit=50,
    )


def _compact_service(completion) -> RuntimeRunCompactorService:
    async def load(
        _state: RuntimeGraphState,
        _context: RuntimeContext,
    ) -> RunCompactInputs:
        from app.models.llm import LLMModel

        return RunCompactInputs(
            model=LLMModel(
                id=uuid.uuid4(),
                tenant_id=None,
                provider="openai",
                model="compact-model",
                label="Compact",
                api_key_encrypted="encrypted",
                enabled=True,
                max_input_tokens=100_000,
                max_output_tokens=256,
            ),
            ledger={},
            effective_input_budget=1000,
            current_input_tokens=900,
            executions=[],
        )

    from app.config import Settings

    return RuntimeRunCompactorService(
        settings=Settings(_env_file=None),
        completion=completion,
        input_loader=load,
    )
