"""Focused Session Context watermark, recent-window, and CAS tests."""

from collections import deque
from datetime import UTC, datetime, timedelta
import uuid

import pytest
from sqlalchemy.dialects import postgresql

from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.session_context_state import SessionContextState
from app.services.agent_runtime import session_context_service as service


class _Result:
    def __init__(self, *, scalar=None, rows=()):
        self._scalar = scalar
        self._rows = list(rows)

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeSession:
    def __init__(self, *results):
        self.results = deque(results)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        if not self.results:
            raise AssertionError("unexpected database execute")
        return self.results.popleft()

    async def commit(self):
        raise AssertionError("Session Context service must not commit the caller transaction")

    async def rollback(self):
        raise AssertionError("Session Context service must not roll back the caller transaction")


def _session(
    *,
    tenant_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    session_type: str = "direct",
) -> ChatSession:
    return ChatSession(
        id=session_id or uuid.uuid4(),
        tenant_id=tenant_id or uuid.uuid4(),
        session_type=session_type,
        agent_id=agent_id or uuid.uuid4(),
        title="Runtime session",
        source_channel="web",
        is_primary=True,
        deleted_at=None,
    )


def _message(
    message_id: uuid.UUID,
    *,
    session_id: uuid.UUID,
    created_at: datetime,
    role: str = "user",
) -> ChatMessage:
    return ChatMessage(
        id=message_id,
        role=role,
        content=f"content:{message_id}",
        conversation_id=str(session_id),
        created_at=created_at,
        mentions=[],
    )


def _state(
    session: ChatSession,
    *,
    version: int,
    watermark: uuid.UUID | None,
    summary: str = "summary",
) -> SessionContextState:
    return SessionContextState(
        id=uuid.uuid4(),
        tenant_id=session.tenant_id,
        agent_id=None if session.session_type == "group" else session.agent_id,
        session_id=session.id,
        summary=summary,
        requirements=["keep exact wording"],
        decisions=[{"value": "LangGraph owns execution"}],
        open_items=[],
        evidence_refs=[],
        workspace_refs=["workspace://runtime"],
        covered_through_message_id=watermark,
        version=version,
    )


def _compiled(statement, *, literal_binds: bool = True):
    return statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": literal_binds},
    )


def _sql(statement) -> str:
    return str(_compiled(statement))


@pytest.mark.asyncio
async def test_context_pack_uses_latest_state_and_recent_20_user_visible_messages():
    session = _session()
    base = datetime(2026, 7, 13, 10, 0, tzinfo=UTC)
    newest_first = [
        _message(
            uuid.UUID(int=index + 1),
            session_id=session.id,
            created_at=base + timedelta(seconds=index),
            role="user" if index % 2 == 0 else "assistant",
        )
        for index in reversed(range(20))
    ]
    state = _state(
        session,
        version=4,
        watermark=newest_first[-1].id,
    )
    db = _FakeSession(
        _Result(scalar=session),
        _Result(scalar=state),
        _Result(rows=newest_first),
    )

    pack = await service.SessionContextService().load_context_pack(
        db,
        tenant_id=session.tenant_id,
        session_id=session.id,
    )

    assert pack.snapshot.version == 4
    assert pack.snapshot.summary == "summary"
    assert len(pack.recent_messages) == 20
    assert [message["created_at"] for message in pack.recent_messages] == sorted(
        message["created_at"] for message in pack.recent_messages
    )
    recent_sql = _sql(db.statements[-1])
    assert "chat_sessions.deleted_at IS NULL" in recent_sql
    assert "chat_messages.role IN ('user', 'assistant')" in recent_sql
    assert "ORDER BY chat_messages.created_at DESC, chat_messages.id DESC" in recent_sql
    assert "LIMIT 20" in recent_sql


@pytest.mark.asyncio
async def test_incremental_read_resolves_watermark_position_before_ordered_query():
    session = _session()
    base = datetime(2026, 7, 13, 10, 0, tzinfo=UTC)
    watermark = _message(
        uuid.UUID(int=20),
        session_id=session.id,
        created_at=base,
    )
    first = _message(
        uuid.UUID(int=21),
        session_id=session.id,
        created_at=base,
        role="assistant",
    )
    second = _message(
        uuid.UUID(int=1),
        session_id=session.id,
        created_at=base + timedelta(seconds=1),
    )
    db = _FakeSession(
        _Result(scalar=session),
        _Result(scalar=watermark),
        _Result(rows=[first, second]),
    )

    messages = await service.SessionContextService().load_messages_after_watermark(
        db,
        tenant_id=session.tenant_id,
        session_id=session.id,
        covered_through_message_id=watermark.id,
    )

    assert [message["id"] for message in messages] == [str(first.id), str(second.id)]
    watermark_sql = _sql(db.statements[1])
    incremental_sql = _sql(db.statements[2])
    assert f"chat_messages.id = '{watermark.id}'" in watermark_sql
    assert "chat_messages.created_at >" in incremental_sql
    assert "chat_messages.created_at =" in incremental_sql
    assert "chat_messages.id >" in incremental_sql
    assert "ORDER BY chat_messages.created_at ASC, chat_messages.id ASC" in incremental_sql


@pytest.mark.asyncio
async def test_missing_or_foreign_watermark_requires_rebuild_instead_of_guessing():
    session = _session()
    db = _FakeSession(
        _Result(scalar=session),
        _Result(scalar=None),
    )

    with pytest.raises(service.SessionContextError) as exc_info:
        await service.SessionContextService().load_messages_after_watermark(
            db,
            tenant_id=session.tenant_id,
            session_id=session.id,
            covered_through_message_id=uuid.uuid4(),
        )

    assert exc_info.value.code == "session_context_rebuild_required"
    assert len(db.statements) == 2


@pytest.mark.asyncio
async def test_compare_and_swap_checks_version_and_watermark_together():
    session = _session()
    base = datetime(2026, 7, 13, 10, 0, tzinfo=UTC)
    old_message = _message(
        uuid.UUID(int=100),
        session_id=session.id,
        created_at=base,
    )
    new_message = _message(
        uuid.UUID(int=1),
        session_id=session.id,
        created_at=base + timedelta(seconds=1),
        role="assistant",
    )
    updated = _state(
        session,
        version=8,
        watermark=new_message.id,
        summary="updated",
    )
    db = _FakeSession(
        _Result(scalar=session),
        _Result(scalar=old_message),
        _Result(scalar=new_message),
        _Result(scalar=updated),
    )

    snapshot = await service.SessionContextService().compare_and_swap(
        db,
        tenant_id=session.tenant_id,
        session_id=session.id,
        expected_version=7,
        expected_covered_through_message_id=old_message.id,
        candidate=service.SessionContextCandidate(
            summary="updated",
            decisions=["checkpoint is authoritative"],
            covered_through_message_id=new_message.id,
        ),
    )

    assert snapshot.version == 8
    assert snapshot.covered_through_message_id == new_message.id
    compiled = _compiled(db.statements[-1], literal_binds=False)
    cas_sql = str(compiled)
    assert "session_context_states.version =" in cas_sql
    assert "covered_through_message_id IS NOT DISTINCT FROM" in cas_sql
    assert 7 in compiled.params.values()
    assert 8 in compiled.params.values()


@pytest.mark.asyncio
async def test_stale_compare_and_swap_preserves_the_winner():
    session = _session()
    db = _FakeSession(
        _Result(scalar=session),
        _Result(scalar=None),
    )

    with pytest.raises(service.SessionContextConflict):
        await service.SessionContextService().compare_and_swap(
            db,
            tenant_id=session.tenant_id,
            session_id=session.id,
            expected_version=3,
            expected_covered_through_message_id=None,
            candidate=service.SessionContextCandidate(summary="stale"),
        )


@pytest.mark.asyncio
async def test_watermark_cannot_regress_even_when_uuid_is_larger():
    session = _session()
    base = datetime(2026, 7, 13, 10, 0, tzinfo=UTC)
    expected = _message(
        uuid.UUID(int=1),
        session_id=session.id,
        created_at=base + timedelta(seconds=1),
    )
    candidate = _message(
        uuid.UUID(int=2**128 - 1),
        session_id=session.id,
        created_at=base,
    )
    db = _FakeSession(
        _Result(scalar=session),
        _Result(scalar=expected),
        _Result(scalar=candidate),
    )

    with pytest.raises(service.SessionContextError) as exc_info:
        await service.SessionContextService().compare_and_swap(
            db,
            tenant_id=session.tenant_id,
            session_id=session.id,
            expected_version=2,
            expected_covered_through_message_id=expected.id,
            candidate=service.SessionContextCandidate(
                summary="bad watermark",
                covered_through_message_id=candidate.id,
            ),
        )

    assert exc_info.value.code == "session_context_watermark_regression"
    assert len(db.statements) == 3


@pytest.mark.asyncio
async def test_first_group_context_insert_has_shared_agent_scope():
    session = _session(session_type="group")
    inserted = _state(session, version=1, watermark=None, summary="group summary")
    db = _FakeSession(
        _Result(scalar=session),
        _Result(scalar=inserted),
    )

    snapshot = await service.SessionContextService().compare_and_swap(
        db,
        tenant_id=session.tenant_id,
        session_id=session.id,
        expected_version=0,
        expected_covered_through_message_id=None,
        candidate=service.SessionContextCandidate(summary="group summary"),
    )

    assert snapshot.version == 1
    compiled = _compiled(db.statements[-1], literal_binds=False)
    insert_sql = str(compiled)
    assert "ON CONFLICT (session_id) DO NOTHING" in insert_sql
    assert "agent_id" in insert_sql
    assert None in compiled.params.values()
