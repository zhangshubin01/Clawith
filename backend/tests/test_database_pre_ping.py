"""pool_pre_ping configuration lock (C2, P2 hardening).

C1 (server-side tcp_keepalives) reaps dead peers; pool_pre_ping is the
client-side complement — SQLAlchemy pings a pooled connection before handing
it out when it may have gone stale, so a PostgreSQL restart / network blip
discards the dead socket and reconnects instead of raising on the first query.
This locks the flag on the real engine (a one-line regression that catches
someone removing pool_pre_ping).
"""

from app.database import engine


def test_engine_pool_pre_ping_enabled() -> None:
    assert engine.sync_engine.pool._pre_ping is True
