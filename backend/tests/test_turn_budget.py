"""TurnBudget 分层计时单元测试。"""
import os
import sys
import time

import pytest

sys.path.insert(0, "/Users/shubinzhang/Documents/agent/Clawith/backend")

from app.plugins.clawith_acp.turn_budget import (
    BudgetExceededError,
    TurnBudget,
    _compute_budget_seconds,
)


def test_hitl_suspend_does_not_consume_compute(monkeypatch):
    monkeypatch.delenv("ACP_COMPUTE_BUDGET_SECONDS", raising=False)
    monkeypatch.delenv("ACP_LLM_TIMEOUT_SECONDS", raising=False)
    b = TurnBudget(workflow_seconds=7200, compute_seconds=600)
    before = b.remaining_compute()
    b.suspend_for_hitl("fs/safe_delete")
    time.sleep(0.05)
    b.resume_from_hitl()
    after = b.remaining_compute()
    assert after >= before - 0.02


def test_three_hitl_suspends_preserve_compute(monkeypatch):
    b = TurnBudget(workflow_seconds=7200, compute_seconds=600)
    initial = b.remaining_compute()
    for _ in range(3):
        b.suspend_for_hitl("fs/safe_delete")
        time.sleep(0.02)
        b.resume_from_hitl()
    assert b.remaining_compute() >= initial - 0.1
    assert b.to_audit_dict()["hitl_suspended_s"] >= 0.05


def test_cap_timeout_respects_remaining():
    b = TurnBudget(workflow_seconds=7200, compute_seconds=600)
    b._compute_deadline = time.monotonic() + 30
    assert b.cap_timeout(180) == pytest.approx(30, abs=0.05)


def test_compute_exceeded_raises():
    b = TurnBudget(workflow_seconds=7200, compute_seconds=0.001)
    time.sleep(0.01)
    with pytest.raises(BudgetExceededError) as exc:
        b.check_compute_or_raise()
    assert exc.value.reason == "compute_exceeded"


def test_llm_timeout_alias_for_compute(monkeypatch):
    monkeypatch.delenv("ACP_COMPUTE_BUDGET_SECONDS", raising=False)
    monkeypatch.setenv("ACP_LLM_TIMEOUT_SECONDS", "600")
    assert _compute_budget_seconds() == 600.0


def test_default_compute_budget_1800(monkeypatch):
    monkeypatch.delenv("ACP_COMPUTE_BUDGET_SECONDS", raising=False)
    monkeypatch.delenv("ACP_LLM_TIMEOUT_SECONDS", raising=False)
    assert _compute_budget_seconds() == 1800.0
