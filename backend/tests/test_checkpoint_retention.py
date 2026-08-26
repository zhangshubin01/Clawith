"""Checkpoint retention background pass tests (no database)."""

import asyncio
from datetime import UTC, datetime

import pytest

from app.services.agent_runtime import checkpoint_retention


class _PruneStub:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, **kwargs) -> int:
        self.calls.append(kwargs)
        return 0


@pytest.mark.asyncio
async def test_retention_pass_invokes_prune_with_window_and_apply_mode():
    stub = _PruneStub()
    now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)

    await checkpoint_retention.retention_pass(
        keep=3,
        min_age_days=3,
        prune_fn=stub,
        now_fn=lambda: now,
    )

    assert stub.calls == [
        {
            "dry_run": False,
            "keep": 3,
            "min_age_dt": datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
            "limit": None,
        }
    ]


@pytest.mark.asyncio
async def test_retention_loop_runs_first_pass_after_delay_and_respects_interval():
    stop = asyncio.Event()
    timeline: list[str] = []

    async def delayed_prune(**kwargs) -> int:
        timeline.append("prune")
        return 0

    async def waiter():
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.6)
        except asyncio.TimeoutError:
            pass
        stop.set()

    task = asyncio.create_task(
        checkpoint_retention.checkpoint_retention_loop(
            stop,
            interval_seconds=0.3,
            first_delay_seconds=0.1,
            keep=3,
            min_age_days=3,
            prune_fn=delayed_prune,
        )
    )
    await waiter()
    await asyncio.wait_for(task, timeout=2)

    assert timeline.count("prune") >= 3  # 0.1s first pass + ~0.3s cadence


@pytest.mark.asyncio
async def test_retention_loop_survives_prune_failure_and_keeps_cadence():
    failures = {"count": 0}

    async def failing_prune(**kwargs) -> int:
        failures["count"] += 1
        raise RuntimeError("boom")

    stop = asyncio.Event()

    async def waiter():
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.8)
        except asyncio.TimeoutError:
            pass
        stop.set()

    task = asyncio.create_task(
        checkpoint_retention.checkpoint_retention_loop(
            stop,
            interval_seconds=0.2,
            first_delay_seconds=0.05,
            keep=3,
            min_age_days=3,
            prune_fn=failing_prune,
        )
    )
    await waiter()
    await asyncio.wait_for(task, timeout=2)

    assert failures["count"] >= 2  # kept looping despite failures
