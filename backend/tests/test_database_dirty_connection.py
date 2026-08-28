"""Dirty-connection checkout probe tests (ADR-0006).

A cancel that lands in the asyncpg lazy-start window can leave a pooled
connection with the SERVER inside a transaction while SQLAlchemy believes it
is clean; every later checkout of that connection explodes with
``cannot use Connection.transaction() in a manually started transaction``
and the connection is never invalidated, so the poison persists for hours.

The checkout probe (`_discard_dirty_connection`) raises DisconnectionError for
such connections so the pool discards them and hands out a healthy one.

Unit tests lock the probe decision; the integration test locks the full seam
against a real PostgreSQL (skipped unless TEST_DATABASE_URL is set).
"""

import os
from types import SimpleNamespace

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DisconnectionError
from sqlalchemy.ext.asyncio import create_async_engine

from app.database import _discard_dirty_connection


class FakeDriverConnection:
    def __init__(self, in_transaction: bool) -> None:
        self._in_transaction = in_transaction

    def is_in_transaction(self) -> bool:
        return self._in_transaction


def test_probe_raises_disconnection_error_when_server_side_in_transaction() -> None:
    dbapi_conn = SimpleNamespace(driver_connection=FakeDriverConnection(True))
    with pytest.raises(DisconnectionError):
        _discard_dirty_connection(dbapi_conn, None, None)


def test_probe_passes_clean_connection() -> None:
    dbapi_conn = SimpleNamespace(driver_connection=FakeDriverConnection(False))
    _discard_dirty_connection(dbapi_conn, None, None)  # must not raise


def test_probe_ignores_dialects_without_driver_connection() -> None:
    _discard_dirty_connection(SimpleNamespace(), None, None)  # must not raise


@pytest.mark.asyncio
async def test_probe_discards_dirty_connection_on_checkout_integration() -> None:
    """Full seam against a real PostgreSQL: construct the dirty state
    (server-side BEGIN SQLAlchemy is unaware of), check the connection back
    in, and assert the next checkout is handed a healthy connection."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")

    eng = create_async_engine(url, pool_size=1, max_overflow=0)
    event.listen(eng.sync_engine, "checkout", _discard_dirty_connection)

    async with eng.connect() as conn:
        await conn.begin()
        await conn.execute(text("SELECT 1"))

    # Construct the dirty state: bare BEGIN on the raw driver connection,
    # SQLAlchemy still thinks the connection is clean (_started=False).
    async with eng.connect() as conn:
        raw = await conn.get_raw_connection()
        assert raw.driver_connection.is_in_transaction() is False
        await raw.driver_connection.execute("BEGIN")
        assert raw.driver_connection.is_in_transaction() is True

    # The probe must discard the dirty connection at checkout and the caller
    # gets a healthy one: begin + execute succeed instead of exploding with
    # "cannot use Connection.transaction() in a manually started transaction".
    async with eng.connect() as conn:
        await conn.begin()
        await conn.execute(text("SELECT 1"))

    await eng.dispose()
