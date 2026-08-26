"""Daily runtime checkpoint retention pass (L3 thread recycling).

The prune logic itself lives in ``app.scripts.prune_runtime_checkpoints``
(CLI-invocable, dry-run default). This module wraps it into a periodic
background pass so old checkpoint/writes/blobs rows are recycled without
operator action. Safety rails are enforced by the script: busy threads are
skipped, threads whose newest checkpoint is younger than ``min_age_days``
are skipped, and blob GC only removes (channel, version) pairs no surviving
checkpoint references.

See docs/technical-plans/20260826-checkpoint-persistence-root-fix.md §L3.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

logger = logging.getLogger(__name__)

PruneFn = Callable[..., object]


async def retention_pass(
    *,
    keep: int,
    min_age_days: int,
    prune_fn: PruneFn | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> None:
    """Run one prune pass with the configured retention window."""
    if prune_fn is None:
        from app.scripts.prune_runtime_checkpoints import run as prune_fn  # type: ignore[assignment]

    now = (now_fn or (lambda: datetime.now(timezone.utc)))()
    min_age_dt = now - timedelta(days=max(0, min_age_days))
    await prune_fn(dry_run=False, keep=keep, min_age_dt=min_age_dt, limit=None)  # type: ignore[misc]


async def checkpoint_retention_loop(
    stop: asyncio.Event,
    *,
    interval_seconds: float,
    first_delay_seconds: float,
    keep: int,
    min_age_days: int,
    prune_fn: PruneFn | None = None,
) -> None:
    """Periodic retention passes until ``stop`` is set.

    The first pass runs after ``first_delay_seconds`` so a fresh container
    start is not racing its own bootstrap. Each pass failure is logged and
    the loop keeps its cadence; a bad prune never kills the task.
    """
    try:
        await asyncio.wait_for(stop.wait(), timeout=max(0.0, first_delay_seconds))
        return
    except asyncio.TimeoutError:
        pass

    while not stop.is_set():
        try:
            await retention_pass(
                keep=keep,
                min_age_days=min_age_days,
                prune_fn=prune_fn,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("runtime checkpoint retention pass failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            raise


async def start_checkpoint_retention() -> None:
    """Start the periodic retention loop. Call from FastAPI startup.

    Shutdown cancels the task (asyncio.CancelledError propagates out of the
    loop's ``wait_for`` calls), matching the other background daemons.
    """
    from app.config import get_settings

    settings = get_settings()
    if settings.AGENT_RUNTIME_CHECKPOINT_RETENTION_INTERVAL_SECONDS <= 0:
        logger.info("runtime checkpoint retention disabled (interval=0)")
        return
    logger.info(
        "runtime checkpoint retention started "
        "(interval=%ss, keep=%s, min-age=%sd)",
        settings.AGENT_RUNTIME_CHECKPOINT_RETENTION_INTERVAL_SECONDS,
        settings.AGENT_RUNTIME_CHECKPOINT_RETENTION_KEEP_PER_THREAD,
        settings.AGENT_RUNTIME_CHECKPOINT_RETENTION_MIN_AGE_DAYS,
    )
    await checkpoint_retention_loop(
        asyncio.Event(),
        interval_seconds=settings.AGENT_RUNTIME_CHECKPOINT_RETENTION_INTERVAL_SECONDS,
        first_delay_seconds=settings.AGENT_RUNTIME_CHECKPOINT_RETENTION_FIRST_DELAY_SECONDS,
        keep=settings.AGENT_RUNTIME_CHECKPOINT_RETENTION_KEEP_PER_THREAD,
        min_age_days=settings.AGENT_RUNTIME_CHECKPOINT_RETENTION_MIN_AGE_DAYS,
    )
