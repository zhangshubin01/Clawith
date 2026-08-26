"""LangGraph checkpoint wiring without product-state dual writes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit
import asyncio
import uuid

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.base import SerializerProtocol
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from app.config import Settings, get_settings


class CheckpointerConfigurationError(ValueError):
    """Checkpoint persistence cannot be configured safely."""


_CHECKPOINT_SCHEMA = "langgraph_checkpoint"
_ALLOWED_RUNTIME_MSGPACK_TYPES = (
    ("app.services.agent_runtime.state", "RunRegistrySnapshot"),
    ("app.services.agent_runtime.state", "RunInputSnapshots"),
)


def runtime_thread_config(
    thread_id: str | uuid.UUID,
    *,
    checkpoint_id: str | None = None,
) -> dict[str, dict[str, str]]:
    """Build an exact LangGraph Thread/checkpoint identity.

    A Thread is not necessarily a Run. Direct Chat can place multiple logical
    Runs on one Thread, while Group and background Runs currently keep their
    independent ``run_id`` Thread identity.
    """
    resolved_thread_id = str(thread_id).strip()
    if not resolved_thread_id:
        raise CheckpointerConfigurationError("Runtime thread_id must not be blank")
    configurable = {"thread_id": resolved_thread_id}
    if checkpoint_id is not None:
        resolved_checkpoint_id = checkpoint_id.strip()
        if not resolved_checkpoint_id:
            raise CheckpointerConfigurationError("checkpoint_id must not be blank")
        configurable["checkpoint_id"] = resolved_checkpoint_id
    return {"configurable": configurable}


def runtime_command_config(
    thread_id: str | uuid.UUID,
    *,
    run_id: uuid.UUID,
    command_id: uuid.UUID,
    checkpoint_id: str | None = None,
) -> dict[str, Any]:
    """Bind one Graph invocation to Clawith Run/Command metadata."""
    config: dict[str, Any] = runtime_thread_config(
        thread_id,
        checkpoint_id=checkpoint_id,
    )
    config["metadata"] = {
        "clawith_run_id": str(run_id),
        "clawith_command_id": str(command_id),
    }
    return config


def _to_psycopg_url(database_url: str) -> str:
    """Normalize a PostgreSQL URI and force the checkpoint-only search path."""
    value = database_url.strip()
    if not value:
        raise CheckpointerConfigurationError("Checkpoint database URL must not be blank")

    scheme, separator, remainder = value.partition("://")
    if not separator:
        raise CheckpointerConfigurationError("Checkpoint database URL must be a PostgreSQL URL")
    if scheme == "postgres":
        scheme = "postgresql"
    elif scheme.startswith("postgresql+"):
        scheme = "postgresql"
    elif scheme != "postgresql":
        raise CheckpointerConfigurationError("Checkpoint database URL must use PostgreSQL")
    normalized = f"{scheme}://{remainder}"
    parts = urlsplit(normalized)
    existing_options: list[str] = []
    other_query_parts: list[str] = []
    explicit_sslmode: str | None = None
    asyncpg_sslmode: str | None = None
    for query_part in parts.query.split("&"):
        if not query_part:
            continue
        encoded_key, separator, encoded_value = query_part.partition("=")
        key = unquote(encoded_key)
        if key == "options":
            existing_options.append(unquote(encoded_value) if separator else "")
        elif key == "ssl":
            if not separator or not encoded_value:
                raise CheckpointerConfigurationError("Checkpoint database ssl query parameter must not be blank")
            value = unquote(encoded_value).strip().lower()
            asyncpg_sslmode = {
                "true": "require",
                "1": "require",
                "false": "disable",
                "0": "disable",
            }.get(value, value)
        elif key == "sslmode":
            if not separator or not encoded_value:
                raise CheckpointerConfigurationError("Checkpoint database sslmode query parameter must not be blank")
            explicit_sslmode = unquote(encoded_value).strip().lower()
            other_query_parts.append(query_part)
        else:
            # Preserve unrelated libpq parameters byte-for-byte. In PostgreSQL
            # connection URIs, unlike HTML form encoding, ``+`` is literal.
            other_query_parts.append(query_part)

    if asyncpg_sslmode is not None:
        if explicit_sslmode is not None and explicit_sslmode != asyncpg_sslmode:
            raise CheckpointerConfigurationError("Checkpoint database URL contains conflicting ssl and sslmode values")
        if explicit_sslmode is None:
            other_query_parts.append(f"sslmode={quote(asyncpg_sslmode, safe='')}")

    search_path_option = f"-c search_path={_CHECKPOINT_SCHEMA},public"
    options = " ".join([option for option in existing_options if option] + [search_path_option])
    encoded_options = quote(options, safe="")
    query = "&".join([*other_query_parts, f"options={encoded_options}"])
    return urlunsplit(parts._replace(query=query))


def checkpoint_database_url(settings: Settings | None = None) -> str:
    """Resolve the dedicated checkpoint DSN, falling back to the primary database."""
    runtime_settings = settings or get_settings()
    configured = runtime_settings.LANGGRAPH_CHECKPOINT_DATABASE_URL or runtime_settings.DATABASE_URL
    return _to_psycopg_url(configured)


def checkpoint_serializer(
    settings: Settings | None = None,
) -> SerializerProtocol:
    """Build an allowlisted serializer, optionally wrapped in AES encryption."""
    runtime_settings = settings or get_settings()
    serde = JsonPlusSerializer(
        allowed_msgpack_modules=_ALLOWED_RUNTIME_MSGPACK_TYPES,
    )
    key = runtime_settings.LANGGRAPH_AES_KEY
    if key is None:
        return serde

    key_bytes = key.encode("utf-8")
    if len(key_bytes) not in (16, 24, 32):
        raise CheckpointerConfigurationError("LANGGRAPH_AES_KEY must encode to 16, 24, or 32 bytes")
    try:
        return EncryptedSerializer.from_pycryptodome_aes(
            serde=serde,
            key=key_bytes,
        )
    except ImportError as exc:
        raise CheckpointerConfigurationError("Checkpoint AES encryption requires pycryptodome") from exc


@asynccontextmanager
async def create_checkpointer(
    settings: Settings | None = None,
) -> AsyncIterator[BaseCheckpointSaver]:
    """Yield a saver bound to the process-level shared checkpoint pool.

    The pool outlives every saver: ``AsyncPostgresSaver`` acquires and releases
    connections per operation and never closes an externally supplied pool.
    Closing the pool once at application shutdown is handled by
    ``close_checkpointer_pool``.

    Checkpoint volume is controlled at the engine level with
    ``durability="exit"`` (one full checkpoint per Run boundary) — never by
    wrapping the saver. LangGraph's checkpoints are incremental: channel values
    are reconstructed by walking the parent chain and replaying writes, so any
    wrapper that skips checkpoints or swallows writes severs that chain and
    corrupts resumed state (see docs/technical-plans/
    20260826-checkpoint-persistence-root-fix.md).
    """
    runtime_settings = settings or get_settings()
    pool = await get_shared_checkpoint_pool(runtime_settings)
    saver: BaseCheckpointSaver = AsyncPostgresSaver(
        conn=pool,
        serde=checkpoint_serializer(runtime_settings),
    )
    yield saver


# ---------------------------------------------------------------------------
# Process-level shared checkpoint pool
#
# LangGraph's ``AsyncPostgresSaver.from_conn_string`` opens a brand-new psycopg
# connection per context manager. Chat intake enters one checkpointer per
# message, so that pattern competes with the SQLAlchemy pool and per-session
# MCP runtimes for PostgreSQL ``max_connections`` slots. A single shared pool
# pins the checkpoint demand to a fixed, configurable budget instead.
# ---------------------------------------------------------------------------

_checkpoint_pool: AsyncConnectionPool[AsyncConnection[DictRow]] | None = None
_checkpoint_pool_dsn: str | None = None
_checkpoint_pool_lock = asyncio.Lock()


async def get_shared_checkpoint_pool(
    settings: Settings | None = None,
) -> AsyncConnectionPool[AsyncConnection[DictRow]]:
    """Return the process-level checkpoint pool, opening it lazily once."""
    global _checkpoint_pool, _checkpoint_pool_dsn
    resolved_dsn = checkpoint_database_url(settings)
    if _checkpoint_pool is not None:
        if _checkpoint_pool_dsn != resolved_dsn:
            raise CheckpointerConfigurationError("Checkpoint pool was already opened for a different database URL")
        return _checkpoint_pool
    async with _checkpoint_pool_lock:
        if _checkpoint_pool is None:
            runtime_settings = settings or get_settings()
            pool: AsyncConnectionPool[AsyncConnection[DictRow]] = AsyncConnectionPool(
                resolved_dsn,
                min_size=runtime_settings.CHECKPOINT_POOL_MIN_SIZE,
                max_size=runtime_settings.CHECKPOINT_POOL_MAX_SIZE,
                timeout=runtime_settings.CHECKPOINT_POOL_TIMEOUT_SECONDS,
                open=False,
                # Mirror AsyncPostgresSaver.from_conn_string's connection setup.
                kwargs={
                    "autocommit": True,
                    "prepare_threshold": 0,
                    "row_factory": dict_row,
                },
            )
            await pool.open()
            _checkpoint_pool = pool
            _checkpoint_pool_dsn = resolved_dsn
        return _checkpoint_pool


async def close_checkpointer_pool() -> None:
    """Close the shared checkpoint pool; idempotent and safe after shutdown."""
    global _checkpoint_pool, _checkpoint_pool_dsn
    pool = _checkpoint_pool
    _checkpoint_pool = None
    _checkpoint_pool_dsn = None
    if pool is not None:
        await pool.close()
