"""ACP turn 分层计时 — workflow 墙钟 vs compute 预算，HITL 等待暂停 compute 时钟。"""
from __future__ import annotations

import os
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from app.debug_session_log import debug_session_log

# 需 IDE 权限弹窗的 RPC — 与 acp_handler / tool_bridge 共用
PERMISSION_GATED_METHODS: frozenset[str] = frozenset({"fs/safe_delete"})


class BudgetExceededError(Exception):
    """Turn 预算耗尽。"""

    def __init__(self, reason: str, *, stats: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.stats = stats or {}


_current_turn_budget: ContextVar["TurnBudget | None"] = ContextVar("acp_turn_budget", default=None)


def get_turn_budget() -> "TurnBudget | None":
    return _current_turn_budget.get()


def set_turn_budget(budget: "TurnBudget | None") -> None:
    _current_turn_budget.set(budget)


def _workflow_timeout_seconds() -> float:
    return float(os.getenv("ACP_WORKFLOW_TIMEOUT_SECONDS", "7200"))


def _compute_budget_seconds() -> float:
    if "ACP_COMPUTE_BUDGET_SECONDS" in os.environ:
        return float(os.environ["ACP_COMPUTE_BUDGET_SECONDS"])
    if "ACP_LLM_TIMEOUT_SECONDS" in os.environ:
        return float(os.environ["ACP_LLM_TIMEOUT_SECONDS"])
    return 1800.0


def default_tool_timeout_seconds() -> float:
    return float(os.getenv("ACP_TOOL_TIMEOUT_SECONDS", "180"))


def default_llm_call_timeout_seconds() -> float:
    return float(os.getenv("ACP_LLM_CALL_TIMEOUT_SECONDS", "120"))


@dataclass
class TurnBudget:
    """双时钟：workflow 含 HITL 墙钟；compute 在 HITL suspend 期间暂停。"""

    workflow_seconds: float
    compute_seconds: float
    _start_mono: float = field(default_factory=time.monotonic)
    _compute_deadline: float = field(init=False)
    _suspended_at: float | None = None
    _hitl_spent: float = 0.0
    _tools_completed: int = 0
    _last_hitl_reason: str = ""

    def __post_init__(self) -> None:
        self._compute_deadline = self._start_mono + self.compute_seconds

    @classmethod
    def from_env(cls) -> "TurnBudget":
        wf = _workflow_timeout_seconds()
        cb = _compute_budget_seconds()
        budget = cls(workflow_seconds=wf, compute_seconds=cb)
        logger.info(
            "[ACP-BUDGET] prompt_start workflow_s={} compute_s={}",
            wf,
            cb,
        )
        return budget

    def remaining_workflow(self) -> float:
        return max(0.0, (self._start_mono + self.workflow_seconds) - time.monotonic())

    def remaining_compute(self) -> float:
        return max(0.0, self._compute_deadline - time.monotonic())

    def suspend_for_hitl(self, reason: str) -> None:
        if self._suspended_at is not None:
            return
        self._suspended_at = time.monotonic()
        self._last_hitl_reason = reason
        logger.debug("[ACP-BUDGET] hitl_suspend reason={}", reason)
        # #region agent log
        debug_session_log(
            "H-HITL",
            "turn_budget.py:suspend_for_hitl",
            "hitl_suspend",
            {"reason": reason},
        )
        # #endregion

    def resume_from_hitl(self) -> None:
        if self._suspended_at is None:
            return
        elapsed = time.monotonic() - self._suspended_at
        self._compute_deadline += elapsed
        self._hitl_spent += elapsed
        logger.debug(
            "[ACP-BUDGET] hitl_resume reason={} duration_s={:.1f} hitl_total_s={:.1f}",
            self._last_hitl_reason,
            elapsed,
            self._hitl_spent,
        )
        # #region agent log
        debug_session_log(
            "H-HITL",
            "turn_budget.py:resume_from_hitl",
            "hitl_resume",
            {
                "reason": self._last_hitl_reason,
                "duration_s": round(elapsed, 2),
                "hitl_total_s": round(self._hitl_spent, 2),
            },
        )
        # #endregion
        self._suspended_at = None
        self._last_hitl_reason = ""

    def record_tool_completed(self) -> None:
        self._tools_completed += 1

    def check_compute_or_raise(self) -> None:
        if self.remaining_compute() <= 0:
            raise BudgetExceededError("compute_exceeded", stats=self.to_audit_dict())

    def check_workflow_or_raise(self) -> None:
        if self.remaining_workflow() <= 0:
            raise BudgetExceededError("workflow_exceeded", stats=self.to_audit_dict())

    def cap_timeout(self, desired: float, *, for_hitl: bool = False) -> float:
        """子调用 timeout = min(desired, 剩余预算)；HITL 仅受 workflow 约束。"""
        floor = 0.1
        if for_hitl:
            capped = min(desired, self.remaining_workflow())
        else:
            capped = min(desired, self.remaining_compute(), self.remaining_workflow())
        return max(floor, capped)

    def elapsed_workflow(self) -> float:
        return time.monotonic() - self._start_mono

    def to_audit_dict(self) -> dict[str, Any]:
        return {
            "workflow_seconds": self.workflow_seconds,
            "compute_seconds": self.compute_seconds,
            "elapsed_workflow_s": round(self.elapsed_workflow(), 2),
            "remaining_workflow_s": round(self.remaining_workflow(), 2),
            "remaining_compute_s": round(self.remaining_compute(), 2),
            "hitl_suspended_s": round(self._hitl_spent, 2),
            "tools_completed": self._tools_completed,
            "hitl_active": self._suspended_at is not None,
        }
