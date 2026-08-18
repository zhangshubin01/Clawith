"""Unified LLM failover error classification.

Provides error classification for failover decisions across all execution paths.
"""

from __future__ import annotations

from enum import Enum
import socket

import httpx

from .client import LLMError, LLMRequestShapeError


class FailoverErrorType(Enum):
    """Classification of LLM errors for failover decisions."""

    RETRYABLE = "retryable"  # Network timeout, DNS, 429, 5xx, transient errors
    NON_RETRYABLE = "non_retryable"  # Auth, validation, schema errors
    UNKNOWN = "unknown"


# Exception types that are deterministic request failures: retrying the same
# request cannot succeed and only duplicates cost.
_NON_RETRYABLE_ERROR_TYPES = (LLMRequestShapeError,)

# Exception types that are transient network-layer failures: DNS resolution,
# connect/read/write errors and timeouts. httpx.TransportError covers
# ConnectError / ReadError / WriteError / TimeoutException / ProxyError and
# their subclasses.
_TRANSIENT_ERROR_TYPES = (
    socket.gaierror,
    ConnectionError,
    TimeoutError,
    httpx.TransportError,
)

_NETWORK_KEYWORDS = (
    "timeout",
    "connection",
    "network",
    "unreachable",
    "refused",
    "reset",
    "dns",
    "getaddrinfo",
    "name or service not known",
    "temporary failure in name resolution",
    "name resolution",
    "nxdomain",
    "cannot resolve",
    "resolve host",
)


def _error_chain(error: Exception) -> list[Exception]:
    """Walk the ``__cause__``/``__context__`` chain, cycle-safe and bounded."""
    chain: list[Exception] = []
    seen: set[int] = set()
    current: Exception | None = error
    while current is not None and len(chain) < 8:
        if id(current) in seen:
            break
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def classify_error(error: Exception) -> FailoverErrorType:
    """Classify an exception as retryable or non-retryable.

    Retryable errors:
    - Transient network-layer exceptions (DNS, connect/read/write, timeouts),
      detected by exception type across the full cause chain
    - Provider 429 (rate limit)
    - Provider 5xx (server errors)
    - Explicit transient provider errors

    Non-retryable errors:
    - Request-shape violations (LLMRequestShapeError)
    - Auth errors (401, 403)
    - Validation errors (400, 422)
    - Schema errors
    - Content policy violations
    """
    chain = _error_chain(error)

    # Non-retryable: deterministic request failures by exception type
    if any(isinstance(link, _NON_RETRYABLE_ERROR_TYPES) for link in chain):
        return FailoverErrorType.NON_RETRYABLE

    error_msg = str(error).lower()

    # Non-retryable: authentication and authorization
    if any(kw in error_msg for kw in ["auth", "unauthorized", "forbidden", "invalid api key", "api key invalid"]):
        return FailoverErrorType.NON_RETRYABLE

    # Non-retryable: validation and schema
    if any(kw in error_msg for kw in ["validation", "invalid request", "schema", "bad request"]):
        return FailoverErrorType.NON_RETRYABLE

    # Non-retryable: content policy
    if any(kw in error_msg for kw in ["content policy", "content_filter", "safety", "moderation"]):
        return FailoverErrorType.NON_RETRYABLE

    # Retryable: transient network-layer errors by exception type
    if any(isinstance(link, _TRANSIENT_ERROR_TYPES) for link in chain):
        return FailoverErrorType.RETRYABLE

    # Retryable: rate limiting
    if any(kw in error_msg for kw in ["rate limit", "429", "too many requests"]):
        return FailoverErrorType.RETRYABLE

    # Retryable: server errors
    if any(kw in error_msg for kw in ["500", "502", "503", "504", "server error", "internal error"]):
        return FailoverErrorType.RETRYABLE

    # Retryable: network and timeout
    if any(kw in error_msg for kw in _NETWORK_KEYWORDS):
        return FailoverErrorType.RETRYABLE

    # Retryable: transient errors
    if any(kw in error_msg for kw in ["temporary", "transient", "unavailable", "overloaded", "busy"]):
        return FailoverErrorType.RETRYABLE

    # LLMError with specific patterns
    if isinstance(error, (LLMError, Exception)):
        # Check the error message for HTTP status codes
        if any(code in error_msg for code in ["401", "403", "400", "422"]):
            return FailoverErrorType.NON_RETRYABLE
        if any(code in error_msg for code in ["429", "500", "502", "503", "504", "408"]):
            return FailoverErrorType.RETRYABLE

        # If it's an error result string, it's likely retryable by default
        if (
            error_msg.startswith("[llm error]")
            or error_msg.startswith("[llm call error]")
            or error_msg.startswith("[error]")
        ):
            return FailoverErrorType.RETRYABLE

    return FailoverErrorType.UNKNOWN


__all__ = [
    "FailoverErrorType",
    "classify_error",
]
