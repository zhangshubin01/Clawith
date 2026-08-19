"""Activity DAO conversation-summary tests.

The agent-session branch must stay index-scoped: the previous implementation
joined two *unfiltered* subqueries over all chat_messages (a GROUP BY and a
window sort) which degraded linearly with table growth. These tests pin the
two-step batched shape (sessions first, then an IN-scoped messages query) and
the output contract.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Callable

import pytest

from app.dao.activity_dao import ActivityDAO


class FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class SessionRow:
    """ChatSession query row: iterable for unpacking, attributes for access."""

    def __init__(self, id: uuid.UUID, agent_id: uuid.UUID, peer_agent_id: uuid.UUID | None, name: str | None) -> None:
        self.id = id
        self.agent_id = agent_id
        self.peer_agent_id = peer_agent_id
        self.name = name

    def __iter__(self) -> Any:
        yield self.id
        yield self.agent_id
        yield self.peer_agent_id
        yield self.name


class FakeSession:
    """Async session that records executed SQL and routes rows by SQL shape."""

    def __init__(self) -> None:
        self.executed_sqls: list[str] = []
        self._routes: list[tuple[Callable[[str], bool], list[Any]]] = []

    def route(self, predicate: Callable[[str], bool], rows: list[Any]) -> None:
        self._routes.append((predicate, rows))

    async def execute(self, stmt: Any) -> FakeResult:
        sql = str(stmt)
        self.executed_sqls.append(sql)
        for predicate, rows in self._routes:
            if predicate(sql):
                return FakeResult(rows)
        return FakeResult([])


class SessionFactory:
    def __init__(self, session: FakeSession) -> None:
        self.session = session

    def __call__(self) -> "SessionFactory":
        return self

    async def __aenter__(self) -> FakeSession:
        return self.session

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False


def _make_session(session_rows: list[SessionRow], stats: list[tuple], last_rows: list[tuple]) -> FakeSession:
    fake = FakeSession()
    # web/channel queries carry a LIKE prefix AND a row_number subquery, so they
    # must be routed before the generic row_number/count rules below.
    fake.route(lambda sql: "LIKE" in sql, [])
    fake.route(lambda sql: "chat_sessions" in sql, session_rows)
    fake.route(lambda sql: "row_number" in sql, last_rows)
    # The batched agent-session stats query filters by conversation_id IN.
    fake.route(lambda sql: "count" in sql and "chat_messages" in sql, stats)
    return fake


def _inject(monkeypatch: pytest.MonkeyPatch, fake: FakeSession) -> None:
    # BaseDAO.session() opens its session via app.dao.base.async_session.
    monkeypatch.setattr("app.dao.base.async_session", SessionFactory(fake))


def _agent_id() -> uuid.UUID:
    return uuid.uuid4()


def _now() -> datetime:
    return datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_agent_messages_queries_are_never_unfiltered_full_scans(monkeypatch) -> None:
    """Every chat_messages query must carry a WHERE clause (regression for the
    old unfiltered GROUP BY / window subqueries over the whole table)."""
    agent = _agent_id()
    session_id = uuid.uuid4()
    fake = _make_session(
        session_rows=[SessionRow(session_id, agent, None, "伙伴")],
        stats=[(str(session_id), 3, _now())],
        last_rows=[(str(session_id), "最新内容")],
    )
    _inject(monkeypatch, fake)

    await ActivityDAO().list_conversation_summaries(agent_id=agent)

    assert fake.executed_sqls, "expected at least one executed query"
    for sql in fake.executed_sqls:
        if "from chat_messages" in sql.lower():
            assert "where" in sql.lower(), f"unfiltered chat_messages query: {sql}"


@pytest.mark.asyncio
async def test_agent_messages_queries_are_scoped_by_conversation_id_in(monkeypatch) -> None:
    """The batched stats/last-message queries must filter by conversation_id IN
    (index-scoped) instead of aggregating the whole table."""
    agent = _agent_id()
    session_id = uuid.uuid4()
    fake = _make_session(
        session_rows=[SessionRow(session_id, agent, None, "伙伴")],
        stats=[(str(session_id), 3, _now())],
        last_rows=[(str(session_id), "最新内容")],
    )
    _inject(monkeypatch, fake)

    await ActivityDAO().list_conversation_summaries(agent_id=agent)

    scoped = [sql for sql in fake.executed_sqls if "count" in sql and "chat_messages" in sql]
    assert scoped, "expected the batched stats query"
    assert "IN (" in scoped[0], f"stats query must use conversation_id IN: {scoped[0]}"

    ranked = [sql for sql in fake.executed_sqls if "row_number" in sql]
    assert ranked, "expected the last-message window query"
    assert "IN (" in ranked[0], f"window query must be IN-scoped, not a full-table sort: {ranked[0]}"


@pytest.mark.asyncio
async def test_agent_conversation_summary_output_contract(monkeypatch) -> None:
    agent = _agent_id()
    peer = _agent_id()
    session_id = uuid.uuid4()
    last_at = _now()
    fake = _make_session(
        session_rows=[SessionRow(session_id, agent, peer, "伙伴")],
        stats=[(str(session_id), 3, last_at)],
        last_rows=[(str(session_id), "最新内容")],
    )
    _inject(monkeypatch, fake)

    summaries = await ActivityDAO().list_conversation_summaries(agent_id=agent)

    agent_entries = [c for c in summaries if c["partner_type"] == "agent"]
    assert len(agent_entries) == 1
    entry = agent_entries[0]
    assert entry["conv_id"] == str(session_id)
    assert entry["partner_id"] == str(peer)
    assert entry["partner_name"] == "🤖 伙伴"
    assert entry["last_message"] == "最新内容"
    assert entry["message_count"] == 3
    assert entry["last_at"] == last_at.isoformat()


@pytest.mark.asyncio
async def test_agent_session_without_messages_uses_defaults(monkeypatch) -> None:
    agent = _agent_id()
    session_id = uuid.uuid4()
    fake = _make_session(
        session_rows=[SessionRow(session_id, agent, None, "伙伴")],
        stats=[],
        last_rows=[],
    )
    _inject(monkeypatch, fake)

    summaries = await ActivityDAO().list_conversation_summaries(agent_id=agent)

    entry = next(c for c in summaries if c["partner_type"] == "agent")
    assert entry["message_count"] == 0
    assert entry["last_message"] == ""
    assert entry["last_at"] is None


@pytest.mark.asyncio
async def test_no_agent_sessions_yields_no_agent_entries(monkeypatch) -> None:
    fake = _make_session(session_rows=[], stats=[], last_rows=[])
    _inject(monkeypatch, fake)

    summaries = await ActivityDAO().list_conversation_summaries(agent_id=_agent_id())

    assert all(c["partner_type"] != "agent" for c in summaries)
