"""CCR 保真压缩 + reversibility gate 单元测试。

覆盖 P0 核心保真语义（不依赖 PG）：
- Layer 0 工具名路由：list_files 走 head+tail，绝不 _text 删行
- 无路由纯文本禁用 _text（回退原文）
- CTX_LOSSLESS_ONLY 全禁有损
- never_worse 回退
- CCR marker / hash / retrieved 工具工具函数
- reversibility gate：store 失败回退原文；成功注入 marker（monkeypatch store_entry）
"""
import sys
sys.path.insert(0, '/Users/shubinzhang/Documents/agent/Clawith/backend')

import pytest

from app.services.llm import ccr_store
from app.services.llm.compression_config import is_tool_excluded
from app.services.llm.emit_guarded import emit_guarded
from app.services.llm.tool_trim import (
    _dispatch_guarded,
    _list_head_tail,
    _TOOL_TYPE_ROUTE,
    _COMPRESS_MARKER,
)


# ── Layer 0 保真路由 ──

def test_list_head_tail_preserves_order_and_counts():
    lines = [f"file_{i}.txt" for i in range(200)]
    out = _list_head_tail("\n".join(lines), head=40, tail=20)
    o = out.split("\n")
    assert o[0] == "file_0.txt"
    assert o[39] == "file_39.txt"
    assert o[-1] == "file_199.txt"
    assert o[-20] == "file_180.txt"
    assert any("省略" in l for l in o)


def test_list_files_is_excluded_and_kept_verbatim_under_budget():
    assert _TOOL_TYPE_ROUTE["list_files"] == "list"
    assert is_tool_excluded("list_files")
    small = "\n".join(f"/ws/f{i}.md" for i in range(10))
    out = _dispatch_guarded(small, tool_name="list_files", budget_tokens=5000, model_name="")
    assert out == small
    assert _COMPRESS_MARKER not in out


def test_list_files_tier2_compresses_when_over_budget():
    big = "\n".join(f"/ws/f{i}.md" for i in range(500))
    out = _dispatch_guarded(big, tool_name="list_files", budget_tokens=100, model_name="")
    assert len(out) < len(big)
    assert _COMPRESS_MARKER in out


def test_configured_extra_exclude_tool(monkeypatch):
    import app.config as cfg

    class _S:
        CTX_EXCLUDE_TOOLS = "run_in_terminal"

    monkeypatch.setattr(cfg, "get_settings", lambda: _S())
    assert is_tool_excluded("run_in_terminal")


def test_text_tool_path_skips_lossy_text():
    # 无专用路由的纯文本，超预算也禁止 _text 删行 → 原样返回，无 marker
    para = ("word " * 50 + "\n") * 100
    out = _dispatch_guarded(para, tool_name="totally_unknown_tool", budget_tokens=10, model_name="")
    assert out == para
    assert _COMPRESS_MARKER not in out


def test_lossless_only_returns_original(monkeypatch):
    import app.config as cfg

    class _S:
        CTX_LOSSLESS_ONLY = True

    monkeypatch.setattr(cfg, "get_settings", lambda: _S())
    big = "\n".join(f"/ws/f{i}.md" for i in range(500))
    out = _dispatch_guarded(big, tool_name="list_files", budget_tokens=10, model_name="")
    assert out == big


def test_never_worse_reverts():
    small = "a\nb\nc"
    out = _dispatch_guarded(small, tool_name="search_clawhub", budget_tokens=1, model_name="")
    assert out == small


def test_emit_guarded_reverts_when_hint_makes_output_worse():
    original = "short original"
    compressed = "tiny"
    hint = "retrieve " * 100
    assert emit_guarded(compressed, hint, original, model_name="") == original


# ── CCR 工具函数 ──

def test_content_sha256_is_64_hex():
    h = ccr_store.content_sha256("hello world")
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_ccr_marker_and_is_retrieved():
    h = "a" * 64
    m = ccr_store.ccr_marker(h)
    assert f"ccr:{h}" in m
    assert "retrieve_context" in m
    assert not ccr_store.is_retrieved(m)
    assert ccr_store.is_retrieved(ccr_store._CCR_RETRIEVED_MARKER + "\nfull content")


def test_messages_have_ccr():
    assert ccr_store.messages_have_ccr([{"role": "tool", "content": "<!-- ccr:abc123 -->\n..."}])
    assert not ccr_store.messages_have_ccr([{"role": "user", "content": "hi"}])
    assert not ccr_store.messages_have_ccr([])


async def test_retrieve_context_supports_line_paging(monkeypatch):
    async def _fake_retrieve(session_id, content_hash):
        return "l0\nl1\nl2\nl3"

    monkeypatch.setattr(ccr_store, "retrieve_entry", _fake_retrieve)
    out = await ccr_store.retrieve_context_tool("sess1", {"hash": "a" * 64, "offset": 1, "limit": 2})
    assert out == ccr_store._CCR_RETRIEVED_MARKER + "\nl1\nl2"


def test_ccr_metrics_snapshot_is_redacted():
    snap = ccr_store.get_ccr_metrics_snapshot()
    assert snap["ccr"]["metrics"]["store_ok"] >= 0
    assert "mem_cache_size" in snap["ccr"]
    assert "ttl_hours" in snap["ccr"]["config"]
    forbidden = str(snap).lower()
    assert "content_hash" not in forbidden
    assert "session_id" not in forbidden
    assert "agent_id" not in forbidden


def test_layer1_keeps_retrieved_tool_content_verbatim():
    from app.services.llm.client import LLMMessage
    from app.services.llm.context_compressor import _multi_role_compress

    original = ccr_store._CCR_RETRIEVED_MARKER + "\n" + ("retrieved full line\n" * 2000)
    messages = [
        LLMMessage(role="system", content="system"),
        LLMMessage(role="tool", tool_call_id="t1", content=original),
    ]
    _multi_role_compress(messages, ctx_window=1000, model_name="")
    assert messages[1].content == original


# ── reversibility gate（headroom #1307）──

async def test_gate_reverts_on_store_failure(monkeypatch):
    from app.services.llm import caller

    async def _fake_store(**kwargs):
        return None  # 模拟 PG 写入失败

    monkeypatch.setattr("app.services.llm.ccr_store.store_entry", _fake_store)
    big = "\n".join(f"file.py:{i}: ERROR value {i}" for i in range(500))
    out = await caller._guarded_compress_with_ccr(
        big, "search_clawhub", 10, "", "acp", "sess1", None,
    )
    assert out == big  # store 失败 → gate skip → 原文


async def test_gate_injects_marker_on_store_ok(monkeypatch):
    from app.services.llm import caller

    async def _fake_store(**kwargs):
        return "b" * 64

    from app.services.llm.compression_result import CompressionResult, Lossiness

    def _fake_dispatch_result(original, tool_name, budget_tokens, model_name, **kwargs):
        return CompressionResult(
            content="compressed-summary",
            changed=True,
            lossiness=Lossiness.SUMMARY,
            recoverable=True,
            strategy="test",
            original_tokens=100,
            final_tokens=10,
        )

    monkeypatch.setattr("app.services.llm.ccr_store.store_entry", _fake_store)
    monkeypatch.setattr(caller, "_dispatch_guarded_result", _fake_dispatch_result)
    big = "x" * 5000
    out = await caller._guarded_compress_with_ccr(
        big, "search_clawhub", 10, "", "acp", "sess1", None,
    )
    assert out.startswith("<!-- ccr:" + "b" * 64 + " -->")
    assert len(out) < len(big)


async def test_gate_reverts_when_retrieve_tool_unavailable(monkeypatch):
    from app.services.llm import caller

    called = {"n": 0}

    async def _fake_store(**kwargs):
        called["n"] += 1
        return "d" * 64

    monkeypatch.setattr("app.services.llm.ccr_store.store_entry", _fake_store)
    big = "\n".join(f"file.py:{i}: ERROR value {i}" for i in range(500))
    out = await caller._guarded_compress_with_ccr(
        big, "search_clawhub", 10, "", "acp", "sess1", None, tools_available=False,
    )
    assert out == big
    assert called["n"] == 0


async def test_gate_noop_when_never_worse(monkeypatch):
    from app.services.llm import caller

    called = {"n": 0}

    async def _fake_store(**kwargs):
        called["n"] += 1
        return "c" * 64

    monkeypatch.setattr("app.services.llm.ccr_store.store_entry", _fake_store)
    small = "x\ny\nz"
    out = await caller._guarded_compress_with_ccr(
        small, "list_files", 1, "", "acp", "sess1", None,
    )
    assert out == small
    assert called["n"] == 0  # 未压缩 → 不 store

def test_layer1_p3_skips_excluded_read_file():
    """P3 不得对 Tier1 exclude 的 read_file 做 _text 有损压缩。"""
    from app.services.llm.client import LLMMessage
    from app.services.llm.context_compressor import _multi_role_compress

    original = "import os\n" + ("def f(): pass\n" * 800)
    messages = [
        LLMMessage(role="system", content="sys"),
        LLMMessage(
            role="assistant",
            content="read",
            tool_calls=[{"id": "tc1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}],
        ),
        LLMMessage(role="tool", tool_call_id="tc1", content=original),
    ]
    _multi_role_compress(messages, ctx_window=500, model_name="")
    assert messages[2].content == original

