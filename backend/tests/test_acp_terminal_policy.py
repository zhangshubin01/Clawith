"""ACP terminal_policy 分类回归 — 与 IDE TerminalService.timeoutForCommand 对齐。"""

import os

import pytest

from app.plugins.clawith_acp.terminal_policy import (
    TerminalMode,
    effective_terminal_mode,
    resolve_terminal_policy,
    routing_mode_from_env,
)


def test_gradlew_test_is_build_tool_300s():
    policy = resolve_terminal_policy("./gradlew test --tests 'com.foo.*'")
    assert policy.mode == TerminalMode.BLOCKING
    assert policy.timeout_seconds == 300.0
    assert "build-tool" in policy.bucket


def test_npm_test_is_long_600s():
    policy = resolve_terminal_policy("npm test")
    assert policy.mode == TerminalMode.BLOCKING
    assert policy.timeout_seconds == 600.0
    assert "long" in policy.bucket


def test_tail_f_is_streaming_600s():
    policy = resolve_terminal_policy("tail -f /tmp/log")
    assert policy.mode == TerminalMode.STREAMING
    assert policy.timeout_seconds == 600.0


def test_default_short_command_blocking_30s():
    policy = resolve_terminal_policy("git status")
    assert policy.mode == TerminalMode.BLOCKING
    assert policy.timeout_seconds == 30.0


def test_routing_env_override(monkeypatch):
    monkeypatch.setenv("ACP_TERMINAL_ROUTING", "streaming")
    policy = resolve_terminal_policy("git status")
    assert effective_terminal_mode(policy) == TerminalMode.STREAMING
    monkeypatch.setenv("ACP_TERMINAL_ROUTING", "blocking")
    policy = resolve_terminal_policy("tail -f x")
    assert effective_terminal_mode(policy) == TerminalMode.BLOCKING
    monkeypatch.delenv("ACP_TERMINAL_ROUTING", raising=False)
    assert routing_mode_from_env() == "auto"
