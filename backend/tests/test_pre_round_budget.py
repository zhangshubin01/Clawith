"""P1：pre_round_budget 阶梯与 fold abort 单测。"""
import sys

sys.path.insert(0, "/Users/shubinzhang/Documents/agent/Clawith/backend")

import pytest

from app.services.llm.compression_config import pre_round_budget, pre_round_budget_post_fold
from app.services.llm.client import LLMMessage
from app.services.llm.context_zones import reactive_fold_messages


def test_pre_round_budget_fold_at_60():
    b = pre_round_budget(0.62, retrieve_avail=True, round_i=10, last_fold_round=-100)
    assert b.should_enter
    assert b.should_fold
    assert b.action == "fold"


def test_pre_round_budget_noop_below_60():
    b = pre_round_budget(0.59, retrieve_avail=True, round_i=10)
    assert not b.should_enter
    assert not b.should_fold


def test_pre_round_budget_no_fold_without_retrieve():
    b = pre_round_budget(0.70, retrieve_avail=False, round_i=10)
    assert b.should_enter
    assert not b.should_fold
    assert b.skip_reason == "no_retrieve_tool"


def test_pre_round_budget_cooldown():
    b = pre_round_budget(0.70, retrieve_avail=True, round_i=5, last_fold_round=4)
    assert not b.should_fold
    assert b.skip_reason == "fold_cooldown"


def test_post_fold_layer1_emergency_only_with_cache():
    b = pre_round_budget_post_fold(
        0.90, tokens_after_fold=90_000, ctx_window=100_000, cache_read=50_000, fold_ran=False,
    )
    assert b.should_layer1

    b2 = pre_round_budget_post_fold(
        0.70, tokens_after_fold=70_000, ctx_window=100_000, cache_read=50_000, fold_ran=False,
    )
    assert not b2.should_layer1
    assert b2.skip_reason == "cache_safe_below_emergency"


def test_post_fold_blocks_after_successful_fold():
    b = pre_round_budget_post_fold(
        0.48, tokens_after_fold=45_000, ctx_window=100_000, cache_read=0, fold_ran=True,
    )
    assert not b.should_layer1
    assert b.skip_reason == "fold_sufficient"


@pytest.mark.asyncio
async def test_fold_aborts_on_ccr_failure(monkeypatch):
    async def _fail_store(**kw):
        return ""

    from app.services.llm import ccr_offload
    monkeypatch.setattr(ccr_offload, "store_entry", _fail_store)
    msgs = [LLMMessage(role="system", content="s")]
    for r in range(8):
        tc = f"tc{r}"
        msgs.append(LLMMessage(role="assistant", content="", tool_calls=[
            {"id": tc, "function": {"name": "search_clawhub", "arguments": "{}"}},
        ]))
        msgs.append(LLMMessage(role="tool", content="x" * 2000, tool_call_id=tc))

    def _est(m, model_name=""):
        return sum(len(getattr(x, "content", "") or "") for x in m) // 4

    kept, folded = await reactive_fold_messages(
        msgs, frozen_head=1, ctx_window=1000, model_name="",
        session_id="s1", agent_id="a1", ctx_path="acp",
        low_water=0.5, live_rounds=2, est_tokens_fn=_est,
    )
    assert folded == 0
    assert len(kept) == len(msgs)
