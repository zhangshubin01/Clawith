"""Deterministic step settlement (D) — pure function seam tests.

Ticket A / S1: ``settle_completed_exchanges`` replaces window-out completed
tool exchanges with deterministic ledger-backed synthetic messages, block by
block, never touching protected or unresolved regions.
"""

from __future__ import annotations

import json

import pytest

from app.services.agent_runtime.run_compactor import (
    RunCompactorError,
    settle_completed_exchanges,
)
from app.services.agent_runtime.state import JsonObject
from app.services.agent_runtime.tool_exchange import validate_tool_exchange_integrity


RUN_ID = "11111111-1111-1111-1111-111111111111"
# ~2000 estimated tokens: decisively outside a 600-token recent window.
_WINDOW_OUT_CONTENT = "A" * 8000
_WINDOW_IN_CONTENT = "B" * 20


def _current_input() -> JsonObject:
    return {
        "id": "current-input",
        "role": "user",
        "content": "Fix the three P1 bugs",
        "runtime_input": "current",
        "runtime_run_id": RUN_ID,
    }


def _assistant(message_id: str, call_id: str, name: str = "edit_file") -> JsonObject:
    return {
        "id": message_id,
        "role": "assistant",
        "content": "",
        "runtime_run_id": RUN_ID,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": "{}"},
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
        "runtime_run_id": RUN_ID,
    }


def _exchange(
    assistant_id: str,
    call_id: str,
    result_id: str,
    *,
    content: str = "result",
) -> tuple[JsonObject, JsonObject]:
    return (
        _assistant(assistant_id, call_id),
        _tool_result(result_id, call_id, content=content),
    )


def _ledger(*call_ids: str, status: str = "succeeded") -> dict[str, JsonObject]:
    result: dict[str, JsonObject] = {}
    for call_id in call_ids:
        result[call_id] = {
            "status": status,
            "tool_name": "edit_file",
            "result_summary": f"Replaced 1 occurrence(s) in src/app.py ({call_id}).",
            "side_effect_classification": "write",
            "retry_policy": "conditional",
            "may_have_side_effect": True,
        }
    return result


def _settle(
    messages: list[JsonObject],
    ledger: dict[str, JsonObject],
    *,
    recent_token_budget: int = 600,
    protected_message_ids: frozenset[str] = frozenset(),
):
    return settle_completed_exchanges(
        messages,
        ledger,
        recent_token_budget=recent_token_budget,
        protected_token_budget=100_000,
        protected_message_ids=protected_message_ids,
    )


def _message_ids(messages) -> list[str]:
    return [str(message["id"]) for message in messages]


def _synthetic_content(message: JsonObject) -> dict:
    content = message["content"]
    assert isinstance(content, dict), "synthetic message content must be an object"
    return content


def test_settles_window_out_completed_exchange_blockwise() -> None:
    # Exchange A leaves the recent window, exchange B stays inside it.
    assistant_a, result_a = _exchange("assistant-a", "call-a", "result-a", content=_WINDOW_OUT_CONTENT)
    assistant_b, result_b = _exchange("assistant-b", "call-b", "result-b", content=_WINDOW_IN_CONTENT)
    messages = [_current_input(), assistant_a, result_a, assistant_b, result_b]
    ledger = _ledger("call-a", "call-b")

    settlement = _settle(messages, ledger)

    assert settlement.settled_count == 1
    ids = _message_ids(settlement.messages)
    # The whole old exchange disappears as a block; one synthetic user message
    # takes the id of the block's last (result) message.
    assert "assistant-a" not in ids
    assert "result-a" in ids  # reused as the synthetic message id
    assert ids == ["current-input", "result-a", "assistant-b", "result-b"]

    synthetic = next(message for message in settlement.messages if message["id"] == "result-a")
    assert synthetic["role"] == "user"
    content = _synthetic_content(synthetic)
    exchange = content["historical_tool_exchange"]
    assert exchange["reason"] == "complete_exchange_settled"
    assert exchange["assistant_message_id"] == "assistant-a"
    (call_summary,) = exchange["calls"]
    assert call_summary["tool_call_id"] == "call-a"
    assert call_summary["tool_name"] == "edit_file"
    assert call_summary["execution_status"] == "succeeded"
    # Deterministic content: no timestamps or volatile fields.
    serialized = json.dumps(content, sort_keys=True)
    assert "timestamp" not in serialized.lower()
    assert "datetime" not in serialized.lower()
    assert "20" not in serialized

    # The window-in exchange keeps its full content verbatim.
    kept_result = next(message for message in settlement.messages if message["id"] == "result-b")
    assert kept_result["content"] == _WINDOW_IN_CONTENT
    kept_assistant = next(message for message in settlement.messages if message["id"] == "assistant-b")
    assert kept_assistant["tool_calls"][0]["id"] == "call-b"

    # Removal plan is exactly the settled block's ids.
    (settled,) = settlement.settled
    assert settled.removed_message_ids == ("assistant-a", "result-a")
    assert settled.synthetic_message["id"] == "result-a"

    # The settled sequence remains a valid tool-exchange-safe history.
    validate_tool_exchange_integrity(settlement.messages)


def test_window_in_exchange_never_settled() -> None:
    assistant, result = _exchange("assistant-a", "call-a", "result-a", content="A" * 40)
    messages = [_current_input(), assistant, result]
    settlement = _settle(messages, _ledger("call-a"), recent_token_budget=10_000)
    assert settlement.settled_count == 0
    assert settlement.messages == tuple(messages)


def test_protected_repair_and_resume_messages_never_settled() -> None:
    assistant_a, result_a = _exchange("assistant-a", "call-a", "result-a", content=_WINDOW_OUT_CONTENT)
    repair = {
        "id": "repair-1",
        "role": "user",
        "content": "Return one complete, non-empty final response.",
        "runtime_intent": "repair",
        "runtime_run_id": RUN_ID,
    }
    resume = {
        "id": "resume-1",
        "role": "user",
        "content": "Continue with the plan",
        "runtime_input": "resume",
        "runtime_run_id": RUN_ID,
    }
    assistant_b, result_b = _exchange("assistant-b", "call-b", "result-b", content=_WINDOW_IN_CONTENT)
    messages = [_current_input(), assistant_a, result_a, repair, resume, assistant_b, result_b]
    ledger = _ledger("call-a", "call-b")

    settlement = _settle(
        messages,
        ledger,
        protected_message_ids=frozenset({"repair-1", "resume-1"}),
    )
    ids = _message_ids(settlement.messages)
    # Protected repair/resume messages survive byte-identical in place.
    assert "repair-1" in ids and "resume-1" in ids
    assert settlement.messages[ids.index("repair-1")]["content"] == repair["content"]
    assert settlement.messages[ids.index("resume-1")]["content"] == resume["content"]
    # The old completed exchange is still settled; protected blocks are never
    # part of the removed ids.
    assert settlement.settled_count == 1
    removed = {message_id for settled in settlement.settled for message_id in settled.removed_message_ids}
    assert "repair-1" not in removed and "resume-1" not in removed
    validate_tool_exchange_integrity(settlement.messages)


def test_unresolved_exchange_is_barrier_suffix_never_settles() -> None:
    # An exchange whose ledger status is still in flight is the compaction
    # barrier: nothing after it may be settled. Blocks BEFORE the barrier still
    # settle normally.
    assistant_a, result_a = _exchange("assistant-a", "call-a", "result-a", content=_WINDOW_OUT_CONTENT)
    assistant_orphan = _assistant("assistant-orphan", "call-orphan")
    assistant_b, result_b = _exchange("assistant-b", "call-b", "result-b", content=_WINDOW_IN_CONTENT)
    messages = [
        _current_input(),
        assistant_a,
        result_a,
        assistant_orphan,
        assistant_b,
        result_b,
    ]
    ledger = _ledger("call-a", "call-b")
    ledger["call-orphan"] = {
        "status": "started",
        "tool_name": "edit_file",
        "may_have_side_effect": True,
    }

    settlement = _settle(messages, ledger)
    ids = _message_ids(settlement.messages)
    # The in-flight exchange and everything after it stay verbatim.
    assert "assistant-orphan" in ids and "assistant-b" in ids and "result-b" in ids
    assert "assistant-a" not in ids
    assert settlement.settled_count == 1
    (settled,) = settlement.settled
    assert settled.removed_message_ids == ("assistant-a", "result-a")
    # The unresolved exchange remains unresolved in the output — settlement
    # must never fabricate a result for it.
    assert "result-a" in ids  # synthetic takes the settled block's last id
    validate_tool_exchange_integrity(
        [message for message in settlement.messages if message["id"] not in {"assistant-orphan"}]
    )


def test_exchange_without_ledger_entry_never_settled() -> None:
    assistant_a, result_a = _exchange("assistant-a", "call-a", "result-a", content=_WINDOW_OUT_CONTENT)
    assistant_b, result_b = _exchange("assistant-b", "call-b", "result-b", content=_WINDOW_IN_CONTENT)
    messages = [_current_input(), assistant_a, result_a, assistant_b, result_b]
    # No ledger record for call-a: its outcome is unobservable -> keep it raw.
    ledger = _ledger("call-b")

    settlement = _settle(messages, ledger)
    assert settlement.settled_count == 0
    assert settlement.messages == tuple(messages)
    validate_tool_exchange_integrity(settlement.messages)


def test_failed_exchange_settles_with_failed_status() -> None:
    assistant_a, result_a = _exchange("assistant-a", "call-a", "result-a", content=_WINDOW_OUT_CONTENT)
    messages = [_current_input(), assistant_a, result_a]
    ledger = _ledger("call-a", status="failed")

    settlement = _settle(messages, ledger)
    assert settlement.settled_count == 1
    synthetic = next(message for message in settlement.messages if message["id"] == "result-a")
    exchange = _synthetic_content(synthetic)["historical_tool_exchange"]
    assert exchange["calls"][0]["execution_status"] == "failed"
    validate_tool_exchange_integrity(settlement.messages)


def test_settlement_is_deterministic_and_idempotent() -> None:
    assistant_a, result_a = _exchange("assistant-a", "call-a", "result-a", content=_WINDOW_OUT_CONTENT)
    assistant_b, result_b = _exchange("assistant-b", "call-b", "result-b", content=_WINDOW_IN_CONTENT)
    messages = [_current_input(), assistant_a, result_a, assistant_b, result_b]
    ledger = _ledger("call-a", "call-b")

    first = _settle(messages, ledger)
    second = _settle(list(messages), dict(ledger))
    assert first == second
    serialized_first = json.dumps([dict(message) for message in first.messages], sort_keys=True)
    serialized_second = json.dumps([dict(message) for message in second.messages], sort_keys=True)
    assert serialized_first == serialized_second

    # Settling the settled sequence settles nothing new.
    again = _settle(list(first.messages), ledger)
    assert again.settled_count == 0
    assert again.messages == first.messages


def test_normal_window_out_messages_pass_through_verbatim() -> None:
    normal = {
        "id": "normal-1",
        "role": "user",
        "content": "Historical note with exact identifiers module.Foo.bar " + ("N" * 8000),
        "runtime_run_id": RUN_ID,
    }
    assistant_b, result_b = _exchange("assistant-b", "call-b", "result-b", content=_WINDOW_IN_CONTENT)
    messages = [_current_input(), normal, assistant_b, result_b]
    settlement = _settle(messages, _ledger("call-b"))
    assert settlement.settled_count == 0
    assert settlement.messages == tuple(messages)


def test_window_budget_overflow_fails_closed_with_run_compactor_error() -> None:
    # A barrier whose mandatory retained region exceeds the recent budget must
    # surface the same deterministic error compaction would.
    assistant_orphan = _assistant("assistant-orphan", "call-orphan")
    messages = [_current_input(), assistant_orphan]
    ledger = _ledger("call-orphan", status="started")
    with pytest.raises(RunCompactorError) as exc_info:
        _settle(messages, ledger, recent_token_budget=1)
    assert exc_info.value.code in {
        "unsafe_exchange_exceeds_recent_budget",
        "input_exceeds_model_context",
    }
