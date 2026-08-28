"""Database connection and session management."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from contextvars import ContextVar

from loguru import logger
from sqlalchemy import event, text
from sqlalchemy.exc import DisconnectionError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_recycle=settings.DB_POOL_RECYCLE_SECONDS,
)


def _discard_dirty_connection(dbapi_conn, connection_record, connection_proxy) -> None:
    """Discard pooled connections the server still considers inside a transaction.

    A cancel that lands in the asyncpg lazy-start window (SQLAlchemy 2.0 sends
    BEGIN only on first statement execution) can leave a connection in this
    state: SQLAlchemy believes it is clean (``_started=False``), so the checkin
    rollback is a no-op, while the server is still in 'T'. Every later checkout
    of that connection then explodes with ``cannot use Connection.transaction()
    in a manually started transaction`` — and ``_handle_exception`` never
    invalidates, so the poison persists for hours (2026-08-28 incident,
    ADR-0006). Raising DisconnectionError here makes the pool discard the
    connection and hand out a healthy one; the check is a local client-side
    state read, no network round-trip.
    """
    driver_conn = getattr(dbapi_conn, "driver_connection", None)
    if driver_conn is not None and driver_conn.is_in_transaction():
        raise DisconnectionError("dirty asyncpg connection: server-side transaction still open")


event.listen(engine.sync_engine, "checkout", _discard_dirty_connection)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def warn_on_connection_budget() -> None:
    """Compare configured connection demand against PostgreSQL ``max_connections``.

    The backend owns three budget lines: the SQLAlchemy pool
    (``DB_POOL_SIZE`` + ``DB_MAX_OVERFLOW``), the shared checkpoint pool
    (``CHECKPOINT_POOL_MAX_SIZE``) and the connections reserved for consumers
    outside the backend (``DB_RESERVED_CONNECTIONS`` — per-session MCP runtimes,
    admin tooling). Warn at startup when the configured demand would exhaust the
    database, so budget mismatches surface at deploy time instead of as
    ``runtime_intake_failed`` chat errors.
    """
    try:
        async with engine.connect() as conn:
            raw_max = (await conn.execute(text("SHOW max_connections"))).scalar_one()
        max_connections = int(raw_max)
    except Exception as exc:  # pragma: no cover - database may be down at boot
        logger.warning(f"[startup] connection budget check skipped (cannot read max_connections): {exc}")
        return

    demand = (
        settings.DB_POOL_SIZE
        + settings.DB_MAX_OVERFLOW
        + settings.CHECKPOINT_POOL_MAX_SIZE
        + settings.DB_RESERVED_CONNECTIONS
    )
    context = (
        f"demand={demand} (sqlalchemy={settings.DB_POOL_SIZE}+{settings.DB_MAX_OVERFLOW}, "
        f"checkpoint={settings.CHECKPOINT_POOL_MAX_SIZE}, reserved={settings.DB_RESERVED_CONNECTIONS}) "
        f"vs max_connections={max_connections}"
    )
    if demand > max_connections:
        logger.error(
            f"[startup] connection budget EXCEEDED: {context}. "
            "Raise max_connections or shrink DB_POOL_SIZE/DB_MAX_OVERFLOW/"
            "CHECKPOINT_POOL_MAX_SIZE/DB_RESERVED_CONNECTIONS."
        )
    elif demand > int(max_connections * 0.8):
        logger.warning(f"[startup] connection budget is tight: {context}.")
    else:
        logger.info(f"[startup] connection budget OK: {context}")


class Base(DeclarativeBase):
    """SQLAlchemy declarative base."""

    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for getting async database sessions."""
    async with async_session() as session:
        token = _session_ctx.set(session)
        try:
            yield session
            await asyncio.shield(session.commit())
        except Exception:
            await asyncio.shield(session.rollback())
            raise
        finally:
            _session_ctx.reset(token)


_session_ctx: ContextVar[AsyncSession | None] = ContextVar("db_session_ctx", default=None)


@asynccontextmanager
async def bind_session_context(session: AsyncSession) -> AsyncGenerator[AsyncSession, None]:
    """Temporarily expose an existing session to DAO helpers without owning its transaction."""
    token = _session_ctx.set(session)
    try:
        yield session
    finally:
        _session_ctx.reset(token)


@asynccontextmanager
async def transaction(session: AsyncSession | None = None) -> AsyncGenerator[AsyncSession, None]:
    """Provide a transactional boundary using contextvars."""
    if session is not None:
        token = _session_ctx.set(session)
        try:
            yield session
            if hasattr(session, "commit"):
                await asyncio.shield(session.commit())
        except Exception:
            if hasattr(session, "rollback"):
                await asyncio.shield(session.rollback())
            raise
        finally:
            _session_ctx.reset(token)
        return

    existing_session = _session_ctx.get()
    if existing_session is not None:
        yield existing_session
        return

    async with async_session() as session:
        token = _session_ctx.set(session)
        try:
            yield session
            await asyncio.shield(session.commit())
        except Exception:
            await asyncio.shield(session.rollback())
            raise
        finally:
            _session_ctx.reset(token)
