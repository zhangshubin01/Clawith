"""PostgreSQL session-level advisory lock for one Agent Runtime thread."""

from collections.abc import Awaitable, Callable
import hashlib
from typing import TypeVar
import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


T = TypeVar("T")

_ACQUIRE_SQL = sa.text("SELECT pg_try_advisory_lock(:lock_key)")
_RELEASE_SQL = sa.text("SELECT pg_advisory_unlock(:lock_key)")


class ThreadLockNotAcquired(RuntimeError):
    """Another worker currently owns the Run thread lock."""

    def __init__(self, run_id: uuid.UUID, lock_key: int) -> None:
        super().__init__(f"Agent Run {run_id} thread lock is already held")
        self.run_id = run_id
        self.lock_key = lock_key


class ThreadLockReleaseError(RuntimeError):
    """The dedicated connection did not own the lock at release time."""

    def __init__(self, run_id: uuid.UUID, lock_key: int) -> None:
        super().__init__(f"Agent Run {run_id} thread lock could not be released")
        self.run_id = run_id
        self.lock_key = lock_key


def thread_lock_key(run_id: uuid.UUID) -> int:
    """Derive one stable signed PostgreSQL bigint key from a Run UUID."""
    digest = hashlib.blake2b(
        run_id.bytes,
        digest_size=8,
        person=b"clawith-run-v1",
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


async def run_with_thread_lock(
    engine: AsyncEngine,
    run_id: uuid.UUID,
    callback: Callable[[AsyncConnection], Awaitable[T]],
) -> T:
    """Run checkpoint/invoke/reconcile work on one locked connection.

    The advisory lock is session-scoped, so the same dedicated connection is
    passed to the callback and retained until the unlock query completes.
    Failure to acquire never invokes the callback.
    """
    lock_key = thread_lock_key(run_id)
    async with engine.connect() as connection:
        acquired_result = await connection.execute(
            _ACQUIRE_SQL,
            {"lock_key": lock_key},
        )
        if not bool(acquired_result.scalar_one()):
            raise ThreadLockNotAcquired(run_id, lock_key)

        try:
            return await callback(connection)
        finally:
            released_result = await connection.execute(
                _RELEASE_SQL,
                {"lock_key": lock_key},
            )
            if not bool(released_result.scalar_one()):
                raise ThreadLockReleaseError(run_id, lock_key)
