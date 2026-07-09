"""list_files 去重缓存与写后失效单测。"""
from __future__ import annotations

import asyncio

import pytest

from app.plugins.clawith_acp import list_dedup
from app.plugins.clawith_acp.tool_bridge import coalesce_or_execute, _inflight


@pytest.fixture(autouse=True)
def _clear_list_cache():
    list_dedup._ls_cache.clear()
    _inflight.clear()
    yield
    list_dedup._ls_cache.clear()
    _inflight.clear()


def test_normalize_list_key_defaults_depth_limit():
    key = list_dedup.normalize_list_key("sess1", "/proj", "src", {})
    assert key.endswith(":3:500")


def test_store_and_get_cached_list():
    key = list_dedup.normalize_list_key("s", "/c", "foo", {"depth": 2, "limit": 100})
    list_dedup.store_list_result(key, "📄 a.txt")
    assert list_dedup.get_cached_list(key) == "📄 a.txt"


def test_invalidate_after_write():
    key = list_dedup.normalize_list_key("s", "/c", "src/main", {"depth": 3})
    list_dedup.store_list_result(key, "📄 Foo.kt")
    list_dedup.invalidate_list_cache_for_path("s", "/c", "src")
    assert list_dedup.get_cached_list(key) is None


@pytest.mark.asyncio
async def test_coalesce_parallel_list_directory_single_executor():
    calls = 0

    async def executor():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return {"entries": [{"name": "a.kt", "isDirectory": False}]}

    args = {"depth": 3, "limit": 500}
    results = await asyncio.gather(
        coalesce_or_execute("fs/list_directory", "src", "sess", executor, args=args),
        coalesce_or_execute("fs/list_directory", "src", "sess", executor, args=args),
        coalesce_or_execute("fs/list_directory", "src", "sess", executor, args=args),
    )
    assert calls == 1
    assert all(isinstance(r, dict) for r in results)


def test_summarize_listing_for_dedup():
    content = "📄 a.kt\n📁 sub\n📄 b.kt"
    count, names = list_dedup.summarize_listing_for_dedup(content)
    assert count == 3
    assert names[0] == "a.kt"


def test_dedup_list_tool_results_adjacent_round():
    from app.services.llm.context_compressor import _dedup_list_tool_results
    from app.services.llm.client import LLMMessage

    store: dict = {}
    msgs = [
        LLMMessage(
            role="assistant",
            tool_calls=[{
                "id": "tc1",
                "function": {"name": "list_files", "arguments": '{"path":"src"}'},
            }],
        ),
        LLMMessage(role="tool", tool_call_id="tc1", content="📄 a.kt\n📄 b.kt"),
        LLMMessage(
            role="assistant",
            tool_calls=[{
                "id": "tc2",
                "function": {"name": "list_files", "arguments": '{"path":"src"}'},
            }],
        ),
        LLMMessage(role="tool", tool_call_id="tc2", content="📄 a.kt\n📄 b.kt"),
    ]
    _dedup_list_tool_results(msgs, round_i=0, list_store=store)
    out = _dedup_list_tool_results(msgs, round_i=1, list_store=store)
    assert "[DUPLICATE-LIST]" in out[3].content
    assert "Round 0" in out[3].content
