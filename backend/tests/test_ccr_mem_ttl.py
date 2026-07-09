import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/Users/shubinzhang/Documents/agent/Clawith/backend")

import pytest

from app.services.llm import ccr_store


@pytest.mark.asyncio
async def test_mem_hit_expired_returns_miss(monkeypatch):
    ccr_store._MEM.clear()
    key = "sess1:abc"
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    ccr_store._mem_put(key, "old content", past)

    async def _no_pg(*a, **k):
        return None

    monkeypatch.setattr(ccr_store, "async_session", lambda: (_ for _ in ()).throw(RuntimeError("no pg")))

    out = ccr_store._mem_get_valid(key)
    assert out is None


@pytest.mark.asyncio
async def test_store_reject_empty_session():
    before = ccr_store.CCR_METRICS.get("store_reject_empty_session", 0)
    h = await ccr_store.store_entry("", None, "content", tool_name="t")
    assert h is None
    assert ccr_store.CCR_METRICS.get("store_reject_empty_session", 0) >= before + 1


def test_metrics_snapshot_has_stats_scope():
    snap = ccr_store.get_ccr_metrics_snapshot()
    assert snap["compression"]["stats"]["scope"] == "process"
    assert "mem_cache_expiring_count" in snap["ccr"]
