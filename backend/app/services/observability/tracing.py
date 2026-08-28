"""Framework-agnostic trace facade over Langfuse (OpenTelemetry-backed).

Phase 1 instruments LLM generations only. Every helper is a strict no-op
unless observability is enabled AND Langfuse credentials are configured, so the
backend runs unchanged in the default configuration. All ``langfuse`` imports
are lazy, and any observability-internal failure is swallowed so tracing can
never break an LLM call path. This module is the only integration point —
swap-ready without touching call sites (C6 anti-reinvention).
"""

from __future__ import annotations

import contextvars
import dataclasses
import json
import re
import time
from contextlib import ExitStack, contextmanager
from typing import Any, Iterator

from loguru import logger

from app.config import get_settings
from app.services.token_tracker import TokenUsage, extract_token_usage

__all__ = [
    "GenerationHandle",
    "RunHandle",
    "flush",
    "is_enabled",
    "mask_text",
    "observe_generation",
    "observe_node",
    "observe_run",
    "observe_tool",
    "set_run_identity",
]

_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "secret",
        "secret_key",
        "token",
        "access_token",
        "refresh_token",
        "password",
        "passwd",
        "authorization",
        "private_key",
        "credential",
    }
)

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\b(postgres(?:ql)?|mysql|redis|mongodb|amqp|rediss)://[^\s\"'<>]+"),
    re.compile(r"(?i)\b(AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{8,}\b"),
)

_MAX_STRING_CHARS = 4000
_UNSET = object()

# Business control-flow exceptions that schedule a command retry rather than
# failing the run (e.g. ToolExecutionReconciliationPending for safe-read lease
# conflicts, RetryableCommandError, RetryableToolNodeError for the safe-read
# tool node retry policy). They are expected noise in traces, not failures.
# Matched by class name to keep this facade framework-agnostic.
_RETRY_CONTROL_FLOW_NAMES = frozenset(
    {
        "ToolExecutionReconciliationPending",
        "GroupWorkspaceReconciliationPending",
        "RetryableCommandError",
        "RetryableToolNodeError",
    }
)

_client: Any = None
_client_error: Exception | None = None
_tenant_clients: dict[str, Any] = {}
_tenant_errors: dict[str, Exception] = {}
_tenant_keys: dict[str, dict[str, str]] | None = None

_run_identity: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "clawith_observability_identity", default={}
)


def is_enabled() -> bool:
    """Whether trace export is configured (independent of SDK health)."""
    settings = get_settings()
    return bool(settings.OBSERVABILITY_ENABLED and settings.LANGFUSE_PUBLIC_KEY and settings.LANGFUSE_SECRET_KEY)


def _tenant_key_map() -> dict[str, dict[str, str]]:
    """Parse the LANGFUSE_TENANT_KEYS JSON map once (tenant_id -> credentials)."""
    global _tenant_keys
    if _tenant_keys is not None:
        return _tenant_keys
    settings = get_settings()
    raw = (settings.LANGFUSE_TENANT_KEYS or "").strip()
    if not raw:
        _tenant_keys = {}
        return _tenant_keys
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("LANGFUSE_TENANT_KEYS must be a JSON object")
        normalized: dict[str, dict[str, str]] = {}
        for tenant_id, creds in parsed.items():
            if not isinstance(creds, dict):
                continue
            public_key = creds.get("public_key")
            secret_key = creds.get("secret_key")
            if isinstance(public_key, str) and public_key and isinstance(secret_key, str) and secret_key:
                normalized[str(tenant_id)] = {"public_key": public_key, "secret_key": secret_key}
            else:
                logger.warning(
                    "[Observability] LANGFUSE_TENANT_KEYS entry for tenant {} is missing public_key/secret_key; skipped",
                    tenant_id,
                )
        _tenant_keys = normalized
    except Exception as exc:  # noqa: BLE001 — observability is best-effort
        _tenant_keys = {}
        logger.warning("[Observability] failed to parse LANGFUSE_TENANT_KEYS; tenant isolation disabled: {}", exc)
    return _tenant_keys


def set_run_identity(**identity: Any) -> None:
    """Record Run/tenant/agent identity for the current async context.

    Phase 2 populates this from ``RuntimeContext`` so every nested generation
    inherits the same Run identity without threading it through signatures.
    Empty and ``None`` values are dropped to keep trace metadata clean.
    """
    _run_identity.set({key: str(value) for key, value in identity.items() if value is not None and value != ""})


def _build_client(*, public_key: str, secret_key: str) -> Any:
    from langfuse import Langfuse  # lazy import (only when enabled)

    settings = get_settings()
    kwargs: dict[str, str] = {}
    if settings.LANGFUSE_HOST:
        kwargs["base_url"] = settings.LANGFUSE_HOST
    if settings.LANGFUSE_RELEASE:
        kwargs["release"] = settings.LANGFUSE_RELEASE
    return Langfuse(public_key=public_key, secret_key=secret_key, **kwargs)


def _get_client(tenant_id: str | None = None) -> Any:
    """Return the Langfuse client for a tenant, or the default client (None when disabled).

    A configured per-tenant key isolates that tenant's traces into its own
    Langfuse project; unmatched tenants fall back to the default client.
    """
    global _client, _client_error
    if not is_enabled():
        return None
    if tenant_id:
        creds = _tenant_key_map().get(tenant_id)
        if creds is not None:
            client = _tenant_clients.get(tenant_id)
            if client is not None:
                return client
            if tenant_id in _tenant_errors:
                return None
            try:
                client = _build_client(**creds)
                _tenant_clients[tenant_id] = client
                return client
            except Exception as exc:  # noqa: BLE001 — observability is best-effort
                _tenant_errors[tenant_id] = exc
                logger.warning(
                    "[Observability] failed to init Langfuse for tenant {}; tenant tracing disabled: {}",
                    tenant_id,
                    exc,
                )
                return None
    if _client is not None:
        return _client
    if _client_error is not None:
        return None
    try:
        settings = get_settings()
        _client = _build_client(public_key=settings.LANGFUSE_PUBLIC_KEY, secret_key=settings.LANGFUSE_SECRET_KEY)
    except Exception as exc:  # noqa: BLE001 — observability is best-effort
        _client_error = exc
        logger.warning("[Observability] failed to init Langfuse; tracing disabled: {}", exc)
    return _client


def _is_retry_control_flow(exc: BaseException) -> bool:
    """True when the exception schedules a command retry (not a run failure)."""
    return type(exc).__name__ in _RETRY_CONTROL_FLOW_NAMES


def _mask_string(value: str) -> str:
    result = value
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub("[REDACTED]", result)
    if len(result) > _MAX_STRING_CHARS:
        result = result[:_MAX_STRING_CHARS] + f"...<truncated {len(value)} chars>"
    return result


def mask_text(value: Any) -> Any:
    """Recursively redact secrets and bound payload size before trace export."""
    if isinstance(value, str):
        return _mask_string(value)
    if isinstance(value, dict):
        return {
            key: ("[REDACTED]" if isinstance(key, str) and key.lower() in _SENSITIVE_KEYS else mask_text(val))
            for key, val in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [mask_text(item) for item in value]
    if dataclasses.is_dataclass(value):
        return mask_text(dataclasses.asdict(value))
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return mask_text(vars(value))
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)[:_MAX_STRING_CHARS]


def _map_usage(
    usage: TokenUsage | dict[str, int] | None,
    *,
    provider: str | None = None,
) -> dict[str, int]:
    """Map Clawith token accounting onto Langfuse/OTel GenAI usage units.

    Langfuse prices every usage bucket independently and sums the per-bucket
    costs with no subtraction, so an ``input`` bucket that still contains cache
    hits would be billed at the (much higher) uncached input price on top of the
    ``input_cache_read`` price — double-billing every hit token. OpenAI-compatible
    providers (DeepSeek et al.) include cache hits in ``prompt_tokens``, so for
    them ``input`` must be reported as the uncached remainder. Anthropic and
    Gemini report ``input`` already excluding cache read/creation, so they keep
    the raw value.
    """
    if usage is None:
        return {}
    if not isinstance(usage, TokenUsage):
        usage = extract_token_usage(usage) or TokenUsage()
    input_tokens = usage.input_tokens
    if usage.cache_read_tokens and provider not in ("anthropic", "gemini"):
        input_tokens = usage.cache_miss_tokens or max(
            usage.input_tokens - usage.cache_read_tokens - usage.cache_creation_tokens,
            0,
        )
    # Langfuse requires `total` to equal the sum of the detail buckets (it
    # validates this and warns "Sum of provided non-total usage_details buckets
    # exceeds provided total" otherwise). Provider totals are not usable for
    # that: DeepSeek's total_tokens is a half-credit equivalent
    # (miss + completion + cache_hit/2), and Anthropic/Gemini totals exclude
    # cache read/write. The detail buckets are the real token counts, so sum
    # them instead of forwarding the provider total.
    total_tokens = input_tokens + usage.output_tokens + usage.cache_read_tokens + usage.cache_creation_tokens
    details: dict[str, int] = {
        "input": input_tokens,
        "output": usage.output_tokens,
        "total": total_tokens,
    }
    if usage.cache_read_tokens:
        details["input_cache_read"] = usage.cache_read_tokens
    if usage.cache_creation_tokens:
        details["input_cache_write"] = usage.cache_creation_tokens
    return details


class GenerationHandle:
    """Write-side of an in-flight generation observation. All writes are safe."""

    __slots__ = ("_span", "_mask", "_output", "_usage", "_metadata", "_level", "_status", "_provider")

    def __init__(self, span: Any, *, mask: bool, provider: str | None = None) -> None:
        self._span = span
        self._mask = mask
        self._provider = provider
        self._output: Any = _UNSET
        self._usage: dict[str, int] = {}
        self._metadata: dict[str, Any] = {}
        self._level: str | None = None
        self._status: str | None = None

    def set_output(self, output: Any) -> None:
        self._output = output

    def set_usage(self, usage: TokenUsage | dict[str, int] | None) -> None:
        self._usage = _map_usage(usage, provider=self._provider)

    def add_metadata(self, **values: Any) -> None:
        self._metadata.update({key: value for key, value in values.items() if value is not None})

    def mark_error(self, exc: BaseException) -> None:
        self._level = "ERROR"
        self._status = f"{type(exc).__name__}: {str(exc)[:400]}"

    def mark_retry(self, exc: BaseException) -> None:
        """Record a business retry-control-flow exit (not an error)."""
        self._metadata["retry_pending"] = True
        self._metadata["retry_type"] = type(exc).__name__
        self._status = f"{type(exc).__name__}: {str(exc)[:400]}"

    def finalize(self, started: float) -> None:
        """Apply accumulated output/usage/error/latency in one span update."""
        self._metadata.setdefault("latency_ms", round((time.perf_counter() - started) * 1000, 2))
        update: dict[str, Any] = {"metadata": self._metadata}
        if self._output is not _UNSET:
            update["output"] = mask_text(self._output) if self._mask else self._output
        if self._usage:
            update["usage_details"] = self._usage
        if self._level is not None:
            update["level"] = self._level
            update["status_message"] = self._status
        elif self._status is not None:
            # retry control flow: keep DEFAULT level, surface the reason
            update["status_message"] = self._status
        try:
            self._span.update(**update)
        except Exception:  # noqa: BLE001 — tracing must never break the call path
            pass


@contextmanager
def observe_generation(
    *,
    name: str,
    model: str | None = None,
    provider: str | None = None,
    agent_id: Any = None,
    input: Any = None,
    capture_input: bool = True,
) -> Iterator[GenerationHandle | None]:
    """Open a ``generation`` observation around one LLM call (no-op when disabled).

    Exceptions raised inside the block are recorded as ``level=ERROR`` and
    re-raised to the caller; observability-internal failures are swallowed.
    """
    identity = dict(_run_identity.get())
    if agent_id is not None:
        identity["agent_id"] = str(agent_id)
    if provider:
        identity["provider"] = provider

    client = _get_client(identity.get("tenant_id"))
    if client is None:
        yield None
        return

    span_input = mask_text(input) if (capture_input and input is not None) else None

    # Propagate native Langfuse user/session attributes so standalone
    # generations (no enclosing ``observe_run``) still group by user/session.
    prop_cm = _user_session_propagation(identity)
    try:
        start_cm = client.start_as_current_observation(
            as_type="generation",
            name=name,
            input=span_input,
            **({"model": model} if model else {}),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Observability] failed to start generation span: {}", exc)
        yield None
        return

    started = time.perf_counter()
    handle: GenerationHandle | None = None
    _deferred_retry: BaseException | None = None
    with ExitStack() as stack:
        if prop_cm is not None:
            stack.enter_context(prop_cm)
        with start_cm as span:
            handle = GenerationHandle(span, mask=True, provider=provider)
            handle.add_metadata(**identity)
            try:
                yield handle
            except BaseException as exc:
                if _is_retry_control_flow(exc):
                    # Re-raise AFTER the OTel span context exits: OTel's
                    # start_as_current_span marks the span ERROR on any
                    # exception inside the block, which would turn business
                    # retry control flow into a false failure.
                    handle.mark_retry(exc)
                    _deferred_retry = exc
                else:
                    handle.mark_error(exc)
                    raise
            finally:
                handle.finalize(started)
    if _deferred_retry is not None:
        raise _deferred_retry


@contextmanager
def _observe_span(
    *,
    as_type: str,
    name: str,
    model: str | None = None,
    input: Any = None,
    capture_input: bool = True,
    identity: dict[str, Any] | None = None,
) -> Iterator[GenerationHandle | None]:
    """Shared span machinery for tool/node observations (no-op when disabled)."""
    meta = dict(_run_identity.get())
    if identity:
        meta.update({key: str(value) for key, value in identity.items() if value is not None})

    client = _get_client(meta.get("tenant_id"))
    if client is None:
        yield None
        return

    span_input = mask_text(input) if (capture_input and input is not None) else None
    prop_cm = _user_session_propagation(meta)
    try:
        start_cm = client.start_as_current_observation(
            as_type=as_type,
            name=name,
            input=span_input,
            **({"model": model} if model else {}),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Observability] failed to start {} span: {}", as_type, exc)
        yield None
        return

    started = time.perf_counter()
    handle: GenerationHandle | None = None
    _deferred_retry: BaseException | None = None
    with ExitStack() as stack:
        if prop_cm is not None:
            stack.enter_context(prop_cm)
        with start_cm as span:
            handle = GenerationHandle(span, mask=True)
            handle.add_metadata(**meta)
            try:
                yield handle
            except BaseException as exc:
                if _is_retry_control_flow(exc):
                    handle.mark_retry(exc)
                    _deferred_retry = exc
                else:
                    handle.mark_error(exc)
                    raise
            finally:
                handle.finalize(started)
    if _deferred_retry is not None:
        raise _deferred_retry


@contextmanager
def observe_tool(
    *,
    tool_name: str,
    tool_call_id: Any = None,
    tool_execution_id: Any = None,
    **identity: Any,
) -> Iterator[GenerationHandle | None]:
    """Open a ``tool`` observation around one tool handler execution (no-op when disabled).

    Process-view only: the durable ledger (``agent_tool_executions``) remains the
    authoritative record of outcome/effect; this span records the execution
    latency/errors plus alignment keys (``tool_call_id``, ``tool_execution_id``)
    that link back to the ledger. Tool arguments are intentionally not captured.
    """
    meta: dict[str, Any] = {"tool_name": tool_name}
    if tool_call_id is not None:
        meta["tool_call_id"] = str(tool_call_id)
    if tool_execution_id is not None:
        meta["tool_execution_id"] = str(tool_execution_id)
    meta.update({key: value for key, value in identity.items() if value is not None})
    with _observe_span(as_type="tool", name=f"tool:{tool_name}", identity=meta) as handle:
        yield handle


@contextmanager
def observe_node(
    *,
    node: str,
    **identity: Any,
) -> Iterator[GenerationHandle | None]:
    """Open a ``span`` observation around one Runtime graph node (no-op when disabled)."""
    meta: dict[str, Any] = {"node": node}
    meta.update({key: value for key, value in identity.items() if value is not None})
    with _observe_span(as_type="span", name=f"node:{node}", identity=meta) as handle:
        yield handle


def _user_session_propagation(identity: dict[str, Any]) -> Any | None:
    """Build a Langfuse ``propagate_attributes`` CM for native session/user, or None."""
    session_id = identity.get("session_id")
    user_id = identity.get("actor_user_id")
    if not session_id and not user_id:
        return None
    try:
        from langfuse import propagate_attributes

        return propagate_attributes(
            user_id=str(user_id) if user_id is not None else None,
            session_id=str(session_id) if session_id is not None else None,
        )
    except Exception:  # noqa: BLE001 — observability is best-effort
        return None


class RunHandle:
    """Write-side of an in-flight run-level observation (root span). All writes are safe."""

    __slots__ = ("_span", "_metadata", "_level", "_status")

    def __init__(self, span: Any) -> None:
        self._span = span
        self._metadata: dict[str, Any] = {}
        self._level: str | None = None
        self._status: str | None = None

    def add_metadata(self, **values: Any) -> None:
        self._metadata.update({key: value for key, value in values.items() if value is not None})

    def mark_error(self, exc: BaseException) -> None:
        self._level = "ERROR"
        self._status = f"{type(exc).__name__}: {str(exc)[:400]}"

    def mark_retry(self, exc: BaseException) -> None:
        """Record a business retry-control-flow exit (not an error)."""
        self._metadata["retry_pending"] = True
        self._metadata["retry_type"] = type(exc).__name__
        self._status = f"{type(exc).__name__}: {str(exc)[:400]}"

    def finalize(self, started: float) -> None:
        """Apply accumulated metadata/error/latency in one span update."""
        self._metadata.setdefault("latency_ms", round((time.perf_counter() - started) * 1000, 2))
        update: dict[str, Any] = {"metadata": self._metadata}
        if self._level is not None:
            update["level"] = self._level
            update["status_message"] = self._status
        elif self._status is not None:
            # retry control flow: keep DEFAULT level, surface the reason
            update["status_message"] = self._status
        try:
            self._span.update(**update)
        except Exception:  # noqa: BLE001 — tracing must never break the call path
            pass


@contextmanager
def observe_run(
    *,
    run_id: Any,
    command_id: Any,
    tenant_id: Any,
    agent_id: Any = None,
    session_id: Any = None,
    actor_user_id: Any = None,
    **identity: Any,
) -> Iterator[RunHandle | None]:
    """Open the root trace for one Runtime command execution (no-op when disabled).

    The root span becomes the Langfuse trace; user/session/trace-name attributes
    are propagated to every nested observation so a Run renders as one trace
    grouped by session/user. Exceptions inside the block are recorded as
    ``level=ERROR`` and re-raised to the caller; observability-internal failures
    are swallowed.
    """
    client = _get_client(str(tenant_id) if tenant_id is not None else None)
    if client is None:
        yield None
        return

    meta: dict[str, str] = {
        "run_id": str(run_id),
        "command_id": str(command_id),
        "tenant_id": str(tenant_id),
    }
    for key, value in (
        ("agent_id", agent_id),
        ("session_id", session_id),
        ("actor_user_id", actor_user_id),
        *identity.items(),
    ):
        if value is not None:
            meta[key] = str(value)
    set_run_identity(**meta)

    try:
        from langfuse import propagate_attributes

        prop_cm = propagate_attributes(
            user_id=meta.get("actor_user_id"),
            session_id=meta.get("session_id"),
            trace_name=f"run:{run_id}",
            metadata={key: meta[key] for key in ("tenant_id", "agent_id", "run_id", "command_id") if key in meta},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Observability] failed to start run trace: {}", exc)
        yield None
        return

    with ExitStack() as stack:
        stack.enter_context(prop_cm)
        try:
            start_cm = client.start_as_current_observation(
                as_type="span",
                name="run",
                metadata=meta,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Observability] failed to start run span: {}", exc)
            yield None
            return
        with start_cm as span:
            handle = RunHandle(span)
            handle.add_metadata(**meta)
            started = time.perf_counter()
            _deferred_retry: BaseException | None = None
            try:
                yield handle
            except BaseException as exc:
                if _is_retry_control_flow(exc):
                    handle.mark_retry(exc)
                    _deferred_retry = exc
                else:
                    handle.mark_error(exc)
                    raise
            finally:
                handle.finalize(started)
        if _deferred_retry is not None:
            raise _deferred_retry


def flush() -> None:
    """Flush any pending trace exports across all clients (no-op when disabled)."""
    clients: list[Any] = []
    default_client = _get_client()
    if default_client is not None:
        clients.append(default_client)
    clients.extend(_tenant_clients.values())
    for client in clients:
        try:
            client.flush()
        except Exception:  # noqa: BLE001
            pass
