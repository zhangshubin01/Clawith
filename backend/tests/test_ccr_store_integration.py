"""CCR store/retrieve 集成测试 — mock PG，验证 mem 缓存与 dedup 语义。"""
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/Users/shubinzhang/Documents/agent/Clawith/backend")

import pytest
from sqlalchemy.exc import IntegrityError

from app.services.llm import ccr_store


class _FakeResult:
    def __init__(self, value=None, rows=None):
        self._value = value
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self._value

    def scalar_one(self):
        return self._value if self._value is not None else 0

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, storage: dict, *, raise_integrity: bool = False):
        self.storage = storage
        self.raise_integrity = raise_integrity

    def add(self, obj):
        key = (obj.session_id, obj.content_hash)
        if key in self.storage and self.raise_integrity:
            raise IntegrityError("dup", {}, Exception())
        self.storage[key] = {"content": obj.content, "expires_at": obj.expires_at}

    async def execute(self, stmt):
        sql = str(stmt).lower()
        if "count" in sql:
            return _FakeResult(len(self.storage))
        if "expires_at" in sql and "content," not in sql:
            return _FakeResult(None)
        if "content" in sql and "content_hash" in sql:
            if self.storage:
                k = next(iter(self.storage))
                row = self.storage[k]
                return _FakeResult(rows=[(row["content"], row["expires_at"])])
            return _FakeResult(rows=[])
        if "created_at" in sql:
            return _FakeResult(rows=[])
        return _FakeResult(None)

    async def commit(self):
        pass


class _SessionFactory:
    def __init__(self, session):
        self.session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *args):
        return False


@pytest.mark.asyncio
async def test_store_then_retrieve_mem_roundtrip(monkeypatch):
    ccr_store._MEM.clear()
    storage: dict = {}
    monkeypatch.setattr(ccr_store, "async_session", lambda: _SessionFactory(_FakeSession(storage)))

    content = "integration test payload " * 50
    h = await ccr_store.store_entry(
        session_id="sess-int-1",
        agent_id="agent-1",
        content=content,
        tool_name="search_clawhub",
        path="acp",
        original_tokens=100,
        compressed_tokens=20,
    )
    assert h is not None
    assert len(h) == 64

    out = await ccr_store.retrieve_entry("sess-int-1", h)
    assert out == content


@pytest.mark.asyncio
async def test_integrity_error_treated_as_store_ok(monkeypatch):
    ccr_store._MEM.clear()
    h = ccr_store.content_sha256("dup content")
    exp = datetime.now(timezone.utc) + timedelta(hours=24)
    storage = {("sess-dup", h): {"content": "dup content", "expires_at": exp}}
    monkeypatch.setattr(
        ccr_store,
        "async_session",
        lambda: _SessionFactory(_FakeSession(storage, raise_integrity=True)),
    )

    result = await ccr_store.store_entry(
        session_id="sess-dup",
        agent_id=None,
        content="dup content",
        tool_name="t",
        path="ws",
    )
    assert result == h


@pytest.mark.asyncio
async def test_retrieve_miss_expired(monkeypatch):
    ccr_store._MEM.clear()
    h = ccr_store.content_sha256("expired")
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    storage = {("sess-exp", h): {"content": "expired", "expires_at": past}}
    monkeypatch.setattr(ccr_store, "async_session", lambda: _SessionFactory(_FakeSession(storage)))

    out = await ccr_store.retrieve_entry("sess-exp", h)
    assert out is None
