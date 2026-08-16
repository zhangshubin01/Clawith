"""P2: TTL cache for the Durable Runtime tool workset."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.services import agent_tools_cache


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    agent_tools_cache.clear_tool_resolution_cache()
    yield
    agent_tools_cache.clear_tool_resolution_cache()


@pytest.mark.asyncio
async def test_cached_resolution_reuses_value_within_ttl(monkeypatch) -> None:
    agent_id = uuid.uuid4()
    calls = 0

    async def fake_resolver(aid):
        nonlocal calls
        calls += 1
        assert aid == agent_id
        return [{"type": "function", "function": {"name": "read_file"}}]

    monkeypatch.setattr(agent_tools_cache, "_resolver", lambda: fake_resolver)

    first = await agent_tools_cache.cached_runtime_agent_tools(agent_id)
    second = await agent_tools_cache.cached_runtime_agent_tools(agent_id)
    assert first == second
    assert calls == 1


@pytest.mark.asyncio
async def test_expired_entry_is_resolved_again(monkeypatch) -> None:
    agent_id = uuid.uuid4()
    calls = 0

    async def fake_resolver(aid):
        nonlocal calls
        calls += 1
        return [calls]

    monkeypatch.setattr(agent_tools_cache, "_resolver", lambda: fake_resolver)
    monkeypatch.setattr(agent_tools_cache, "TOOL_RESOLUTION_TTL_SECONDS", 0.0)

    first = await agent_tools_cache.cached_runtime_agent_tools(agent_id)
    second = await agent_tools_cache.cached_runtime_agent_tools(agent_id)
    assert first == [1]
    assert second == [2]
    assert calls == 2


@pytest.mark.asyncio
async def test_agent_invalidation_drops_cached_value(monkeypatch) -> None:
    agent_id = uuid.uuid4()
    calls = 0

    async def fake_resolver(aid):
        nonlocal calls
        calls += 1
        return [calls]

    monkeypatch.setattr(agent_tools_cache, "_resolver", lambda: fake_resolver)

    assert await agent_tools_cache.cached_runtime_agent_tools(agent_id) == [1]
    agent_tools_cache.invalidate_agent_tool_resolution(agent_id)
    assert await agent_tools_cache.cached_runtime_agent_tools(agent_id) == [2]
    assert calls == 2


@pytest.mark.asyncio
async def test_global_invalidation_drops_everything(monkeypatch) -> None:
    first_agent = uuid.uuid4()
    second_agent = uuid.uuid4()
    calls = 0

    async def fake_resolver(aid):
        nonlocal calls
        calls += 1
        return [aid, calls]

    monkeypatch.setattr(agent_tools_cache, "_resolver", lambda: fake_resolver)

    await agent_tools_cache.cached_runtime_agent_tools(first_agent)
    await agent_tools_cache.cached_runtime_agent_tools(second_agent)
    agent_tools_cache.invalidate_all_tool_resolutions()
    await agent_tools_cache.cached_runtime_agent_tools(first_agent)
    await agent_tools_cache.cached_runtime_agent_tools(second_agent)
    assert calls == 4


@pytest.mark.asyncio
async def test_concurrent_misses_share_one_resolution(monkeypatch) -> None:
    agent_id = uuid.uuid4()
    calls = 0
    started = asyncio.Event()

    async def fake_resolver(aid):
        nonlocal calls
        calls += 1
        started.set()
        await asyncio.sleep(0.05)
        return [aid]

    monkeypatch.setattr(agent_tools_cache, "_resolver", lambda: fake_resolver)

    results = await asyncio.gather(
        agent_tools_cache.cached_runtime_agent_tools(agent_id),
        agent_tools_cache.cached_runtime_agent_tools(agent_id),
        agent_tools_cache.cached_runtime_agent_tools(agent_id),
    )
    assert all(r == results[0] for r in results)
    assert calls == 1
    assert started.is_set()


@pytest.mark.asyncio
async def test_failed_resolution_is_not_cached(monkeypatch) -> None:
    agent_id = uuid.uuid4()
    calls = 0

    async def fake_resolver(aid):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        return [calls]

    monkeypatch.setattr(agent_tools_cache, "_resolver", lambda: fake_resolver)

    with pytest.raises(RuntimeError, match="boom"):
        await agent_tools_cache.cached_runtime_agent_tools(agent_id)
    assert await agent_tools_cache.cached_runtime_agent_tools(agent_id) == [2]
    assert calls == 2
