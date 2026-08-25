"""Tests for the observability tracing facade (no live Langfuse required)."""

from __future__ import annotations

from contextlib import contextmanager
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
        gen.set_output("hello")
        gen.set_usage(TokenUsage(total_tokens=7, input_tokens=3, output_tokens=4, cache_read_tokens=2))

    update = span.updates[-1]
    assert update["output"] == "hello"
    assert update["usage_details"] == {"input": 3, "output": 4, "total": 7, "input_cache_read": 2}
    assert update["metadata"]["agent_id"] == "a-1"
    assert update["metadata"]["provider"] == "deepseek"
    assert "latency_ms" in update["metadata"]


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
