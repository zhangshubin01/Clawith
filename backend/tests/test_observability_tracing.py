"""Tests for the observability tracing facade (no live Langfuse required)."""

from __future__ import annotations

from contextlib import contextmanager
import sys
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from app.services.observability import tracing
from app.services.token_tracker import TokenUsage


class _FakeSpan:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    def update(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)


@contextmanager
def _fake_start_cm(span: _FakeSpan) -> Iterator[_FakeSpan]:
    yield span


class _FakeClient:
    def __init__(self, span: _FakeSpan | None = None, *, raise_on_start: bool = False) -> None:
        self.span = span or _FakeSpan()
        self.raise_on_start = raise_on_start

    def start_as_current_observation(self, **kwargs: Any) -> Any:
        if self.raise_on_start:
            raise RuntimeError("boom")
        return _fake_start_cm(self.span)


def test_observe_generation_is_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tracing, "_get_client", lambda _tenant_id=None: None)
    captured: list[str] = []

    with tracing.observe_generation(name="llm", model="m", provider="p") as gen:
        assert gen is None
        captured.append("ran")

    assert captured == ["ran"]


def test_observe_run_is_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tracing, "_get_client", lambda _tenant_id=None: None)
    captured: list[str] = []

    with tracing.observe_run(run_id="r-1", command_id="c-1", tenant_id="t-1") as run_handle:
        assert run_handle is None
        captured.append("ran")

    assert captured == ["ran"]


def test_observe_tool_is_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tracing, "_get_client", lambda _tenant_id=None: None)
    captured: list[str] = []

    with tracing.observe_tool(tool_name="write_file", tool_call_id="call-1") as handle:
        assert handle is None
        captured.append("ran")

    assert captured == ["ran"]


def test_observe_node_is_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tracing, "_get_client", lambda _tenant_id=None: None)
    captured: list[str] = []

    with tracing.observe_node(node="execute_tool", run_id="r-1") as handle:
        assert handle is None
        captured.append("ran")

    assert captured == ["ran"]


def test_observe_run_creates_root_span_with_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    span = _FakeSpan()
    monkeypatch.setattr(tracing, "_get_client", lambda _tenant_id=None, _span=span: _FakeClient(span=_span))

    with tracing.observe_run(
        run_id="r-1",
        command_id="c-1",
        tenant_id="t-1",
        agent_id="a-1",
        session_id="s-1",
        actor_user_id="u-1",
        graph_name="clawith_agent_runtime",
    ) as run_handle:
        assert run_handle is not None

    update = span.updates[-1]
    assert update["metadata"]["run_id"] == "r-1"
    assert update["metadata"]["command_id"] == "c-1"
    assert update["metadata"]["tenant_id"] == "t-1"
    assert update["metadata"]["agent_id"] == "a-1"
    assert update["metadata"]["session_id"] == "s-1"
    assert update["metadata"]["actor_user_id"] == "u-1"
    assert update["metadata"]["graph_name"] == "clawith_agent_runtime"
    assert "latency_ms" in update["metadata"]


def test_observe_run_records_output_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    span = _FakeSpan()
    monkeypatch.setattr(tracing, "_get_client", lambda _tenant_id=None, _span=span: _FakeClient(span=_span))

    with tracing.observe_run(run_id="r-1", command_id="c-1", tenant_id="t-1") as run_handle:
        assert run_handle is not None
        run_handle.set_output("final reply text")

    update = span.updates[-1]
    assert update["output"] == "final reply text"
    assert update["metadata"]["run_id"] == "r-1"


def test_observe_run_omits_output_when_never_set(monkeypatch: pytest.MonkeyPatch) -> None:
    span = _FakeSpan()
    monkeypatch.setattr(tracing, "_get_client", lambda _tenant_id=None, _span=span: _FakeClient(span=_span))

    with tracing.observe_run(run_id="r-1", command_id="c-1", tenant_id="t-1"):
        pass

    update = span.updates[-1]
    assert "output" not in update


def test_observe_run_output_masks_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    span = _FakeSpan()
    monkeypatch.setattr(tracing, "_get_client", lambda _tenant_id=None, _span=span: _FakeClient(span=_span))

    with tracing.observe_run(run_id="r-1", command_id="c-1", tenant_id="t-1") as run_handle:
        assert run_handle is not None
        run_handle.set_output("token is Bearer abc.def.ghi and the answer follows")

    output = span.updates[-1]["output"]
    assert isinstance(output, str)
    assert "abc.def.ghi" not in output
    assert "[REDACTED]" in output


def test_observe_run_output_truncates_to_max_string_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    span = _FakeSpan()
    monkeypatch.setattr(tracing, "_get_client", lambda _tenant_id=None, _span=span: _FakeClient(span=_span))

    long_reply = "x" * 5000
    with tracing.observe_run(run_id="r-1", command_id="c-1", tenant_id="t-1") as run_handle:
        assert run_handle is not None
        run_handle.set_output(long_reply)

    output = span.updates[-1]["output"]
    marker = "...<truncated 5000 chars>"
    assert isinstance(output, str)
    assert output == "x" * tracing._MAX_STRING_CHARS + marker
    assert len(output) == tracing._MAX_STRING_CHARS + len(marker)


def test_observe_run_records_error_and_reraises(monkeypatch: pytest.MonkeyPatch) -> None:
    span = _FakeSpan()
    monkeypatch.setattr(tracing, "_get_client", lambda _tenant_id=None, _span=span: _FakeClient(span=_span))

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with tracing.observe_run(run_id="r-1", command_id="c-1", tenant_id="t-1") as run_handle:
            assert run_handle is not None
            raise Boom("kaboom")

    update = span.updates[-1]
    assert update["level"] == "ERROR"
    assert "Boom" in update["status_message"]


def test_observe_run_retry_control_flow_not_error(monkeypatch: pytest.MonkeyPatch) -> None:
    span = _FakeSpan()
    monkeypatch.setattr(tracing, "_get_client", lambda _tenant_id=None, _span=span: _FakeClient(span=_span))

    # Business retry-control-flow exception (matched by class name, no import)
    class ToolExecutionReconciliationPending(RuntimeError):
        defer_without_attempt = True

    with pytest.raises(ToolExecutionReconciliationPending):
        with tracing.observe_run(run_id="r-1", command_id="c-1", tenant_id="t-1") as run_handle:
            assert run_handle is not None
            raise ToolExecutionReconciliationPending("A safe read attempt still owns the active receipt")

    update = span.updates[-1]
    assert "level" not in update  # stays DEFAULT, not ERROR
    assert update["status_message"].startswith("ToolExecutionReconciliationPending")
    assert update["metadata"]["retry_pending"] is True
    assert update["metadata"]["retry_type"] == "ToolExecutionReconciliationPending"


def test_observe_node_retry_control_flow_not_error(monkeypatch: pytest.MonkeyPatch) -> None:
    span = _FakeSpan()
    monkeypatch.setattr(tracing, "_get_client", lambda _tenant_id=None, _span=span: _FakeClient(span=_span))

    class RetryableCommandError(RuntimeError):
        pass

    with pytest.raises(RetryableCommandError):
        with tracing.observe_node(node="tool") as node_handle:
            assert node_handle is not None
            raise RetryableCommandError("retry me")

    update = span.updates[-1]
    assert "level" not in update
    assert update["metadata"]["retry_pending"] is True


def test_observe_tool_retryable_tool_node_error_not_error(monkeypatch: pytest.MonkeyPatch) -> None:
    span = _FakeSpan()
    monkeypatch.setattr(tracing, "_get_client", lambda _tenant_id=None, _span=span: _FakeClient(span=_span))

    # Safe-read tool retry control flow (matched by class name, no import):
    # every instance is caught by the graph's TOOL_RETRY_POLICY, so it is a
    # scheduled retry, not a tool failure.
    class RetryableToolNodeError(RuntimeError):
        pass

    with pytest.raises(RetryableToolNodeError):
        with tracing.observe_tool(tool_name="read_file", tool_call_id="call-1") as tool_handle:
            assert tool_handle is not None
            raise RetryableToolNodeError("safe read tool attempt is eligible for Runtime retry")

    update = span.updates[-1]
    assert "level" not in update  # stays DEFAULT, not ERROR
    assert update["status_message"].startswith("RetryableToolNodeError")
    assert update["metadata"]["retry_pending"] is True
    assert update["metadata"]["retry_type"] == "RetryableToolNodeError"


def test_observe_run_retryable_tool_node_error_not_error(monkeypatch: pytest.MonkeyPatch) -> None:
    span = _FakeSpan()
    monkeypatch.setattr(tracing, "_get_client", lambda _tenant_id=None, _span=span: _FakeClient(span=_span))

    class RetryableToolNodeError(RuntimeError):
        pass

    with pytest.raises(RetryableToolNodeError):
        with tracing.observe_run(run_id="r-1", command_id="c-1", tenant_id="t-1") as run_handle:
            assert run_handle is not None
            raise RetryableToolNodeError("safe read tool attempt is eligible for Runtime retry")

    update = span.updates[-1]
    assert "level" not in update
    assert update["status_message"].startswith("RetryableToolNodeError")
    assert update["metadata"]["retry_pending"] is True
    assert update["metadata"]["retry_type"] == "RetryableToolNodeError"


def test_observe_run_sets_identity_for_nested_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    span = _FakeSpan()
    monkeypatch.setattr(tracing, "_get_client", lambda _tenant_id=None, _span=span: _FakeClient(span=_span))

    with tracing.observe_run(
        run_id="r-9",
        command_id="c-9",
        tenant_id="t-9",
        agent_id="a-9",
        session_id="s-9",
        actor_user_id="u-9",
    ):
        with tracing.observe_generation(name="llm") as gen:
            assert gen is not None

    # The nested generation inherits the ambient run identity (run metadata).
    update = span.updates[-1]
    assert update["metadata"]["run_id"] == "r-9"
    assert update["metadata"]["session_id"] == "s-9"
    assert update["metadata"]["tenant_id"] == "t-9"


def test_observe_tool_records_identity_and_output(monkeypatch: pytest.MonkeyPatch) -> None:
    span = _FakeSpan()
    monkeypatch.setattr(tracing, "_get_client", lambda _tenant_id=None, _span=span: _FakeClient(span=_span))

    with tracing.observe_tool(
        tool_name="edit_file",
        tool_call_id="call-1",
        tool_execution_id="exec-1",
        side_effect_classification="write",
        retry_policy="conditional",
    ) as tool_handle:
        assert tool_handle is not None
        tool_handle.set_output({"status": "succeeded", "result_summary": "patched", "error_code": None})

    update = span.updates[-1]
    assert update["output"] == {"status": "succeeded", "result_summary": "patched", "error_code": None}
    assert update["metadata"]["tool_name"] == "edit_file"
    assert update["metadata"]["tool_call_id"] == "call-1"
    assert update["metadata"]["tool_execution_id"] == "exec-1"
    assert update["metadata"]["side_effect_classification"] == "write"
    assert update["metadata"]["retry_policy"] == "conditional"


def test_observe_node_records_node_and_error(monkeypatch: pytest.MonkeyPatch) -> None:
    span = _FakeSpan()
    monkeypatch.setattr(tracing, "_get_client", lambda _tenant_id=None, _span=span: _FakeClient(span=_span))

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with tracing.observe_node(node="model") as node_handle:
            assert node_handle is not None
            raise Boom("node failed")

    update = span.updates[-1]
    assert update["metadata"]["node"] == "model"
    assert update["level"] == "ERROR"
    assert "Boom" in update["status_message"]


def test_observe_tool_swallows_start_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tracing, "_get_client", lambda _tenant_id=None: _FakeClient(raise_on_start=True))
    captured: list[str] = []

    with tracing.observe_tool(tool_name="write_file") as tool_handle:
        assert tool_handle is None
        captured.append("ran")

    assert captured == ["ran"]


def test_tenant_key_map_parses_valid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tracing, "_tenant_keys", None)
    settings = tracing.get_settings()
    monkeypatch.setattr(
        settings,
        "LANGFUSE_TENANT_KEYS",
        '{"t-1": {"public_key": "pk-1", "secret_key": "sk-1"}, "t-2": {"public_key": "pk-2", "secret_key": "sk-2"}}',
    )
    assert tracing._tenant_key_map() == {
        "t-1": {"public_key": "pk-1", "secret_key": "sk-1"},
        "t-2": {"public_key": "pk-2", "secret_key": "sk-2"},
    }
    # cached on second call
    assert tracing._tenant_key_map() == tracing._tenant_key_map()


def test_tenant_key_map_rejects_bad_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tracing, "_tenant_keys", None)
    settings = tracing.get_settings()
    monkeypatch.setattr(settings, "LANGFUSE_TENANT_KEYS", "{not-json")
    assert tracing._tenant_key_map() == {}


def test_tenant_key_map_skips_incomplete_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tracing, "_tenant_keys", None)
    settings = tracing.get_settings()
    monkeypatch.setattr(
        settings,
        "LANGFUSE_TENANT_KEYS",
        '{"t-ok": {"public_key": "pk", "secret_key": "sk"}, "t-bad": {"public_key": "pk-only"}}',
    )
    assert tracing._tenant_key_map() == {"t-ok": {"public_key": "pk", "secret_key": "sk"}}


def test_get_client_uses_tenant_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[tuple[str, str]] = []

    def _fake_build(*, public_key: str, secret_key: str) -> object:
        built.append((public_key, secret_key))
        return object()

    monkeypatch.setattr(tracing, "_build_client", _fake_build)
    monkeypatch.setattr(tracing, "_tenant_keys", {"t-1": {"public_key": "pk-t1", "secret_key": "sk-t1"}})
    monkeypatch.setattr(tracing, "_tenant_clients", {})
    monkeypatch.setattr(tracing, "_tenant_errors", {})
    monkeypatch.setattr(tracing, "_client", None)
    monkeypatch.setattr(tracing, "_client_error", None)

    c1 = tracing._get_client("t-1")
    c2 = tracing._get_client("t-1")
    assert built == [("pk-t1", "sk-t1")]  # cached, built once
    assert c1 is c2


def test_get_client_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    default = object()
    monkeypatch.setattr(tracing, "_client", default)
    monkeypatch.setattr(tracing, "_tenant_keys", {"t-1": {"public_key": "pk", "secret_key": "sk"}})
    # unknown tenant -> default client
    assert tracing._get_client("t-unknown") is default
    # no tenant -> default client
    assert tracing._get_client(None) is default


def test_get_client_tenant_init_failure_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*, public_key: str, secret_key: str) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(tracing, "_build_client", _boom)
    monkeypatch.setattr(tracing, "_tenant_keys", {"t-1": {"public_key": "pk", "secret_key": "sk"}})
    monkeypatch.setattr(tracing, "_tenant_clients", {})
    monkeypatch.setattr(tracing, "_tenant_errors", {})

    assert tracing._get_client("t-1") is None
    assert "t-1" in tracing._tenant_errors
    # error cached — second call also None without rebuilding
    assert tracing._get_client("t-1") is None


def test_flush_covers_tenant_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeFlushClient:
        def __init__(self, name: str) -> None:
            self.name = name
            self.flushed = False

        def flush(self) -> None:
            self.flushed = True

    default = _FakeFlushClient("default")
    tenant = _FakeFlushClient("tenant")
    monkeypatch.setattr(tracing, "_client", default)
    monkeypatch.setattr(tracing, "_tenant_clients", {"t-1": tenant})

    tracing.flush()
    assert default.flushed and tenant.flushed


def test_observe_generation_swallows_span_start_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tracing, "_get_client", lambda _tenant_id=None: _FakeClient(raise_on_start=True))
    captured: list[str] = []

    with tracing.observe_generation(name="llm") as gen:
        assert gen is None
        captured.append("ran")

    assert captured == ["ran"]


def test_observe_generation_records_output_usage_and_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    span = _FakeSpan()
    monkeypatch.setattr(tracing, "_get_client", lambda _tenant_id=None, _span=span: _FakeClient(span=_span))

    with tracing.observe_generation(
        name="llm",
        model="deepseek-v4-pro",
        provider="deepseek",
        agent_id="a-1",
    ) as gen:
        assert gen is not None
        gen.set_output({"content": "hello", "reasoning_content": None})
        # DeepSeek counts cache hits inside prompt_tokens: input=3 = hit 2 + miss 1.
        # Langfuse prices buckets independently (no subtraction), so `input` must
        # be reported as the uncached remainder to avoid double-billing hits.
        gen.set_usage(
            TokenUsage(
                total_tokens=7,
                input_tokens=3,
                output_tokens=4,
                cache_read_tokens=2,
                cache_miss_tokens=1,
            )
        )

    update = span.updates[-1]
    assert update["output"] == {"content": "hello", "reasoning_content": None}
    assert update["usage_details"] == {"input": 1, "output": 4, "total": 7, "input_cache_read": 2}
    assert update["metadata"]["agent_id"] == "a-1"
    assert update["metadata"]["provider"] == "deepseek"
    assert "latency_ms" in update["metadata"]


def test_observe_generation_captures_input_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    span = _FakeSpan()
    started: dict[str, Any] = {}

    class _CapturingClient(_FakeClient):
        def start_as_current_observation(self, **kwargs: Any) -> Any:
            started.update(kwargs)
            return _fake_start_cm(self.span)

    monkeypatch.setattr(tracing, "_get_client", lambda _tenant_id=None: _CapturingClient(span=span))

    prompt = [{"role": "user", "content": "hi"}]
    with tracing.observe_generation(name="llm", input=prompt) as gen:
        assert gen is not None

    # The prompt is always captured now — there is no opt-out switch left.
    assert started["input"] == prompt


def test_observe_generation_output_dict_reasoning_exempt_from_default_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    span = _FakeSpan()
    monkeypatch.setattr(tracing, "_get_client", lambda _tenant_id=None, _span=span: _FakeClient(span=_span))

    long_reasoning = "r" * 50000
    long_content = "c" * 5000
    with tracing.observe_generation(name="llm") as gen:
        assert gen is not None
        gen.set_output({"content": long_content, "reasoning_content": long_reasoning})

    output = span.updates[-1]["output"]
    # The generation cap (64k) bounds the whole output tree, so both keys
    # survive past the generic 4k bound — the raised cap exists exactly so
    # long reasoning text is not lost.
    assert output["reasoning_content"] == long_reasoning
    assert output["content"] == long_content


def test_observe_generation_output_dict_reasoning_truncates_at_generation_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    span = _FakeSpan()
    monkeypatch.setattr(tracing, "_get_client", lambda _tenant_id=None, _span=span: _FakeClient(span=_span))

    overflow = "r" * 70000
    with tracing.observe_generation(name="llm") as gen:
        assert gen is not None
        gen.set_output({"content": "ok", "reasoning_content": overflow})

    output = span.updates[-1]["output"]
    marker = "...<truncated 70000 chars>"
    assert output["reasoning_content"] == "r" * tracing._GENERATION_MAX_STRING_CHARS + marker
    assert output["content"] == "ok"


def test_observe_generation_output_dict_redacts_secrets_in_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    span = _FakeSpan()
    monkeypatch.setattr(tracing, "_get_client", lambda _tenant_id=None, _span=span: _FakeClient(span=_span))

    with tracing.observe_generation(name="llm") as gen:
        assert gen is not None
        gen.set_output(
            {
                "content": "the answer",
                "reasoning_content": "credential hint: Bearer abc.def.ghi must not leak",
            }
        )

    output = span.updates[-1]["output"]
    assert "abc.def.ghi" not in output["reasoning_content"]
    assert "[REDACTED]" in output["reasoning_content"]
    assert output["content"] == "the answer"


def test_observe_generation_records_error_and_reraises(monkeypatch: pytest.MonkeyPatch) -> None:
    span = _FakeSpan()
    monkeypatch.setattr(tracing, "_get_client", lambda _tenant_id=None, _span=span: _FakeClient(span=_span))

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with tracing.observe_generation(name="llm") as gen:
            assert gen is not None
            raise Boom("kaboom")

    update = span.updates[-1]
    assert update["level"] == "ERROR"
    assert "Boom" in update["status_message"]


def test_set_run_identity_propagates_and_direct_arg_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    span = _FakeSpan()
    monkeypatch.setattr(tracing, "_get_client", lambda _tenant_id=None, _span=span: _FakeClient(span=_span))

    token = tracing._run_identity.set({})
    try:
        tracing.set_run_identity(tenant_id="t-1", run_id="r-1", agent_id="a-1")
        with tracing.observe_generation(name="llm", agent_id="a-2") as gen:
            assert gen is not None
    finally:
        tracing._run_identity.reset(token)

    update = span.updates[-1]
    assert update["metadata"]["tenant_id"] == "t-1"
    assert update["metadata"]["run_id"] == "r-1"
    # The direct call-site agent_id overrides the ambient run identity.
    assert update["metadata"]["agent_id"] == "a-2"


def test_mask_text_redacts_secrets() -> None:
    value = {
        "authorization": "Bearer abc.def.ghi",
        "api_key": "sk-secret123456",
        "nested": {"dsn": "postgresql://user:pass@host/db"},
        "clean": "hello world",
    }
    masked = tracing.mask_text(value)
    assert masked["authorization"] == "[REDACTED]"
    assert masked["api_key"] == "[REDACTED]"
    assert masked["nested"]["dsn"] == "[REDACTED]"  # DSN URL pattern
    assert masked["clean"] == "hello world"


def test_mask_text_redacts_bearer_and_aws_keys() -> None:
    masked = tracing.mask_text("token is Bearer eyJhbGciOiJIUzI1NiJ9.abc and akiaABCDEFGHIJKLMNOP")
    assert "Bearer" not in masked
    assert "akiaABCDEFGHIJKLMNOP" not in masked


def test_map_usage_from_provider_dict() -> None:
    usage = tracing._map_usage({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
    assert usage == {"input": 10, "output": 5, "total": 15}


def test_map_usage_deepseek_cache_hits_not_double_billed() -> None:
    """DeepSeek reports hits inside prompt_tokens; input must be the miss only."""
    usage = tracing._map_usage(
        {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "prompt_cache_hit_tokens": 6,
            "prompt_cache_miss_tokens": 4,
        },
        provider="deepseek",
    )
    assert usage == {
        "input": 4,
        "output": 5,
        "total": 15,
        "input_cache_read": 6,
    }


def test_map_usage_anthropic_input_kept_uncached_semantics() -> None:
    """Anthropic's input_tokens already exclude cache read; keep it unchanged."""
    usage = tracing._map_usage(
        TokenUsage(total_tokens=20, input_tokens=10, output_tokens=10, cache_read_tokens=4),
        provider="anthropic",
    )
    assert usage == {
        "input": 10,
        "output": 10,
        "total": 24,
        "input_cache_read": 4,
    }


def test_map_usage_total_is_bucket_sum_not_provider_total() -> None:
    """DeepSeek's total_tokens is a half-credit equivalent (miss + completion +
    cache_hit/2), not the real token count. Langfuse requires `total` to equal the
    sum of the detail buckets, otherwise it warns "Sum of provided non-total
    usage_details buckets exceeds provided total" and token stats under-report.
    """
    usage = tracing._map_usage(
        TokenUsage(
            total_tokens=23955,  # provider equivalent: 3296 + 1587 + 38144/2
            input_tokens=41440,  # prompt_tokens including cache hits
            output_tokens=1587,
            cache_read_tokens=38144,
            cache_miss_tokens=3296,
        ),
        provider="deepseek",
    )
    assert usage == {
        "input": 3296,
        "output": 1587,
        "total": 43027,  # bucket sum (3296 + 1587 + 38144), NOT 23955
        "input_cache_read": 38144,
    }


def test_map_usage_no_cache_keeps_input_as_is() -> None:
    usage = tracing._map_usage(
        TokenUsage(total_tokens=15, input_tokens=10, output_tokens=5),
        provider="deepseek",
    )
    assert usage == {"input": 10, "output": 5, "total": 15}


def test_build_client_passes_release_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """LANGFUSE_RELEASE 非空时，client 构造 kwargs 必须含 release=部署 commit。"""
    captured: dict[str, Any] = {}

    class _FakeLangfuse:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setitem(sys.modules, "langfuse", SimpleNamespace(Langfuse=_FakeLangfuse))
    settings = tracing.get_settings()
    monkeypatch.setattr(settings, "LANGFUSE_RELEASE", "72daf94c")
    monkeypatch.setattr(settings, "LANGFUSE_HOST", "")

    client = tracing._build_client(public_key="pk-1", secret_key="sk-1")

    assert client is not None
    assert captured["public_key"] == "pk-1"
    assert captured["secret_key"] == "sk-1"
    assert captured["release"] == "72daf94c"


def test_build_client_omits_release_when_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    """LANGFUSE_RELEASE 为空（默认）时不传 release——不改变既有 client 构造行为。"""
    captured: dict[str, Any] = {}

    class _FakeLangfuse:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setitem(sys.modules, "langfuse", SimpleNamespace(Langfuse=_FakeLangfuse))
    settings = tracing.get_settings()
    monkeypatch.setattr(settings, "LANGFUSE_RELEASE", "")
    monkeypatch.setattr(settings, "LANGFUSE_HOST", "")

    tracing._build_client(public_key="pk-1", secret_key="sk-1")

    assert captured == {"public_key": "pk-1", "secret_key": "sk-1"}


def test_disabled_observability_client_and_run_are_safe_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """observability disabled 时 _get_client 为 None，observe_run no-op 不抛错。"""
    settings = tracing.get_settings()
    monkeypatch.setattr(settings, "OBSERVABILITY_ENABLED", False)
    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setattr(settings, "LANGFUSE_SECRET_KEY", "")
    monkeypatch.setattr(tracing, "_client", None)
    monkeypatch.setattr(tracing, "_client_error", None)

    assert tracing._get_client() is None
    captured: list[str] = []
    with tracing.observe_run(run_id="r-1", command_id="c-1", tenant_id="t-1") as run_handle:
        assert run_handle is None
        captured.append("ran")
    assert captured == ["ran"]


class _TraceAwareSpan(_FakeSpan):
    """Fake root span carrying the Langfuse trace id (StatefulSpan.trace_id)."""

    def __init__(self, trace_id: str) -> None:
        super().__init__()
        self.trace_id = trace_id


def test_observe_run_exposes_current_trace_id_in_context(monkeypatch: pytest.MonkeyPatch) -> None:
    span = _TraceAwareSpan("0123456789abcdef0123456789abcdef")
    monkeypatch.setattr(tracing, "_get_client", lambda _tenant_id=None, _span=span: _FakeClient(span=_span))

    assert tracing.current_trace_id() is None
    seen: list[str | None] = []
    with tracing.observe_run(run_id="r-1", command_id="c-1", tenant_id="t-1"):
        seen.append(tracing.current_trace_id())
    # 退出 observe_run 后上下文恢复——挂载点结算读的是 checkpoint metadata，
    # 不在 trace 上下文内。
    assert seen == ["0123456789abcdef0123456789abcdef"]
    assert tracing.current_trace_id() is None


def test_current_trace_id_is_none_when_run_observations_never_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tracing, "_get_client", lambda _tenant_id=None: None)
    with tracing.observe_run(run_id="r-1", command_id="c-1", tenant_id="t-1") as run_handle:
        assert run_handle is None
        assert tracing.current_trace_id() is None
