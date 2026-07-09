"""F6：单 prompt 深 loop 累积 — 动态 budget + 提前 Layer1。"""
import sys

sys.path.insert(0, "/Users/shubinzhang/Documents/agent/Clawith/backend")

from app.services.llm.compression_config import layer1_compress_threshold_ratio
from app.services.llm.content_router import compress_one_result
from app.services.llm.context_compressor import _est_tokens_str
from app.services.llm.tool_trim import _effective_tool_budget, _tool_token_budget


def test_effective_budget_unchanged_before_deep_start():
    base = _tool_token_budget("read_file", 100_000)
    assert _effective_tool_budget("read_file", 100_000, round_i=3) == base


def test_effective_budget_tightens_at_round_28():
    base = _tool_token_budget("list_focus_items", 200_000)
    deep = _effective_tool_budget("list_focus_items", 200_000, round_i=28)
    assert deep < base * 0.5


def test_deep_round_forces_tier2_compression():
    """2832 tok list 在 round 28 应超收紧 budget 并压缩。"""
    content = "item\n" * 1500
    tok = _est_tokens_str(content, "")
    budget = _effective_tool_budget("list_focus_items", 200_000, round_i=28)
    assert tok > budget
    r = compress_one_result(
        content,
        tool_name="list_focus_items",
        budget_tokens=budget,
        model_name="",
        ctx_window=200_000,
        session_pressure=0.2,
    )
    assert r.changed
    assert r.final_tokens < tok


def test_layer1_ratio_lowers_after_deep_start():
    assert layer1_compress_threshold_ratio(2, 0.80) == 0.80
    assert layer1_compress_threshold_ratio(12, 0.80) == 0.45
