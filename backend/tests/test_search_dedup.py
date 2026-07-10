"""search_dedup 单元测试。"""
import os
import sys

sys.path.insert(0, "/Users/shubinzhang/Documents/agent/Clawith/backend")

from app.plugins.clawith_acp import search_dedup


def test_search_cache_disabled_by_default(monkeypatch):
    monkeypatch.delenv("SEARCH_CACHE_TTL", raising=False)
    import importlib
    importlib.reload(search_dedup)
    assert not search_dedup.search_cache_enabled()
    assert search_dedup.get_cached_search("k") is None


def test_search_cache_store_and_invalidate(monkeypatch):
    monkeypatch.setenv("SEARCH_CACHE_TTL", "60")
    import importlib
    importlib.reload(search_dedup)
    search_dedup.store_search_result("sess1:fs/search_text:q", "out")
    assert search_dedup.get_cached_search("sess1:fs/search_text:q") == "out"
    search_dedup.invalidate_search_cache_for_session("sess1")
    assert search_dedup.get_cached_search("sess1:fs/search_text:q") is None
