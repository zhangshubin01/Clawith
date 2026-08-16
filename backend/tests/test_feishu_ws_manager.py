"""Tests for FeishuWSManager initial-connect retry behavior.

Guards the fix that retries the FIRST Feishu WebSocket handshake with
exponential backoff. Previously, when the initial _connect() failed
(e.g. transient network timeout), the SDK's own _reconnect() never ran —
it is only reachable from _receive_message_loop, which is only started
after a successful first connect — leaving the channel dead until the
next backend restart.
"""
import asyncio
import uuid

import pytest

from app.services import feishu_ws
from app.services.feishu_ws import FeishuWSManager


class _FakeWSClient:
    """Stand-in for lark_oapi.ws.Client.

    Fails the first ``fail_first`` _connect() attempts, then reports a
    successful connection.
    """

    def __init__(self, fail_first: int = 0):
        self.fail_first = fail_first
        self.connect_calls = 0
        self._conn = None
        self._conn_id = ""

    async def _connect(self):
        self.connect_calls += 1
        if self.connect_calls <= self.fail_first:
            raise RuntimeError(f"simulated handshake failure #{self.connect_calls}")
        self._conn = object()  # non-None marks the connection as up
        self._conn_id = f"conn-{self.connect_calls}"

    async def _ping_loop(self):
        return

    async def _disconnect(self):
        self._conn = None
        self._conn_id = ""


@pytest.fixture
def fake_clients(monkeypatch):
    """Replace ws.Client with a fake factory; exposes created instances."""
    instances: list[_FakeWSClient] = []

    def _factory(*args, **kwargs):
        client = _FakeWSClient()
        instances.append(client)
        return client

    monkeypatch.setattr(feishu_ws.ws, "Client", _factory)
    return instances


@pytest.fixture
def skip_backoff_sleep(monkeypatch):
    """Skip only backoff sleeps (delay 5-25s); other sleeps stay real."""
    real_sleep = asyncio.sleep

    async def _fake_sleep(delay, *args, **kwargs):
        if 5 <= delay < 25:
            await real_sleep(0)  # yield control so the retry loop can't starve the loop
            return
        return await real_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(feishu_ws.asyncio, "sleep", _fake_sleep)
    return _fake_sleep


async def _wait_for(predicate, timeout: float = 5.0):
    """Poll until predicate() is truthy, then assert it happened in time."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    pytest.fail(f"condition not met within {timeout}s")


async def _stop_manager(manager: FeishuWSManager, agent_id: uuid.UUID):
    task = manager._tasks.get(agent_id)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def _make_client(fake_clients, n):
    client = _FakeWSClient(fail_first=n)
    fake_clients.append(client)
    return client


async def test_initial_connect_retries_until_success(fake_clients, skip_backoff_sleep):
    manager = FeishuWSManager()
    agent_id = uuid.uuid4()
    manager._create_event_handler = lambda aid: object()  # type: ignore[method-assign]

    # First handshake fails; manager must retry instead of giving up.
    feishu_ws.ws.Client = lambda *args, **kwargs: _make_client(fake_clients, 1)

    await manager.start_client(agent_id, "app-id", "app-secret")
    client = fake_clients[0]

    await _wait_for(lambda: client._conn is not None)
    assert client.connect_calls == 2, "manager should retry once after initial failure"
    assert client._conn_id == "conn-2"

    await _stop_manager(manager, agent_id)


async def test_initial_connect_backoff_delay_capped(fake_clients, monkeypatch):
    manager = FeishuWSManager()
    agent_id = uuid.uuid4()
    manager._create_event_handler = lambda aid: object()  # type: ignore[method-assign]

    # Always-failing client; record backoff delays to assert the 300s cap.
    feishu_ws.ws.Client = lambda *args, **kwargs: _make_client(fake_clients, 10_000)

    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def _recording_sleep(delay, *args, **kwargs):
        if delay >= 5:
            delays.append(delay)
            await real_sleep(0)  # yield control so the retry loop can't starve the loop
            return
        return await real_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(feishu_ws.asyncio, "sleep", _recording_sleep)

    await manager.start_client(agent_id, "app-id", "app-secret")
    client = fake_clients[0]
    await _wait_for(lambda: client.connect_calls >= 7, timeout=10.0)

    assert delays[0] == 10
    assert delays[1] == 20
    assert len(delays) >= 6
    assert all(d <= 300 for d in delays)
    assert delays[-1] == 300

    await _stop_manager(manager, agent_id)
