"""Error classification tests for LLM failover decisions.

Includes a regression case for the 2026-08-17 ``model_call_failed`` incident:
a single transient DNS resolution failure (``[Errno -2] Name or service not
known``) must classify as RETRYABLE so the runtime retries instead of failing
the whole Run.
"""

import socket

import httpx
import pytest

from app.services.llm.client import LLMError, LLMRequestShapeError
from app.services.llm.failover import FailoverErrorType, classify_error


class TestClassifyNetworkErrors:
    def test_dns_resolution_failure_is_retryable(self):
        # The exact failure behind the 2026-08-17 model_call_failed incident.
        error = httpx.ConnectError("[Errno -2] Name or service not known")
        assert classify_error(error) is FailoverErrorType.RETRYABLE

    def test_socket_gaierror_is_retryable(self):
        error = socket.gaierror(-2, "Name or service not known")
        assert classify_error(error) is FailoverErrorType.RETRYABLE

    def test_builtin_connection_errors_are_retryable(self):
        assert classify_error(ConnectionRefusedError(111, "Connection refused")) is FailoverErrorType.RETRYABLE
        assert classify_error(ConnectionResetError(104, "Connection reset by peer")) is FailoverErrorType.RETRYABLE
        assert classify_error(BrokenPipeError("Broken pipe")) is FailoverErrorType.RETRYABLE

    def test_builtin_timeout_is_retryable(self):
        assert classify_error(TimeoutError("timed out")) is FailoverErrorType.RETRYABLE

    @pytest.mark.parametrize(
        "error",
        [
            httpx.ConnectTimeout("connect timeout"),
            httpx.ReadTimeout("read timeout"),
            httpx.WriteTimeout("write timeout"),
            httpx.PoolTimeout("pool timeout"),
            httpx.ReadError("read error"),
            httpx.WriteError("write error"),
            httpx.RemoteProtocolError("remote protocol error"),
        ],
    )
    def test_httpx_transport_errors_are_retryable(self, error):
        assert classify_error(error) is FailoverErrorType.RETRYABLE


class TestClassifyWrappedErrors:
    def test_wrapped_dns_error_is_retryable(self):
        cause = socket.gaierror(-2, "Name or service not known")
        error = LLMError("provider call failed")
        error.__cause__ = cause
        assert classify_error(error) is FailoverErrorType.RETRYABLE

    def test_deeply_wrapped_connect_error_is_retryable(self):
        inner = httpx.ConnectError("connect failed")
        middle = RuntimeError("http transport failed")
        middle.__cause__ = inner
        outer = LLMError("provider request failed")
        outer.__cause__ = middle
        assert classify_error(outer) is FailoverErrorType.RETRYABLE


class TestClassifyDeterministicErrors:
    def test_request_shape_error_is_non_retryable(self):
        error = LLMRequestShapeError("final provider request violates shape invariant")
        assert classify_error(error) is FailoverErrorType.NON_RETRYABLE

    def test_wrapped_request_shape_error_is_non_retryable(self):
        cause = LLMRequestShapeError("final provider request violates shape invariant")
        error = LLMError("provider call failed")
        error.__cause__ = cause
        assert classify_error(error) is FailoverErrorType.NON_RETRYABLE

    def test_http_401_is_non_retryable(self):
        request = httpx.Request("POST", "https://example.com/v1/chat/completions")
        response = httpx.Response(401, request=request)
        error = httpx.HTTPStatusError("Unauthorized", request=request, response=response)
        assert classify_error(error) is FailoverErrorType.NON_RETRYABLE

    def test_http_400_is_non_retryable(self):
        request = httpx.Request("POST", "https://example.com/v1/chat/completions")
        response = httpx.Response(400, request=request)
        error = httpx.HTTPStatusError("Bad Request", request=request, response=response)
        assert classify_error(error) is FailoverErrorType.NON_RETRYABLE

    def test_unknown_message_is_unknown(self):
        assert classify_error(Exception("something weird")) is FailoverErrorType.UNKNOWN


class TestClassifyKeywordErrors:
    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("429 rate limit exceeded", FailoverErrorType.RETRYABLE),
            ("502 server error", FailoverErrorType.RETRYABLE),
            ("connection timed out", FailoverErrorType.RETRYABLE),
            ("network is unreachable", FailoverErrorType.RETRYABLE),
            ("temporary failure in name resolution", FailoverErrorType.RETRYABLE),
            ("invalid api key", FailoverErrorType.NON_RETRYABLE),
            ("content policy violation", FailoverErrorType.NON_RETRYABLE),
        ],
    )
    def test_keyword_classification(self, message, expected):
        assert classify_error(RuntimeError(message)) is expected
