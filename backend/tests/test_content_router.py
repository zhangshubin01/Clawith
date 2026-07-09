"""content_router 单元测试。"""
import sys
sys.path.insert(0, "/Users/shubinzhang/Documents/agent/Clawith/backend")

from app.services.llm.content_router import compress_one, _faithful_search
from app.services.llm.compression_config import is_tool_excluded

def test_read_file_excluded():
    big = "x" * 10000
    assert compress_one(big, tool_name="read_file") == big

def test_faithful_grep():
    g = "./a.py:1: x\n./b.py:2: y\n" + "\n".join(f"./f{i}.py:{i}: m" for i in range(5))
    assert _faithful_search(g) == g

def test_search_clawhub_not_excluded():
    assert not is_tool_excluded("search_clawhub")

def test_faithful_search_non_grep_falls_through():
    """非 grep 形态 search 不应被 faithful_search 短路为全文保留。"""
    blob = "Result 1: long description " * 400
    assert _faithful_search(blob) is None


def test_list_focus_items_not_excluded():
    assert not is_tool_excluded("list_focus_items")


def test_list_focus_items_compresses_large_list():
    lines = [f"- item {i}: metadata payload" for i in range(200)]
    big = "\n".join(lines)
    out = compress_one(big, tool_name="list_focus_items", budget_tokens=500, ctx_window=100000)
    assert len(out) < len(big)
    assert "行已省略" in out or "<!-- ctx:trimmed -->" in out

