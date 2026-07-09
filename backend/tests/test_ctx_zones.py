"""Wave6 三区 + PrefixCacheTracker + reactive_fold + offload_dropped 单测（不依赖真实 PG）。"""
import sys

sys.path.insert(0, "/Users/shubinzhang/Documents/agent/Clawith/backend")

import pytest

from app.services.llm.client import LLMMessage
from app.services.llm import ccr_offload, context_zones
from app.services.llm.context_zones import (
    PrefixCacheTracker,
    compute_zones,
    reactive_fold_messages,
)


def _est(msgs, model_name=""):
    """测试用 token 估算：内容字符数 // 4。"""
    total = 0
    for m in msgs:
        c = getattr(m, "content", None)
        if isinstance(c, str):
            total += len(c)
    return total // 4


def _history(rounds: int, *, tool_name: str = "search_clawhub", content_len: int = 2000):
    """system + rounds 轮 (assistant tool_calls + tool)。"""
    msgs = [LLMMessage(role="system", content="SYSTEM-PROMPT")]
    for r in range(rounds):
        tc = f"tc{r}"
        msgs.append(LLMMessage(role="assistant", content="",
                               tool_calls=[{"id": tc, "function": {"name": tool_name, "arguments": "{}"}}]))
        msgs.append(LLMMessage(role="tool", content="x" * content_len, tool_call_id=tc))
    return msgs


# ── compute_zones ──

def test_compute_zones_basic():
    msgs = _history(rounds=6)
    z = compute_zones(msgs, frozen_count=1, live_rounds=2)
    assert z.frozen_end == 1
    starts = [i for i, m in enumerate(msgs) if m.role == "assistant" and m.tool_calls]
    assert z.live_start == starts[-2]
    assert z.frozen_end < z.live_start  # 存在 compressible 中段


def test_compute_zones_insufficient_rounds_empty_compressible():
    msgs = _history(rounds=2)
    z = compute_zones(msgs, frozen_count=1, live_rounds=10)
    assert z.live_start == z.frozen_end  # 轮数不足 → 无 compressible


# ── PrefixCacheTracker ──

def test_tracker_stable_prefix_growth():
    t = PrefixCacheTracker(min_frozen=2)
    m1 = [LLMMessage(role="system", content="s"), LLMMessage(role="user", content="u1")]
    assert t.observe(m1, cache_read=0, ctx_window=1000) == 2  # 首轮回退到 min_frozen
    m2 = m1 + [LLMMessage(role="assistant", content="a1")]
    assert t.observe(m2, cache_read=500, ctx_window=1000) == 2


def test_tracker_cliff_shrinks_to_min():
    t = PrefixCacheTracker(min_frozen=2)
    m = [LLMMessage(role="system", content="s")] + [LLMMessage(role="user", content=f"u{i}") for i in range(5)]
    t.observe(m, cache_read=8000, ctx_window=10000)
    t.observe(m, cache_read=8000, ctx_window=10000)  # 建立高 cache_read 基线
    fz = t.observe(m, cache_read=1000, ctx_window=10000)  # 断崖：骤降 >50%
    assert fz == t.min_frozen


def test_tracker_overlay_reuses_prefix_objects():
    t = PrefixCacheTracker(min_frozen=2)
    m1 = [LLMMessage(role="system", content="s"), LLMMessage(role="user", content="u")]
    t.observe(m1, cache_read=0, ctx_window=1000)
    t.note_forwarded(m1)
    m2 = [LLMMessage(role="system", content="s"), LLMMessage(role="user", content="u"),
          LLMMessage(role="assistant", content="a")]
    t.observe(m2, cache_read=400, ctx_window=1000)
    out = t.overlay(m2)
    assert out[0] is m1[0] and out[1] is m1[1]  # 复用上轮对象，保证字节一致


# ── offload_dropped_messages ──

@pytest.mark.asyncio
async def test_offload_dropped_dual_adapter(monkeypatch):
    async def _fake_store(**kw):
        return "h" * 64

    monkeypatch.setattr(ccr_offload, "store_entry", _fake_store)
    dropped = [
        LLMMessage(role="assistant", content="", tool_calls=[{"id": "t0", "function": {"name": "search_clawhub", "arguments": "{}"}}]),
        LLMMessage(role="tool", content="x" * 2000, tool_call_id="t0"),
        {"role": "user", "content": "old user message long enough to store"},
    ]
    markers, n = await ccr_offload.offload_dropped_messages(
        dropped, session_id="s1", agent_id="a1", ctx_path="acp",
    )
    assert n >= 2
    assert all(mk.startswith("<!-- ccr:") for mk in markers)


@pytest.mark.asyncio
async def test_offload_dropped_empty_or_no_session():
    markers, n = await ccr_offload.offload_dropped_messages([], session_id="s", agent_id="a", ctx_path="acp")
    assert n == 0 and markers == []
    markers2, n2 = await ccr_offload.offload_dropped_messages(
        [LLMMessage(role="tool", content="x" * 2000, tool_call_id="t")],
        session_id="", agent_id="a", ctx_path="acp",
    )
    assert n2 == 0 and markers2 == []


# ── reactive_fold_messages ──

@pytest.mark.asyncio
async def test_reactive_fold_protects_frozen_and_drops_middle(monkeypatch):
    async def _fake_store(**kw):
        return "h" * 64

    monkeypatch.setattr(ccr_offload, "store_entry", _fake_store)
    msgs = _history(rounds=8)
    before_first = msgs[0]
    kept, folded = await reactive_fold_messages(
        msgs, frozen_head=1, ctx_window=1000, model_name="",
        session_id="s1", agent_id="a1", ctx_path="acp",
        low_water=0.5, live_rounds=2, est_tokens_fn=_est,
    )
    assert folded > 0
    assert kept[0] is before_first  # frozen 前缀对象未动（字节不变）
    assert any(isinstance(m, LLMMessage) and m.role == "user"
               and "折叠归档" in (m.content or "") for m in kept)  # 边界摘要
    assert _est(kept) <= _est(msgs)  # token 下降


@pytest.mark.asyncio
async def test_reactive_fold_skips_tier1_rounds(monkeypatch):
    async def _fake_store(**kw):
        return "h" * 64

    monkeypatch.setattr(ccr_offload, "store_entry", _fake_store)
    msgs = _history(rounds=8, tool_name="read_file")  # 全 Tier1 → 均不可折叠
    kept, folded = await reactive_fold_messages(
        msgs, frozen_head=1, ctx_window=1000, model_name="",
        session_id="s1", agent_id="a1", ctx_path="acp",
        low_water=0.5, live_rounds=2, est_tokens_fn=_est,
    )
    assert folded == 0  # Tier1 守卫：一条都不折叠
    assert kept is msgs
