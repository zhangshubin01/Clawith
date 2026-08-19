"""Install or upgrade tables owned by the pinned LangGraph checkpointer."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg import AsyncConnection

from app.config import Settings
from app.services.agent_runtime.checkpointer import (
    checkpoint_database_url,
    close_checkpointer_pool,
    create_checkpointer,
)


_SETUP_LOCK_NAME = "clawith:langgraph_checkpoint:setup"

# LangGraph's ``AsyncPostgresSaver.setup()`` creates these single-column
# thread_id indexes with CREATE INDEX CONCURRENTLY IF NOT EXISTS on every
# bootstrap, but each table's primary key already starts with (thread_id, ...),
# so they are pure write amplification on the hottest tables. Alembic drops
# them too (f066); this cleanup keeps them gone after setup() recreates them.
_DROP_REDUNDANT_THREAD_INDEXES: tuple[str, ...] = (
    "DROP INDEX IF EXISTS langgraph_checkpoint.checkpoints_thread_id_idx",
    "DROP INDEX IF EXISTS langgraph_checkpoint.checkpoint_blobs_thread_id_idx",
    "DROP INDEX IF EXISTS langgraph_checkpoint.checkpoint_writes_thread_id_idx",
)

# Run-boundary lookups (langgraph_driver.read_latest / read_for_command,
# run_state_reader._read_unsettled) find a run's checkpoint via
# ``aget_state_history(filter={"clawith_run_id": ...})``, which emits
# ``metadata @> %s`` containment predicates. Without an index the planner
# bitmap-scans every checkpoint of the thread (~1800 rows on hot threads) and
# the query averaged ~0.9s (pg_stat_statements). jsonb_path_ops serves exactly
# the @> predicate these lookups need, at a fraction of a full GIN index size.
_METADATA_GIN_INDEX_DDL: str = (
    "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
    "checkpoints_metadata_gin_idx "
    "ON langgraph_checkpoint.checkpoints USING gin (metadata jsonb_path_ops)"
)


@asynccontextmanager
async def checkpoint_setup_lock(
    settings: Settings | None = None,
) -> AsyncIterator[None]:
    """Serialize saver DDL across concurrently starting bootstrap processes.

    ``AsyncPostgresSaver.setup()`` maintains its own migration ledger, but the
    initial ledger read and write are not one atomic operation. A PostgreSQL
    session advisory lock keeps the explicit deployment step idempotent when
    more than one bootstrap process starts at the same time. PostgreSQL releases
    the lock automatically if the setup process exits unexpectedly.
    """

    connection = await AsyncConnection.connect(
        checkpoint_database_url(settings),
        autocommit=True,
    )
    try:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
                (_SETUP_LOCK_NAME,),
            )
        try:
            yield
        finally:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                    (_SETUP_LOCK_NAME,),
                )
    finally:
        await connection.close()


async def _execute_checkpoint_ddl(
    statements: tuple[str, ...] | list[str],
    settings: Settings | None = None,
) -> None:
    """Run idempotent checkpoint-schema DDL over one autocommit connection."""
    connection = await AsyncConnection.connect(
        checkpoint_database_url(settings),
        autocommit=True,
    )
    try:
        async with connection.cursor() as cursor:
            for statement in statements:
                await cursor.execute(statement)
    finally:
        await connection.close()


async def drop_redundant_thread_indexes(settings: Settings | None = None) -> None:
    """Drop LangGraph thread_id indexes that are fully covered by each PK.

    Runs right after ``AsyncPostgresSaver.setup()`` (which recreates them via
    ``CREATE INDEX CONCURRENTLY IF NOT EXISTS``); idempotent via IF EXISTS.
    Alembic migration f066 applies the same drop for environments that do not
    run this bootstrap step again.
    """
    await _execute_checkpoint_ddl(_DROP_REDUNDANT_THREAD_INDEXES, settings)


async def ensure_checkpoint_metadata_index(settings: Settings | None = None) -> None:
    """Create the GIN index backing run-boundary checkpoint lookups.

    Runs right after ``AsyncPostgresSaver.setup()`` — the checkpoint tables are
    guaranteed to exist by then, which is why this is not an Alembic migration
    (migrations run before the LangGraph bootstrap on fresh environments).
    Idempotent via IF NOT EXISTS; CONCURRENTLY keeps checkpoint writes flowing
    during the first build on populated deployments.
    """
    await _execute_checkpoint_ddl([_METADATA_GIN_INDEX_DDL], settings)


async def setup_checkpoint_tables(settings: Settings | None = None) -> None:
    """Run the upstream idempotent migration ledger inside its isolated schema.

    Alembic creates ``langgraph_checkpoint`` first. This explicit bootstrap
    step then lets the pinned saver version create or upgrade only its own
    tables. FastAPI runtime startup intentionally does not run checkpoint DDL.
    """
    async with checkpoint_setup_lock(settings):
        async with create_checkpointer(settings) as checkpointer:
            await checkpointer.setup()
        await drop_redundant_thread_indexes(settings)
        await ensure_checkpoint_metadata_index(settings)


def main() -> None:
    async def _run() -> None:
        try:
            await setup_checkpoint_tables()
        finally:
            # 显式关闭共享池，避免依赖 asyncio.run 取消后台连接任务
            # （异步池 worker 在 teardown 阶段可能拖住事件循环，曾导致本脚本挂起）。
            await close_checkpointer_pool()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
