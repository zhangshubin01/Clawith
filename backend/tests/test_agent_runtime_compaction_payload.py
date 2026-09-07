"""Compaction payload contract (A in payload) — S3 seam tests.

Ticket A / S3: the compaction request payload ALWAYS carries the
``completed_actions`` pipeline (empty list when nothing completed), populated
deterministically from the execution ledger, and the compaction instruction
spells out the three precedence rules that make the pipeline authoritative.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
import uuid

import pytest

from app.config import Settings
from app.models.llm import LLMModel
from app.services.agent_runtime.run_compactor import (
    CompactHistoryMessage,
    CompactRequestShape,
    RunCompactInputs,
    RuntimeRunCompactorService,
    build_completed_actions,
)
from app.services.agent_runtime.state import (
    JsonObject,
    RunInputSnapshots,
    RunRegistrySnapshot,
    RuntimeContext,
    RuntimeGraphState,
)
from app.services.llm.client import LLMMessage
from app.services.llm.single_step import LLMCompletionStep
from app.services.token_tracker import TokenUsage

_COMPACTION_SECTION_HEADINGS = (
    "## Primary Request and Intent",
    "## Key Technical Concepts",
    "## Files and Code",
    "## Errors and Fixes",
    "## Pending Jobs",
    "## Current Work",
    "## Next Step",
    "## Critical Context",
)

_PIPELINE_PRECEDENCE_SENTINELS = (
    # completed_actions entries are settled ledger facts (DONE = settled fact)
    "completed_actions lists settled ledger facts",
    # failed executions are retries, never new tasks
    "A FAILED tool execution in the history is a retry to be resolved, never a",
    # stale summaries lose against the pipeline (summary-vs-pipeline, kept)
    "the pipeline wins: correct the stale fact",
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


def _state(
    messages: list[JsonObject],
) -> tuple[RuntimeGraphState, RuntimeContext, uuid.UUID]:
    tenant_id = uuid.uuid4()
    run_id = uuid.uuid4()
    current = next(
        (message for message in reversed(messages) if message.get("runtime_input") == "current"),
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
        content="\n\n".join(f"## {heading}\n{value}" for heading, value in sections.items()),
        tool_calls=(),
        reasoning_content=None,
        retry_instruction=None,
        usage=TokenUsage(total_tokens=10),
    )


def _execution(
    *,
    execution_id: str,
    call_id: str,
    tool_name: str = "edit_file",
    status: str = "succeeded",
    path: str | None = "src/app.py",
    summary: str | None = "Replaced 1 occurrence(s).",
) -> SimpleNamespace:
    arguments = {"path": path} if path is not None else {}
    return SimpleNamespace(
        id=execution_id,
        tool_call_id=call_id,
        tool_name=tool_name,
        status=status,
        sanitized_arguments=arguments,
        result_summary=summary,
        started_at=datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC),
    )


def _service(
    *,
    model: LLMModel,
    completion,
    effective_budget: int,
    current_tokens: int,
    executions: list | None = None,
) -> RuntimeRunCompactorService:
    async def load(
        state: RuntimeGraphState,
        _context: RuntimeContext,
    ) -> RunCompactInputs:
        return RunCompactInputs(
            model=model,
            ledger={},
            effective_input_budget=effective_budget,
            current_input_tokens=current_tokens,
            executions=executions or [],
            request_shape=_request_shape(list(state["messages"])),  # type: ignore[typeddict-item]
        )

    return RuntimeRunCompactorService(
        settings=_settings(),
        completion=completion,
        input_loader=load,
    )


def _compacting_messages() -> list[JsonObject]:
    return [
        {
            **_normal("current", "EXACT CURRENT INPUT"),
            "runtime_input": "current",
        },
        _normal("completed-work", "completed work " * 300),
        _normal("recent", "recent result"),
    ]


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


def _serialize_prompt(messages: list[LLMMessage]) -> str:
    return json.dumps(
        [message.to_openai_format() for message in messages],
        ensure_ascii=False,
    )


def _ledger_message(messages: list[LLMMessage]) -> LLMMessage | None:
    """The deterministic ledger segment: a user message whose content starts
    with the completed_actions heading."""
    for message in messages:
        if (
            message.role == "user"
            and isinstance(message.content, str)
            and message.content.startswith("## completed_actions\n")
        ):
            return message
    return None


@pytest.mark.asyncio
async def test_payload_always_carries_completed_actions_key() -> None:
    """No executions yet: the prompt carries no ``completed_actions`` ledger segment."""
    state, context, tenant_id = _state(_compacting_messages())
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
    assert prompts, "compaction must have issued at least one prompt"
    for prompt in prompts:
        serialized = _serialize_prompt(prompt)
        assert "## completed_actions" not in serialized


@pytest.mark.asyncio
async def test_payload_completed_actions_match_deterministic_pipeline() -> None:
    """The ledger segment replays build_completed_actions over the same ledger."""
    executions = [
        _execution(execution_id="e1", call_id="c1", summary="First edit"),
        _execution(
            execution_id="e2",
            call_id="c2",
            status="failed",
            summary="Boom",
        ),
        _execution(
            execution_id="e3",
            call_id="c3",
            tool_name="write_file",
            path="docs/x.md",
            summary="Created",
        ),
    ]
    state, context, tenant_id = _state(_compacting_messages())
    prompts: list[list] = []

    async def complete(_model, prompt, **_kwargs):
        prompts.append(prompt)
        return _step()

    await _service(
        model=_model(tenant_id),
        completion=complete,
        effective_budget=1_000,
        current_tokens=900,
        executions=executions,
    ).compact_if_needed(state, context)

    assert prompts
    expected = build_completed_actions(executions)
    assert [entry["call_id"] for entry in expected] == ["c1", "c3"]
    for prompt in prompts:
        ledger = _ledger_message(prompt)
        assert ledger is not None
        assert json.loads(ledger.content[len("## completed_actions\n") :]) == expected


@pytest.mark.asyncio
async def test_instruction_carries_pipeline_precedence_rules() -> None:
    """The instruction sentinels for the three precedence rules are verbatim."""
    state, context, tenant_id = _state(_compacting_messages())
    prompts: list[list] = []

    async def complete(_model, prompt, **_kwargs):
        prompts.append(prompt)
        return _step()

    await _service(
        model=_model(tenant_id),
        completion=complete,
        effective_budget=1_000,
        current_tokens=900,
    ).compact_if_needed(state, context)

    assert prompts
    for prompt in prompts:
        assert prompt[0].role == "system"
        instruction = prompt[-1]
        assert instruction.role == "user"
        content = instruction.content
        assert isinstance(content, str)
        for sentinel in _PIPELINE_PRECEDENCE_SENTINELS:
            assert sentinel in content


@pytest.mark.asyncio
async def test_instruction_keeps_all_eight_section_headings() -> None:
    """The instruction still demands every section of the fixed structure."""
    state, context, tenant_id = _state(_compacting_messages())
    prompts: list[list] = []

    async def complete(_model, prompt, **_kwargs):
        prompts.append(prompt)
        return _step()

    await _service(
        model=_model(tenant_id),
        completion=complete,
        effective_budget=1_000,
        current_tokens=900,
    ).compact_if_needed(state, context)

    assert prompts
    instruction = prompts[0][-1]
    assert instruction.role == "user"
    content = instruction.content
    assert isinstance(content, str)
    for heading in _COMPACTION_SECTION_HEADINGS:
        assert heading in content
