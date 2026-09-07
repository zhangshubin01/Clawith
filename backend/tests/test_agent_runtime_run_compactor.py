"""Frozen D-016 Thread Running Summary and semantic-boundary tests."""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any, cast

import pytest

from app.config import Settings
from app.models.llm import LLMModel
from app.services.agent_runtime.model_capabilities import ModelCapabilityError
from app.services.agent_runtime.run_compactor import (
    _COMPACTION_INSTRUCTION,
    CompactHistoryMessage,
    CompactRequestShape,
    RunCompactInputs,
    RunCompactorError,
    RuntimeRunCompactorService,
    TransientRunCompactorError,
    _compact_messages,
    _prior_run_covered_note,
    settle_step_messages,
)
from app.services.agent_runtime.thread_visibility import bound_current_run_window
from app.services.llm.client import LLMMessage
from app.services.agent_runtime.state import (
    JsonObject,
    RunInputSnapshots,
    RunRegistrySnapshot,
    RuntimeContext,
    RuntimeGraphState,
)
from app.services.llm.single_step import LLMCompletionStep
from app.services.llm.finish import FINISH_PROTOCOL_REMINDER
from app.services.token_tracker import TokenUsage


_TINY_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/"
    "x8AAusB9Wl2ZQAAAABJRU5ErkJggg=="
)


def _settings() -> Settings:
    return Settings(_env_file=None)


def _model(tenant_id: uuid.UUID, *, input_tokens: int = 100_000) -> LLMModel:
    return LLMModel(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        provider="openai",
        model="compact-model",
        label="Compact",
        api_key_encrypted="encrypted",
        enabled=True,
        max_input_tokens=input_tokens,
        max_output_tokens=256,
    )


def _normal(message_id: str, content: str | None = None) -> JsonObject:
    return {
        "id": message_id,
        "role": "user",
        "content": content or message_id,
    }


def _assistant(message_id: str, call_id: str) -> JsonObject:
    return {
        "id": message_id,
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }
        ],
    }


def _tool_result(
    message_id: str,
    call_id: str,
    *,
    content: str = "result",
) -> JsonObject:
    return {
        "id": message_id,
        "role": "tool",
        "tool_call_id": call_id,
        "content": content,
    }


def _state(messages: list[JsonObject]) -> tuple[RuntimeGraphState, RuntimeContext, uuid.UUID]:
    tenant_id = uuid.uuid4()
    run_id = uuid.uuid4()
    current = next(
        (
            message
            for message in reversed(messages)
            if message.get("runtime_input") == "current"
        ),
        messages[-1],
    )
    registry = RunRegistrySnapshot(
        tenant_id=str(tenant_id),
        run_id=str(run_id),
        goal="Complete the work",
        run_kind="foreground",
        source_type="chat",
        model_id=str(uuid.uuid4()),
        graph_name="runtime_graph",
        graph_version="v1",
        agent_id=str(uuid.uuid4()),
    )
    state: RuntimeGraphState = {
        "registry": registry,
        "snapshots": RunInputSnapshots(
            session_context={"version": 0},
            session_context_version=0,
            recent_session_messages=(),
            related_run_summaries=(),
            initial_input={
                "message_id": current["id"],
                "input_content": current["content"],
            },
        ),
        "messages": messages,  # type: ignore[typeddict-item]
        "lifecycle": {
            "status": "running",
            "next_route": "compact",
            "pending_tool_calls": [],
        },
    }
    context = RuntimeContext(
        tenant_id=str(tenant_id),
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
    return state, context, tenant_id


def _step(**overrides: str) -> LLMCompletionStep:
    sections = {
        "Primary Request and Intent": "Complete the work accurately",
        "Key Technical Concepts": "Runtime compact internals",
        "Files and Code": "run_compactor.py",
        "Errors and Fixes": "No errors",
        "Pending Jobs": "None",
        "Current Work": "Compacting completed history",
        "Next Step": "Answer the exact current request",
        "Critical Context": "Use the durable receipt",
        **overrides,
    }
    return LLMCompletionStep(
        content="\n\n".join(
            f"## {heading}\n{value}" for heading, value in sections.items()
        ),
        tool_calls=(),
        reasoning_content=None,
        retry_instruction=None,
        usage=TokenUsage(total_tokens=10),
    )


def _request_shape(messages: list[JsonObject]) -> CompactRequestShape:
    """A minimal cache-stable prefix reconstructed from the state messages."""
    history = tuple(
        CompactHistoryMessage(
            message=LLMMessage(
                role=cast(Any, message.get("role")),
                content=message.get("content"),
            ),
            state_message_id=(
                message.get("id") if isinstance(message.get("id"), str) else None
            ),
        )
        for message in messages
        if message.get("role") in {"user", "assistant", "tool"}
    )
    return CompactRequestShape(
        system_content="test-system-prompt",
        provider_tools=(),
        history=history,
    )


async def _collapsed_request_shape(
    messages: list[JsonObject],
    *,
    current_run_id: str,
) -> CompactRequestShape:
    """Rebuild the cache-stable prefix the way the live request does for a
    multi-Run Thread: ``bound_current_run_window`` collapses prior-Run messages
    into one ``prior-run-summary:{run_id}`` note, and the current-Run window
    stays raw."""
    prior_run_summary, current = await bound_current_run_window(
        messages,
        current_run_id=current_run_id,
    )
    history: list[CompactHistoryMessage] = []
    if prior_run_summary is not None:
        history.append(
            CompactHistoryMessage(
                message=LLMMessage(
                    role="user",
                    content=cast(str, prior_run_summary.get("content")),
                ),
                state_message_id=prior_run_summary.get("id"),
            )
        )
    for message in current:
        role = message.get("role")
        if role not in {"user", "assistant", "tool"}:
            continue
        history.append(
            CompactHistoryMessage(
                message=LLMMessage(
                    role=cast(Any, role),
                    content=message.get("content"),
                ),
                state_message_id=(
                    message.get("id") if isinstance(message.get("id"), str) else None
                ),
            )
        )
    return CompactRequestShape(
        system_content="test-system-prompt",
        provider_tools=(),
        history=tuple(history),
    )


def _serialize_prompt(messages: list[LLMMessage]) -> str:
    return json.dumps(
        [message.to_openai_format() for message in messages],
        ensure_ascii=False,
    )


def _service(
    *,
    model: LLMModel,
    completion,
    effective_budget: int,
    current_tokens: int,
    ledger: dict | None = None,
) -> RuntimeRunCompactorService:
    async def load(
        state: RuntimeGraphState,
        _context: RuntimeContext,
    ) -> RunCompactInputs:
        return RunCompactInputs(
            model=model,
            ledger=ledger or {},
            effective_input_budget=effective_budget,
            current_input_tokens=current_tokens,
            request_shape=_request_shape(list(state["messages"])),  # type: ignore[typeddict-item]
        )

    return RuntimeRunCompactorService(
        settings=_settings(),
        completion=completion,
        input_loader=load,
    )


@pytest.mark.asyncio
async def test_below_eighty_percent_skips_compact() -> None:
    messages = [_normal("old"), _normal("current")]
    state, context, tenant_id = _state(messages)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("sub-80% request must not call the compact model")

    result = await _service(
        model=_model(tenant_id),
        completion=forbidden,
        effective_budget=1_000,
        current_tokens=799,
    ).compact_if_needed(state, context)

    assert result.compacted is False


@pytest.mark.asyncio
async def test_missing_complete_business_request_budget_fails_closed() -> None:
    state, context, tenant_id = _state(
        [_normal("old", "old " * 300), _normal("current")]
    )

    async def load(
        _state: RuntimeGraphState,
        _context: RuntimeContext,
    ) -> RunCompactInputs:
        return RunCompactInputs(model=_model(tenant_id), ledger={})

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("missing request budget must fail before model use")

    service = RuntimeRunCompactorService(
        settings=_settings(),
        completion=forbidden,
        input_loader=load,
    )

    with pytest.raises(RunCompactorError) as raised:
        await service.compact_if_needed(state, context)

    assert raised.value.code == "missing_request_budget"


@pytest.mark.asyncio
async def test_invalid_request_budget_from_input_loader_is_deterministic() -> None:
    state, context, _tenant_id = _state([_normal("current")])

    async def load(
        _state: RuntimeGraphState,
        _context: RuntimeContext,
    ) -> RunCompactInputs:
        raise ModelCapabilityError(
            "invalid_request_budget",
            "requested output tokens leave no room in the shared context window",
        )

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("invalid request budget must fail before model use")

    service = RuntimeRunCompactorService(
        settings=_settings(),
        completion=forbidden,
        input_loader=load,
    )

    with pytest.raises(RunCompactorError) as raised:
        await service.compact_if_needed(state, context)

    assert raised.value.code == "invalid_request_budget"
    assert raised.value.is_deterministic_compact_error is True


@pytest.mark.asyncio
async def test_invalid_compact_model_budget_is_a_deterministic_runtime_error() -> None:
    state, context, tenant_id = _state(
        [_normal("old", "old " * 300), _normal("current")]
    )
    model = _model(tenant_id)
    model.max_input_tokens = None
    model.context_window_tokens_override = 250

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("invalid compact budget must fail before model use")

    with pytest.raises(RunCompactorError) as raised:
        await _service(
            model=model,
            completion=forbidden,
            effective_budget=1_000,
            current_tokens=800,
        ).compact_if_needed(state, context)

    assert raised.value.code == "invalid_request_budget"
    assert raised.value.is_deterministic_compact_error is True


@pytest.mark.asyncio
async def test_at_eighty_percent_compacts_prefix_and_keeps_current_input_exact() -> None:
    messages = [
        *[_normal(f"old-{index}", "old history " * 12) for index in range(8)],
        {
            **_normal("current", "EXACT CURRENT INPUT"),
            "runtime_input": "current",
        },
    ]
    state, context, tenant_id = _state(messages)
    observed_tools: list[dict] | None = None

    async def complete(*_args, **kwargs):
        nonlocal observed_tools
        observed_tools = kwargs.get("tools")
        return _step()

    result = await _service(
        model=_model(tenant_id),
        completion=complete,
        effective_budget=1_000,
        current_tokens=800,
    ).compact_if_needed(state, context)

    assert result.compacted is True
    assert result.thread_summary is not None
    assert result.thread_summary["format"] == "thread_running_summary_markdown_v1"
    assert "## Primary Request and Intent" in result.thread_summary["text"]
    assert observed_tools == []
    assert result.recent_messages is not None
    assert result.recent_messages[-1]["content"] == "EXACT CURRENT INPUT"
    assert result.recent_messages[-1]["runtime_input"] == "current"
    assert result.covered_through_message_id != "current"


@pytest.mark.asyncio
async def test_compact_completion_requests_thinking_disabled() -> None:
    """DeepSeek thinking shares the output budget with the summary and must
    be switched off on the auxiliary compaction call (probe-verified)."""
    messages = [
        *[_normal(f"old-{index}", "old history " * 12) for index in range(8)],
        {
            **_normal("current", "EXACT CURRENT INPUT"),
            "runtime_input": "current",
        },
    ]
    state, context, tenant_id = _state(messages)
    observed_thinking_disabled: bool | None = None

    async def complete(*_args, **kwargs):
        nonlocal observed_thinking_disabled
        observed_thinking_disabled = kwargs.get("thinking_disabled")
        return _step()

    result = await _service(
        model=_model(tenant_id),
        completion=complete,
        effective_budget=1_000,
        current_tokens=800,
    ).compact_if_needed(state, context)

    assert result.compacted is True
    assert observed_thinking_disabled is True


@pytest.mark.asyncio
async def test_large_image_base64_is_budgeted_by_context_tokens_not_char_count() -> None:
    padded_png = base64.b64encode(
        base64.b64decode(_TINY_PNG_BASE64) + b"x" * (64 * 1024)
    ).decode("ascii")
    marker = f"[image_data:data:image/png;base64,{padded_png}] inspect"
    messages = [
        _normal("old", "old completed history " * 300),
        {
            **_normal("current", marker),
            "runtime_input": "current",
        },
    ]
    state, context, tenant_id = _state(messages)
    prompts: list[list] = []

    async def complete(_model, prompt, **_kwargs):
        prompts.append(prompt)
        return _step()

    result = await _service(
        model=_model(tenant_id),
        completion=complete,
        effective_budget=1_000,
        current_tokens=900,
    ).compact_if_needed(state, context)

    assert result.compacted is True
    assert result.recent_messages is not None
    assert result.recent_messages[-1]["content"] == marker
    # F2 replays the exact input in main-request form (no image-omission
    # downgrade); the 64KB base64 is token-estimated via context tokens, so it
    # must not blow the recent budget and the compaction must succeed.
    serialized = _serialize_prompt(prompts[0])
    assert "image_data" in serialized


@pytest.mark.asyncio
async def test_long_single_run_compacts_safe_work_after_exact_current_input() -> None:
    messages = [
        {
            **_normal("current", "EXACT CURRENT INPUT"),
            "runtime_input": "current",
        },
        _normal("completed-work", "completed work " * 300),
        _normal("recent", "recent result"),
    ]
    state, context, tenant_id = _state(messages)
    prompts: list[list] = []

    async def complete(_model, prompt, **_kwargs):
        prompts.append(prompt)
        return _step()

    result = await _service(
        model=_model(tenant_id),
        completion=complete,
        effective_budget=1_000,
        current_tokens=900,
    ).compact_if_needed(state, context)

    assert result.compacted is True
    assert result.covered_through_message_id == "completed-work"
    assert result.recent_messages is not None
    assert [message["id"] for message in result.recent_messages] == [
        "current",
        "recent",
    ]
    assert result.recent_messages[0]["content"] == "EXACT CURRENT INPUT"
    # The exact current input is replayed verbatim in the compact request.
    assert any(
        message.role == "user" and message.content == "EXACT CURRENT INPUT"
        for message in prompts[0]
    )


@pytest.mark.asyncio
async def test_prior_run_input_marker_does_not_pin_current_run_compact() -> None:
    messages = [
        {
            **_normal("prior-run-input", "prior input " * 300),
            "runtime_input": "current",
            "runtime_run_id": str(uuid.uuid4()),
        },
        {
            **_normal("current", "EXACT CURRENT INPUT"),
            "runtime_input": "current",
        },
    ]
    state, context, tenant_id = _state(messages)

    async def complete(*_args, **_kwargs):
        return _step()

    result = await _service(
        model=_model(tenant_id),
        completion=complete,
        effective_budget=1_000,
        current_tokens=900,
    ).compact_if_needed(state, context)

    assert result.covered_through_message_id == "prior-run-input"
    assert result.recent_messages is not None
    assert [message["id"] for message in result.recent_messages] == ["current"]


@pytest.mark.asyncio
async def test_prior_run_plain_candidates_and_repairs_never_enter_compact_summary() -> None:
    prior_run_id = str(uuid.uuid4())
    messages = [
        {
            **_normal("prior-input", "prior input " * 300),
            "runtime_input": "current",
            "runtime_run_id": prior_run_id,
        },
        {
            "id": "prior-draft",
            "role": "assistant",
            "content": "PRIVATE REPLACED DRAFT",
            "runtime_run_id": prior_run_id,
        },
        {
            "id": "prior-repair",
            "role": "user",
            "content": FINISH_PROTOCOL_REMINDER,
            "runtime_intent": "repair",
            "runtime_run_id": prior_run_id,
        },
        {
            "id": "prior-finish-candidate",
            "role": "assistant",
            "content": "THREAD TERMINAL CANDIDATE",
            "runtime_intent": "finish",
            "runtime_run_id": prior_run_id,
        },
        {
            **_normal("current", "EXACT CURRENT INPUT"),
            "runtime_input": "current",
        },
    ]
    state, context, tenant_id = _state(messages)
    state["messages"][-1]["runtime_run_id"] = context.run_id  # type: ignore[index]
    prompts: list[list] = []

    async def complete(_model, prompt, **_kwargs):
        prompts.append(prompt)
        return _step()

    result = await _service(
        model=_model(tenant_id),
        completion=complete,
        effective_budget=1_000,
        current_tokens=900,
    ).compact_if_needed(state, context)

    serialized_payload = _serialize_prompt(prompts[0])
    assert "PRIVATE REPLACED DRAFT" not in serialized_payload
    assert "THREAD TERMINAL CANDIDATE" not in serialized_payload
    assert FINISH_PROTOCOL_REMINDER not in serialized_payload
    assert result.recent_messages is not None
    recent_contents = [str(message.get("content", "")) for message in result.recent_messages]
    assert "PRIVATE REPLACED DRAFT" not in recent_contents
    assert "THREAD TERMINAL CANDIDATE" not in recent_contents
    assert FINISH_PROTOCOL_REMINDER not in recent_contents


@pytest.mark.asyncio
async def test_current_run_repair_state_stays_raw_but_out_of_compact_prompt() -> None:
    messages = [
        _normal("old-safe", "old completed history " * 300),
        {
            **_normal("current", "EXACT CURRENT INPUT"),
            "runtime_input": "current",
        },
        {
            "id": "current-draft",
            "role": "assistant",
            "content": "CURRENT PRIVATE DRAFT",
            "runtime_intent": "repair_draft",
        },
        {
            "id": "current-repair",
            "role": "user",
            "content": FINISH_PROTOCOL_REMINDER,
            "runtime_intent": "repair",
        },
    ]
    state, context, tenant_id = _state(messages)
    for message in state["messages"][1:]:  # type: ignore[index]
        message["runtime_run_id"] = context.run_id
    prompts: list[list] = []

    async def complete(_model, prompt, **_kwargs):
        prompts.append(prompt)
        return _step()

    result = await _service(
        model=_model(tenant_id),
        completion=complete,
        effective_budget=1_000,
        current_tokens=900,
    ).compact_if_needed(state, context)

    assert result.recent_messages is not None
    assert [message["id"] for message in result.recent_messages] == [
        "current",
        "current-draft",
        "current-repair",
    ]
    # Only the current input is replayed into the compact request; the repair
    # draft and repair reminder stay raw (retained) but out of the prompt.
    serialized_prompt = _serialize_prompt(prompts[0])
    assert "EXACT CURRENT INPUT" in serialized_prompt
    assert "CURRENT PRIVATE DRAFT" not in serialized_prompt
    assert FINISH_PROTOCOL_REMINDER not in serialized_prompt


@pytest.mark.asyncio
async def test_current_run_resume_input_remains_exact_across_later_compact() -> None:
    messages = [
        {
            **_normal("current", "EXACT CURRENT INPUT"),
            "runtime_input": "current",
        },
        _normal("before-resume", "completed before resume " * 160),
        {
            **_normal("resume", "EXACT RESUME INPUT"),
            "runtime_input": "resume",
        },
        _normal("after-resume", "completed after resume " * 160),
        _normal("recent", "recent result"),
    ]
    state, context, tenant_id = _state(messages)
    state["messages"][2]["runtime_run_id"] = context.run_id  # type: ignore[index]

    async def complete(*_args, **_kwargs):
        return _step()

    result = await _service(
        model=_model(tenant_id),
        completion=complete,
        effective_budget=1_000,
        current_tokens=900,
    ).compact_if_needed(state, context)

    assert result.covered_through_message_id == "after-resume"
    assert result.recent_messages is not None
    assert [message["id"] for message in result.recent_messages] == [
        "current",
        "resume",
        "recent",
    ]
    assert result.recent_messages[1]["content"] == "EXACT RESUME INPUT"


@pytest.mark.asyncio
async def test_generated_current_message_id_is_protected_by_run_identity() -> None:
    messages = [
        {
            **_normal("generated-current", "EXACT GENERATED INPUT"),
            "runtime_input": "current",
        },
        _normal("completed-work", "completed work " * 300),
        _normal("recent", "recent result"),
    ]
    state, context, tenant_id = _state(messages)
    state["messages"][0]["runtime_run_id"] = context.run_id  # type: ignore[index]
    state["snapshots"] = RunInputSnapshots(
        session_context={"version": 0},
        session_context_version=0,
        recent_session_messages=(),
        related_run_summaries=(),
        initial_input={"input_content": "EXACT GENERATED INPUT"},
    )

    async def complete(*_args, **_kwargs):
        return _step()

    result = await _service(
        model=_model(tenant_id),
        completion=complete,
        effective_budget=1_000,
        current_tokens=900,
    ).compact_if_needed(state, context)

    assert result.recent_messages is not None
    assert [message["id"] for message in result.recent_messages] == [
        "generated-current",
        "recent",
    ]


@pytest.mark.asyncio
async def test_started_exchange_is_retained_and_never_crossed() -> None:
    messages = [
        _normal("old-safe", "old " * 300),
        _assistant("assistant-pending", "call-pending"),
        {**_normal("current", "exact"), "runtime_input": "current"},
    ]
    state, context, tenant_id = _state(messages)

    async def complete(*_args, **_kwargs):
        return _step()

    result = await _service(
        model=_model(tenant_id),
        completion=complete,
        effective_budget=1_000,
        current_tokens=900,
        ledger={"call-pending": {"status": "started"}},
    ).compact_if_needed(state, context)

    assert result.covered_through_message_id == "old-safe"
    assert result.recent_messages is not None
    assert [message["id"] for message in result.recent_messages] == [
        "assistant-pending",
        "current",
    ]


@pytest.mark.asyncio
async def test_cancelled_not_started_exchange_can_enter_summary() -> None:
    messages = [
        _normal("old-safe", "old " * 300),
        _assistant("assistant-cancelled", "call-cancelled"),
        {**_normal("current", "exact"), "runtime_input": "current"},
    ]
    state, context, tenant_id = _state(messages)

    async def complete(*_args, **_kwargs):
        return _step()

    result = await _service(
        model=_model(tenant_id),
        completion=complete,
        effective_budget=1_000,
        current_tokens=900,
        ledger={
            "call-cancelled": {
                "status": "not_started",
                "tool_name": "lookup",
                "cancelled_before_execution": True,
                "may_have_side_effect": False,
            }
        },
    ).compact_if_needed(state, context)

    assert result.covered_through_message_id == "assistant-cancelled"
    assert result.recent_messages is not None
    assert [message["id"] for message in result.recent_messages] == ["current"]


@pytest.mark.asyncio
async def test_oversized_settled_exchange_enters_summary_as_facts_and_refs() -> None:
    ledger = {
        "call-1": {
            "status": "succeeded",
            "tool_name": "lookup",
            "result_summary": "found the answer",
            "result_ref": "result://call-1",
            "request_ref": "request://call-1",
        }
    }
    messages = [
        _assistant("assistant-tools", "call-1"),
        _tool_result("result-1", "call-1", content="x" * 30_000),
        {**_normal("current", "exact"), "runtime_input": "current"},
    ]
    # Production order: the executor settles the completed exchange into a
    # deterministic synthetic message before Thread Compact; the covered span
    # then replays that synthetic raw (no re-synthesis in the compact prompt).
    settlement = settle_step_messages(
        messages,
        ledger,
        effective_input_budget=1_000,
        current_input_id="current",
        current_run_id=str(uuid.uuid4()),
    )
    state, context, tenant_id = _state(list(settlement.messages))
    prompts: list[list] = []

    async def complete(_model, prompt, **_kwargs):
        prompts.append(prompt)
        # A short summary keeps the shrink check (summary < covered synthetic)
        # and the summary budget satisfied while the synthetic stays covered.
        return _step(
            **{
                "Primary Request and Intent": "go",
                "Key Technical Concepts": "x",
                "Files and Code": "f",
                "Errors and Fixes": "none",
                "Pending Jobs": "none",
                "Current Work": "w",
                "Next Step": "n",
                "Critical Context": "c",
            }
        )

    result = await _service(
        model=_model(tenant_id, input_tokens=5_000),
        completion=complete,
        effective_budget=400,
        current_tokens=360,
        ledger=ledger,
    ).compact_if_needed(state, context)

    assert result.compacted is True
    serialized = _serialize_prompt(prompts[0])
    assert "historical_tool_exchange" in serialized
    assert "result://call-1" in serialized
    assert "request://call-1" in serialized
    assert "x" * 1_000 not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_error",
    [
        TimeoutError("provider network timeout"),
        RuntimeError("HTTP 429 Too Many Requests"),
        RuntimeError("HTTP 503 Service Unavailable"),
    ],
)
async def test_transient_provider_failure_is_typed_for_langgraph_retry(
    provider_error: Exception,
) -> None:
    state, context, tenant_id = _state(
        [_normal("old", "old " * 300), _normal("current")]
    )
    calls = 0

    async def complete(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise provider_error

    with pytest.raises(TransientRunCompactorError) as raised:
        await _service(
            model=_model(tenant_id),
            completion=complete,
            effective_budget=1_000,
            current_tokens=900,
        ).compact_if_needed(state, context)

    assert raised.value.is_transient_compact_error is True
    assert calls == 1


@pytest.mark.asyncio
async def test_unknown_provider_failure_is_typed_for_langgraph_retry() -> None:
    state, context, tenant_id = _state(
        [_normal("old", "old " * 300), _normal("current")]
    )

    calls = 0

    async def complete(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise json.JSONDecodeError("Expecting value", "", 0)

    with pytest.raises(TransientRunCompactorError) as raised:
        await _service(
            model=_model(tenant_id),
            completion=complete,
            effective_budget=1_000,
            current_tokens=900,
        ).compact_if_needed(state, context)

    assert raised.value.is_transient_compact_error is True
    assert calls == 1


@pytest.mark.asyncio
async def test_invalid_summary_uses_deterministic_degraded_checkpoint() -> None:
    state, context, tenant_id = _state(
        [_normal("old", "old " * 300), _normal("current")]
    )

    async def complete(*_args, **_kwargs):
        return LLMCompletionStep(
            content="   ",
            tool_calls=(),
            reasoning_content=None,
            retry_instruction=None,
            usage=TokenUsage(total_tokens=1),
        )

    result = await _service(
        model=_model(tenant_id),
        completion=complete,
        effective_budget=1_000,
        current_tokens=900,
    ).compact_if_needed(state, context)

    assert result.compacted is True
    assert result.thread_summary is not None
    assert result.thread_summary["degraded"] is True


@pytest.mark.asyncio
async def test_summary_over_4096_tokens_is_rejected() -> None:
    state, context, tenant_id = _state(
        [_normal("old", "old " * 15_000), _normal("current")]
    )

    async def complete(*_args, **_kwargs):
        return _step(**{"Files and Code": "x" * 20_000})

    with pytest.raises(RunCompactorError) as raised:
        await _service(
            model=_model(tenant_id, input_tokens=100_000),
            completion=complete,
            effective_budget=100_000,
            current_tokens=80_000,
        ).compact_if_needed(state, context)

    assert raised.value.code == "thread_summary_exceeds_budget"


@pytest.mark.asyncio
async def test_compact_request_output_is_capped_by_summary_budget() -> None:
    state, context, tenant_id = _state(
        [_normal("old", "old " * 15_000), _normal("current")]
    )
    model = _model(tenant_id, input_tokens=100_000)
    model.max_output_tokens = 32_000
    observed_limits: list[int | None] = []

    async def complete(*_args, **kwargs):
        observed_limits.append(kwargs.get("max_output_tokens"))
        return _step()

    result = await _service(
        model=model,
        completion=complete,
        effective_budget=100_000,
        current_tokens=80_000,
    ).compact_if_needed(state, context)

    assert result.compacted is True
    assert observed_limits
    assert set(observed_limits) == {10_922}


@pytest.mark.asyncio
async def test_compact_request_output_respects_lower_model_limit() -> None:
    state, context, tenant_id = _state(
        [_normal("old", "old " * 15_000), _normal("current")]
    )
    model = _model(tenant_id, input_tokens=100_000)
    model.max_output_tokens = 256
    observed_limits: list[int | None] = []

    async def complete(*_args, **kwargs):
        observed_limits.append(kwargs.get("max_output_tokens"))
        return _step()

    result = await _service(
        model=model,
        completion=complete,
        effective_budget=100_000,
        current_tokens=80_000,
    ).compact_if_needed(state, context)

    assert result.compacted is True
    assert observed_limits
    assert set(observed_limits) == {256}


@pytest.mark.asyncio
async def test_length_output_splits_batch_instead_of_repeating_same_prompt() -> None:
    state, context, tenant_id = _state(
        [
            _normal("old-1", "old one " * 200),
            _normal("old-2", "old two " * 200),
            _normal("current"),
        ]
    )
    responses = [
        LLMCompletionStep(
            content="partial summary",
            tool_calls=(),
            reasoning_content=None,
            retry_instruction=None,
            usage=TokenUsage(total_tokens=1),
            finish_reason="length",
        ),
        _step(),
        _step(),
    ]
    prompts: list[list] = []

    async def complete(_model, messages, **_kwargs):
        prompts.append(messages)
        return responses.pop(0)

    result = await _service(
        model=_model(tenant_id),
        completion=complete,
        effective_budget=1_000,
        current_tokens=900,
    ).compact_if_needed(state, context)

    assert result.compacted is True
    assert len(prompts) == 3
    assert prompts[0] != prompts[1]
    assert prompts[0] != prompts[2]


@pytest.mark.asyncio
async def test_compaction_instruction_is_final_user_message_after_system_prefix() -> None:
    state, context, tenant_id = _state(
        [_normal("old", "old " * 300), _normal("current")]
    )
    prompts: list[list] = []

    async def complete(_model, messages, **_kwargs):
        prompts.append(messages)
        return _step()

    result = await _service(
        model=_model(tenant_id),
        completion=complete,
        effective_budget=1_000,
        current_tokens=900,
    ).compact_if_needed(state, context)

    assert result.compacted is True
    assert len(prompts) == 1
    messages = prompts[0]
    # F2 keeps the byte-identical system message first; the instruction is
    # still the final user message.
    assert messages[0].role == "system"
    assert messages[-1].role == "user"
    instruction = messages[-1].content
    assert isinstance(instruction, str)
    for section in (
        "## Primary Request and Intent",
        "## Key Technical Concepts",
        "## Files and Code",
        "## Errors and Fixes",
        "## Pending Jobs",
        "## Current Work",
        "## Next Step",
        "## Critical Context",
    ):
        assert section in instruction
    assert "stuck loop" in instruction
    assert "Next Step never controls Runtime routing" in instruction


def test_compaction_instruction_forbids_corrective_rewrite_of_authorized_inputs() -> None:
    """Primary Request and Intent must preserve authorization verbatim.

    Regression contract for run 764eb591: the summary LLM "corrected" the
    user's authorized wording ("均等创建者指示" → "均待创建者指示") and invented
    "no authorization was granted". The instruction must hard-require verbatim
    reproduction of ``authoritative_exact_inputs``, forbid corrective rewrites,
    and make the original wording win over any prior judgment.
    """
    instruction = _COMPACTION_INSTRUCTION

    # Hard rule: verbatim copy of the authoritative exact inputs.
    assert "authoritative_exact_inputs" in instruction
    assert "VERBATIM" in instruction
    # Hard rule: the original wording wins over any prior judgment.
    assert "wins over" in instruction
    # Hard rule: suspected typos are preserved, not corrected.
    assert "原文如此" in instruction
    # Forbidden judgment sentences must be quoted as counter-examples.
    assert "mis-transcription" in instruction
    assert "No ... authorization was granted" in instruction
    assert "未获授权" in instruction


def test_compaction_instruction_treats_completed_actions_as_settled_ledger_facts() -> None:
    """completed_actions entries are settled ledger facts, not speculation.

    Regression contract for run 764eb591: the summary/resume model opposed a
    "DONE" entry (a settled ledger fact) against a later read showing old
    content and declared its own completed work "maybe not done", then redid it
    from scratch 19 times. The instruction must hard-require the triple
    semantics: DONE = settled ledger fact; read does not contradict a settled
    DONE; and a conflict is annotated for verification — never claimed as
    not-done / hallucination / from-scratch, never silently sided with either.
    """
    instruction = _COMPACTION_INSTRUCTION

    # DONE = settled ledger fact, not a guess or hallucination.
    assert "DONE = settled ledger fact" in instruction
    # read of old content is a current-disk-state fact, not a contradiction.
    assert "read does not contradict a settled DONE" in instruction
    # a conflict is annotated for verification, never re-done from scratch.
    assert "annotate" in instruction
    assert "not-done" in instruction
    assert "hallucination" in instruction
    assert "from-scratch" in instruction


def test_compaction_instruction_does_not_silently_side_with_done_or_read() -> None:
    """The instruction must not re-introduce a silent taking-sides clause.

    The corrected frame is "annotate the conflict, never claim not-done /
    hallucination / from-scratch" — neither "believe DONE over read" nor
    "believe read over DONE". Guard against the read-vs-pipeline taking-sides
    sentence sneaking back in.
    """
    instruction = _COMPACTION_INSTRUCTION

    # No silent "pipeline wins over read" (read-vs-pipeline taking sides).
    assert "pipeline wins over read" not in instruction
    # No silent "believe completed_actions" (以 completed_actions 为准).
    assert "以 completed_actions 为准" not in instruction
    # The old "prefer it over re-deriving outcomes from raw history" framing is
    # gone — that was the read-vs-pipeline taking-sides sentence.
    assert "prefer it over re-deriving" not in instruction


@pytest.mark.asyncio
async def test_shrink_failure_splits_batch_instead_of_repeating_same_prompt() -> None:
    state, context, tenant_id = _state(
        [
            _normal("old-1", "old one " * 200),
            _normal("old-2", "old two " * 200),
            _normal("current"),
        ]
    )
    responses = [
        LLMCompletionStep(
            content="x" * 4_000,
            tool_calls=(),
            reasoning_content=None,
            retry_instruction=None,
            usage=TokenUsage(total_tokens=1),
        ),
        _step(),
        _step(),
    ]
    prompts: list[list] = []

    async def complete(_model, messages, **_kwargs):
        prompts.append(messages)
        return responses.pop(0)

    result = await _service(
        model=_model(tenant_id),
        completion=complete,
        effective_budget=1_000,
        current_tokens=900,
    ).compact_if_needed(state, context)

    assert result.compacted is True
    assert len(prompts) == 3
    assert prompts[0] != prompts[1]
    assert prompts[0] != prompts[2]
    assert result.thread_summary is not None
    assert result.thread_summary.get("degraded") is not True


@pytest.mark.asyncio
async def test_shrink_failure_single_block_degrades_with_shrink_failed_flag() -> None:
    state, context, tenant_id = _state(
        [_normal("old", "x" * 1_500), _normal("current")]
    )
    calls = 0

    async def complete(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _step(**{"Files and Code": "x" * 2_000})

    result = await _service(
        model=_model(tenant_id),
        completion=complete,
        effective_budget=1_000,
        current_tokens=900,
    ).compact_if_needed(state, context)

    assert calls == 1
    assert result.compacted is True
    assert result.thread_summary is not None
    assert result.thread_summary["degraded"] is True
    assert result.thread_summary["shrink_failed"] is True
    assert result.thread_summary["reason"] == "model_summary_incomplete"


@pytest.mark.asyncio
async def test_unstructured_output_degrades_single_block() -> None:
    state, context, tenant_id = _state(
        [_normal("old", "old " * 300), _normal("current")]
    )
    calls = 0

    async def complete(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return LLMCompletionStep(
            content=(
                "## Primary Request and Intent\nKeep going\n\n"
                "## Errors and Fixes\nNo errors"
            ),
            tool_calls=(),
            reasoning_content=None,
            retry_instruction=None,
            usage=TokenUsage(total_tokens=1),
        )

    result = await _service(
        model=_model(tenant_id),
        completion=complete,
        effective_budget=1_000,
        current_tokens=900,
    ).compact_if_needed(state, context)

    assert calls == 1
    assert result.compacted is True
    assert result.thread_summary is not None
    assert result.thread_summary["degraded"] is True
    assert result.thread_summary.get("shrink_failed") is not True


@pytest.mark.asyncio
async def test_unstructured_output_splits_batch_instead_of_degrading() -> None:
    state, context, tenant_id = _state(
        [
            _normal("old-1", "old one " * 200),
            _normal("old-2", "old two " * 200),
            _normal("current"),
        ]
    )
    responses = [
        LLMCompletionStep(
            content=(
                "## Primary Request and Intent\nKeep going\n\n"
                "## Key Technical Concepts\npython\n\n"
                "## Files and Code\nrun_compactor.py\n\n"
                "## Errors and Fixes\nNo errors"
            ),
            tool_calls=(),
            reasoning_content=None,
            retry_instruction=None,
            usage=TokenUsage(total_tokens=1),
        ),
        _step(),
        _step(),
    ]
    prompts: list[list] = []

    async def complete(_model, messages, **_kwargs):
        prompts.append(messages)
        return responses.pop(0)

    result = await _service(
        model=_model(tenant_id),
        completion=complete,
        effective_budget=1_000,
        current_tokens=900,
    ).compact_if_needed(state, context)

    assert result.compacted is True
    assert len(prompts) == 3
    assert result.thread_summary is not None
    assert result.thread_summary.get("degraded") is not True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "finish_reason"),
    [
        ("   ", "stop"),
        ("partial summary", "length"),
    ],
)
async def test_repairable_single_block_output_degrades_without_terminating_run(
    content: str,
    finish_reason: str,
) -> None:
    state, context, tenant_id = _state(
        [_normal("old", "old " * 300), _normal("current")]
    )
    calls = 0

    async def complete(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return LLMCompletionStep(
            content=content,
            tool_calls=(),
            reasoning_content=None,
            retry_instruction=None,
            usage=TokenUsage(total_tokens=1),
            finish_reason=finish_reason,
        )

    result = await _service(
        model=_model(tenant_id),
        completion=complete,
        effective_budget=1_000,
        current_tokens=900,
    ).compact_if_needed(state, context)

    assert calls == 1
    assert result.compacted is True
    assert result.thread_summary is not None
    assert result.thread_summary["degraded"] is True
    assert result.thread_summary["reason"] == "model_summary_incomplete"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("finish_reason", "tool_calls"),
    [
        ("content_filter", ()),
        ("refusal", ()),
        ("unknown", ()),
        (
            "tool_calls",
            (
                {
                    "id": "unexpected",
                    "type": "function",
                    "function": {"name": "unexpected", "arguments": "{}"},
                },
            ),
        ),
    ],
)
async def test_nonrepairable_compact_outputs_are_rejected_atomically(
    finish_reason: str,
    tool_calls: tuple[dict, ...],
) -> None:
    state, context, tenant_id = _state(
        [_normal("old", "old " * 300), _normal("current")]
    )
    calls = 0

    async def complete(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return LLMCompletionStep(
            content="apparently complete summary",
            tool_calls=tool_calls,
            reasoning_content=None,
            retry_instruction=None,
            usage=TokenUsage(total_tokens=1),
            finish_reason=finish_reason,
        )

    with pytest.raises(RunCompactorError) as raised:
        await _service(
            model=_model(tenant_id),
            completion=complete,
            effective_budget=1_000,
            current_tokens=900,
        ).compact_if_needed(state, context)

    assert calls == 1
    assert raised.value.code == "invalid_thread_compact_output"
    assert "thread_summary" not in state
    assert "summary_covered_through_message_id" not in state


def test_compact_messages_single_run_is_byte_identical_without_prior_run() -> None:
    # A single-Run shape carries no ``prior-run-summary:`` entry, so the
    # prior-Run note must not be injected: the compact request is byte-identical
    # to the pre-fix order (system → covered history → exact input → instruction).
    shape = _request_shape(
        [
            _normal("old-safe", "old " * 300),
            {**_normal("current", "EXACT CURRENT INPUT"), "runtime_input": "current"},
        ]
    )
    messages = _compact_messages(
        shape,
        covered_ids=frozenset({"old-safe"}),
        summary_text=None,
        exact_ids=frozenset({"current"}),
    )
    assert [message.role for message in messages] == ["system", "user", "user", "user"]
    assert messages[1].content == "old " * 300
    assert messages[2].content == "EXACT CURRENT INPUT"
    assert messages[3].content == _COMPACTION_INSTRUCTION


def test_prior_run_covered_note_not_injected_without_orphan_covered_ids() -> None:
    # A collapsed note exists, but every covered id is representable in the
    # cache-stable history (current-Run content): no orphan → no injection.
    shape = CompactRequestShape(
        system_content="sys",
        provider_tools=(),
        history=(
            CompactHistoryMessage(
                message=LLMMessage(role="user", content="历史上下文（非当前任务）：上一轮已完成"),
                state_message_id="prior-run-summary:abc",
            ),
            CompactHistoryMessage(
                message=LLMMessage(role="user", content="old"),
                state_message_id="old-1",
            ),
            CompactHistoryMessage(
                message=LLMMessage(role="user", content="current"),
                state_message_id="current",
            ),
        ),
    )
    assert _prior_run_covered_note(shape, frozenset({"old-1"})) is None


@pytest.mark.asyncio
async def test_prior_run_is_recorded_via_collapsed_note_not_silently_lost() -> None:
    prior_run_id = str(uuid.uuid4())
    goal = "PRIOR GOAL " * 300
    raw_fact = "PRIOR RAW TOOL FACT " * 200
    messages = [
        {
            **_normal("prior-input", goal),
            "runtime_input": "current",
            "runtime_run_id": prior_run_id,
        },
        {
            **_assistant("prior-assist-1", "prior-call-1"),
            "runtime_run_id": prior_run_id,
        },
        {
            **_tool_result("prior-tool-1", "prior-call-1", content=raw_fact),
            "runtime_run_id": prior_run_id,
            "result_ref": "tool-result://prior-artifact",
        },
        {
            **_normal("current", "EXACT CURRENT INPUT"),
            "runtime_input": "current",
        },
    ]
    state, context, tenant_id = _state(messages)
    state["messages"][-1]["runtime_run_id"] = context.run_id  # type: ignore[index]
    model = _model(tenant_id)
    prompts: list[list] = []

    async def load(
        _state: RuntimeGraphState,
        _context: RuntimeContext,
    ) -> RunCompactInputs:
        shape = await _collapsed_request_shape(
            list(_state["messages"]),  # type: ignore[typeddict-item]
            current_run_id=_context.run_id,
        )
        return RunCompactInputs(
            model=model,
            ledger={},
            effective_input_budget=1_000,
            current_input_tokens=900,
            request_shape=shape,
        )

    async def complete(_model, prompt, **_kwargs):
        prompts.append(prompt)
        return _step()

    result = await RuntimeRunCompactorService(
        settings=_settings(),
        completion=complete,
        input_loader=load,
    ).compact_if_needed(state, context)

    serialized = _serialize_prompt(prompts[0])
    # The collapsed prior-Run note is re-fed (goal + artifact survive)...
    assert "历史上下文（非当前任务）" in serialized
    assert "PRIOR GOAL" in serialized
    assert "tool-result://prior-artifact" in serialized
    # ...while the prior-Run raw tool facts never leak back into the request.
    assert "PRIOR RAW TOOL FACT" not in serialized
    # The current input stays exact, the watermark crosses the prior Run, and
    # only the current-Run message survives as recent.
    assert "EXACT CURRENT INPUT" in serialized
    assert result.covered_through_message_id == "prior-tool-1"
    assert result.recent_messages is not None
    assert [message["id"] for message in result.recent_messages] == ["current"]
    assert result.thread_summary is not None
    assert "degraded" not in result.thread_summary
