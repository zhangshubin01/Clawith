"""批次 A（2.3 hard_ceil + 2.1 excluded 三层）单元测试。"""
import sys

sys.path.insert(0, "/Users/shubinzhang/Documents/agent/Clawith/backend")

from app.services.llm.compression_config import (
    exclude_tier,
    is_tier1_strict,
    is_tier2_lossless_only,
    is_tool_excluded,
)
from app.services.llm.content_router import compress_one_result
from app.services.llm.context_compressor import _get_ctx_guard_ratios
from app.services.llm.tool_trim import _TOOL_HARD_CEIL_CHARS


def test_hard_ceil_chars_is_16k():
    assert _TOOL_HARD_CEIL_CHARS == 16384


def test_exclude_tiers():
    assert exclude_tier("read_file") == 1
    assert exclude_tier("list_files") == 2
    assert exclude_tier("search_clawhub") == 0
    assert is_tier1_strict("read_file")
    assert is_tier2_lossless_only("list_files")
    assert is_tool_excluded("read_file")
    assert is_tool_excluded("list_files")
    assert not is_tool_excluded("search_clawhub")


def test_tier1_verbatim_when_low_session_pressure():
    big = "x" * 200
    r = compress_one_result(
        big,
        tool_name="read_file",
        budget_tokens=500,
        model_name="",
        session_pressure=0.2,
    )
    assert r.content == big
    assert r.strategy == "tier1_verbatim"


def test_tier1_compresses_when_high_session_pressure_and_over_budget():
    big = "import os\n" + ("def f():\n    pass\n" * 500)
    r = compress_one_result(
        big,
        tool_name="read_file",
        budget_tokens=100,
        model_name="",
        ctx_window=100000,
        session_pressure=0.7,
    )
    assert r.changed
    assert len(r.content) < len(big)


def test_deepseek_guard_ratios_lowered():
    warn, compress = _get_ctx_guard_ratios("deepseek-v4-flash")
    assert warn == 0.60
    assert compress == 0.80
