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
from contextlib import contextmanager
from typing import Any, Iterator

from loguru import logger

from app.config import get_settings
from app.services.token_tracker import TokenUsage, extract_token_usage

__all__ = [
    "GenerationHandle",
    "flush",
    "is_enabled",
    "mask_text",
    "observe_generation",
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

_client: Any = None
_client_error: Exception | None = None

_run_identity: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "clawith_observability_identity", default={}
)


def is_enabled() -> bool:
    """Whether trace export is configured (independent of SDK health)."""
    settings = get_settings()
    return bool(
        settings.OBSERVABILITY_ENABLED
        and settings.LANGFUSE_PUBLIC_KEY
        and settings.LANGFUSE_SECRET_KEY
    )


def set_run_identity(**identity: Any) -> None:
    """Record Run/tenant/agent identity for the current async context.

    Phase 2 populates this from ``RuntimeContext`` so every nested generation
    inherits the same Run identity without threading it through signatures.
    Empty and ``None`` values are dropped to keep trace metadata clean.
    """
    _run_identity.set(
        {
            key: str(value)
            for key, value in identity.items()
            if value is not None and value != ""
        }
    )


def _get_client() -> Any:
    global _client, _client_error
    if not is_enabled():
        return None
    if _client is not None:
        return _client
    if _client_error is not None:
        return None
    try:
        from langfuse import Langfuse  # lazy import (only when enabled)

        settings = get_settings()
        kwargs: dict[str, str] = {}
        if settings.LANGFUSE_HOST:
            kwargs["base_url"] = settings.LANGFUSE_HOST
        _client = Langfuse(
            public_key=settings.LANGFUSE_PUBLIC_KEY,
            secret_key=settings.LANGFUSE_SECRET_KEY,
            **kwargs,
        )
    except Exception as exc:  # noqa: BLE001 — observability is best-effort
        _client_error = exc
        logger.warning("[Observability] failed to init Langfuse; tracing disabled: {}", exc)
    return _client


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


def _map_usage(usage: TokenUsage | dict[str, int] | None) -> dict[str, int]:
    """Map Clawith token accounting onto Langfuse/OTel GenAI usage units."""
    if usage is None:
        return {}
    if not isinstance(usage, TokenUsage):
        usage = extract_token_usage(usage) or TokenUsage()
    details: dict[str, int] = {
        "input": usage.input_tokens,
        "output": usage.output_tokens,
        "total": usage.total_tokens,
    }
    if usage.cache_read_tokens:
        details["input_cache_read"] = usage.cache_read_tokens
    if usage.cache_creation_tokens:
        details["input_cache_write"] = usage.cache_creation_tokens
    return details


class GenerationHandle:
    """Write-side of an in-flight generation observation. All writes are safe."""

    __slots__ = ("_span", "_mask", "_output", "_usage", "_metadata", "_level", "_status")

    def __init__(self, span: Any, *, mask: bool) -> None:
        self._span = span
        self._mask = mask
        self._output: Any = _UNSET
        self._usage: dict[str, int] = {}
        self._metadata: dict[str, Any] = {}
        self._level: str | None = None
        self._status: str | None = None

    def set_output(self, output: Any) -> None:
        self._output = output

    def set_usage(self, usage: TokenUsage | dict[str, int] | None) -> None:
        self._usage = _map_usage(usage)

    def add_metadata(self, **values: Any) -> None:
        self._metadata.update({key: value for key, value in values.items() if value is not None})

    def mark_error(self, exc: BaseException) -> None:
        self._level = "ERROR"
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
    client = _get_client()
    if client is None:
        yield None
        return

    identity = dict(_run_identity.get())
    if agent_id is not None:
        identity["agent_id"] = str(agent_id)
    if provider:
        identity["provider"] = provider

    span_input = mask_text(input) if (capture_input and input is not None) else None

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
    with start_cm as span:
        handle = GenerationHandle(span, mask=True)
        handle.add_metadata(**identity)
        try:
            yield handle
        except BaseException as exc:
            handle.mark_error(exc)
            raise
        finally:
            handle.finalize(started)


def flush() -> None:
    """Flush any pending trace exports (no-op when disabled)."""
    client = _get_client()
    if client is not None:
        try:
            client.flush()
        except Exception:  # noqa: BLE001
            pass
