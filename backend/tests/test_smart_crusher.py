"""smart_crusher 单元测试。"""
import sys
sys.path.insert(0, "/Users/shubinzhang/Documents/agent/Clawith/backend")

from app.services.llm.smart_crusher import (
    _lossless_compact, anchor_crush, parse_json_tiered, smart_crush,
)

def test_parse_json_tiered_strict():
    data, tier = parse_json_tiered('{"a": 1}')
    assert tier == "strict" and data == {"a": 1}

def test_parse_json_tiered_relaxed():
    data, tier = parse_json_tiered('{"a": 1,}')
    assert tier == "relaxed" and data == {"a": 1}

def test_parse_json_tiered_text():
    data, tier = parse_json_tiered("plain text")
    assert tier == "text" and data is None

def test_lossless_compact():
    raw = '{\n  "key": "value"\n}'
    compact, ok = _lossless_compact(raw)
    assert ok and "\n" not in compact

def test_anchor_crush():
    out = anchor_crush("\n".join(f"line {i}" for i in range(200)))
    assert len(out.split("\n")) < 200
