"""API tests for the agent_maintainers management endpoints.

Direct-call style (mirrors test_agent_delete_api.py): the endpoints' role
gating lives in the ``get_current_admin`` dependency (security layer), so these
tests focus on the endpoint business logic — tenant isolation (via
``_require_maintainer_agent``), validation, audit logging, and status codes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from app.api import agents as agents_api
from app.core.security import get_current_admin
from app.models.agent import Agent, AgentMaintainer
from app.models.audit import AuditLog
from app.models.user import Identity, User


class RecordingDB:
    """Minimal fake AsyncSession: records add/commit/rollback."""

    def __init__(self):
        self.added: list[object] = []
        self.commit_count = 0
        self.rollback_count = 0

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None

    async def commit(self):
        self.commit_count += 1

    async def rollback(self):
        self.rollback_count += 1


def make_user(**overrides) -> User:
    values = {
        "id": uuid.uuid4(),
        "username": "alice",
        "email": "alice@example.com",
        "display_name": "Alice",
        "role": "org_admin",
        "tenant_id": uuid.uuid4(),
        "is_active": True,
    }
    values.update(overrides)
    return User(**values)


def make_agent(creator: User, **overrides) -> Agent:
    values = {
        "id": uuid.uuid4(),
        "name": "Ops Bot",
        "role_description": "assistant",
        "creator_id": creator.id,
        "tenant_id": creator.tenant_id,
        "status": "idle",
        "agent_type": "native",
        "deleted_at": None,
    }
    values.update(overrides)
    return Agent(**values)


def _patch_agent(monkeypatch, agent: Agent | None) -> None:
    async def fake_get(agent_id, db=None):
        return agent

    monkeypatch.setattr(agents_api.agent_dao, "get", fake_get)


def _audit_action(db: RecordingDB) -> str | None:
    for obj in db.added:
        if isinstance(obj, AuditLog):
            return obj.action
    return None


# ---------------------------------------------------------------------------
# GET /agents/{agent_id}/maintainers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_maintainers_returns_items_and_implicit_creator(monkeypatch):
    admin = make_user()
    creator = make_user(tenant_id=admin.tenant_id)
    agent = make_agent(creator, tenant_id=admin.tenant_id)
    maintainer_user = make_user(tenant_id=admin.tenant_id, username="bob")
    now = datetime.now(UTC)
    row = AgentMaintainer(
        id=uuid.uuid4(),
        agent_id=agent.id,
        user_id=maintainer_user.id,
        created_by=admin.id,
        created_at=now,
    )

    _patch_agent(monkeypatch, agent)
    monkeypatch.setattr(
        agents_api.maintainer_service,
        "list_maintainers",
        lambda db, agent_id: _async_return([row]),
    )
    monkeypatch.setattr(
        agents_api.user_dao,
        "list_by_ids",
        lambda ids, db=None: _async_return([maintainer_user]),
    )
    monkeypatch.setattr(
        agents_api.user_dao,
        "get_with_identity",
        lambda creator_id: _async_return(creator),
    )

    result = await agents_api.list_agent_maintainers(
        agent_id=agent.id, current_user=admin, db=RecordingDB()
    )

    assert result["maintainers"][0]["user_id"] == str(maintainer_user.id)
    assert result["maintainers"][0]["username"] == "bob"
    assert result["creator"]["user_id"] == str(creator.id)
    assert result["creator"]["is_implicit"] is True


# ---------------------------------------------------------------------------
# POST /agents/{agent_id}/maintainers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_maintainer_succeeds_and_writes_audit(monkeypatch):
    admin = make_user()
    creator = make_user(tenant_id=admin.tenant_id)
    agent = make_agent(creator, tenant_id=admin.tenant_id)
    target = make_user(tenant_id=admin.tenant_id, username="bob")
    row = AgentMaintainer(id=uuid.uuid4(), agent_id=agent.id, user_id=target.id)

    _patch_agent(monkeypatch, agent)
    monkeypatch.setattr(agents_api.user_dao, "get", lambda uid, db=None: _async_return(target))
    monkeypatch.setattr(
        agents_api.maintainer_service, "is_maintainer", lambda db, aid, uid: _async_return(False)
    )
    monkeypatch.setattr(
        agents_api.maintainer_service,
        "add_maintainer",
        lambda db, *, agent_id, user_id, created_by: _async_return(row),
    )

    db = RecordingDB()
    result = await agents_api.add_agent_maintainer(
        agent_id=agent.id,
        data={"user_id": str(target.id)},
        current_user=admin,
        db=db,
    )

    assert result["user_id"] == str(target.id)
    assert _audit_action(db) == "maintainer_added"
    assert db.commit_count == 1


@pytest.mark.asyncio
async def test_add_maintainer_rejects_creator(monkeypatch):
    admin = make_user()
    creator = make_user(tenant_id=admin.tenant_id)
    agent = make_agent(creator, tenant_id=admin.tenant_id)

    _patch_agent(monkeypatch, agent)
    monkeypatch.setattr(
        agents_api.user_dao, "get", lambda uid, db=None: _async_return(creator)
    )

    with pytest.raises(HTTPException) as exc:
        await agents_api.add_agent_maintainer(
            agent_id=agent.id,
            data={"user_id": str(creator.id)},
            current_user=admin,
            db=RecordingDB(),
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_add_maintainer_rejects_duplicate(monkeypatch):
    admin = make_user()
    creator = make_user(tenant_id=admin.tenant_id)
    agent = make_agent(creator, tenant_id=admin.tenant_id)
    target = make_user(tenant_id=admin.tenant_id)

    _patch_agent(monkeypatch, agent)
    monkeypatch.setattr(agents_api.user_dao, "get", lambda uid, db=None: _async_return(target))
    monkeypatch.setattr(
        agents_api.maintainer_service, "is_maintainer", lambda db, aid, uid: _async_return(True)
    )

    with pytest.raises(HTTPException) as exc:
        await agents_api.add_agent_maintainer(
            agent_id=agent.id,
            data={"user_id": str(target.id)},
            current_user=admin,
            db=RecordingDB(),
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_add_maintainer_rejects_cross_tenant_user(monkeypatch):
    admin = make_user()
    creator = make_user(tenant_id=admin.tenant_id)
    agent = make_agent(creator, tenant_id=admin.tenant_id)
    other_tenant_user = make_user(tenant_id=uuid.uuid4())

    _patch_agent(monkeypatch, agent)
    monkeypatch.setattr(
        agents_api.user_dao, "get", lambda uid, db=None: _async_return(other_tenant_user)
    )

    with pytest.raises(HTTPException) as exc:
        await agents_api.add_agent_maintainer(
            agent_id=agent.id,
            data={"user_id": str(other_tenant_user.id)},
            current_user=admin,
            db=RecordingDB(),
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_add_maintainer_rejects_inactive_user(monkeypatch):
    admin = make_user()
    creator = make_user(tenant_id=admin.tenant_id)
    agent = make_agent(creator, tenant_id=admin.tenant_id)
    inactive = make_user(tenant_id=admin.tenant_id, is_active=False)

    _patch_agent(monkeypatch, agent)
    monkeypatch.setattr(agents_api.user_dao, "get", lambda uid, db=None: _async_return(inactive))

    with pytest.raises(HTTPException) as exc:
        await agents_api.add_agent_maintainer(
            agent_id=agent.id,
            data={"user_id": str(inactive.id)},
            current_user=admin,
            db=RecordingDB(),
        )
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# DELETE /agents/{agent_id}/maintainers/{user_id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_maintainer_succeeds_and_writes_audit(monkeypatch):
    admin = make_user()
    creator = make_user(tenant_id=admin.tenant_id)
    agent = make_agent(creator, tenant_id=admin.tenant_id)
    target = make_user(tenant_id=admin.tenant_id)

    _patch_agent(monkeypatch, agent)
    monkeypatch.setattr(
        agents_api.maintainer_service,
        "remove_maintainer",
        lambda db, *, agent_id, user_id: _async_return(True),
    )

    db = RecordingDB()
    await agents_api.remove_agent_maintainer(
        agent_id=agent.id, user_id=target.id, current_user=admin, db=db
    )

    assert _audit_action(db) == "maintainer_removed"
    assert db.commit_count == 1


@pytest.mark.asyncio
async def test_remove_maintainer_rejects_creator(monkeypatch):
    admin = make_user()
    creator = make_user(tenant_id=admin.tenant_id)
    agent = make_agent(creator, tenant_id=admin.tenant_id)

    _patch_agent(monkeypatch, agent)

    with pytest.raises(HTTPException) as exc:
        await agents_api.remove_agent_maintainer(
            agent_id=agent.id, user_id=creator.id, current_user=admin, db=RecordingDB()
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_remove_maintainer_not_found(monkeypatch):
    admin = make_user()
    creator = make_user(tenant_id=admin.tenant_id)
    agent = make_agent(creator, tenant_id=admin.tenant_id)
    ghost = uuid.uuid4()

    _patch_agent(monkeypatch, agent)
    monkeypatch.setattr(
        agents_api.maintainer_service,
        "remove_maintainer",
        lambda db, *, agent_id, user_id: _async_return(False),
    )

    with pytest.raises(HTTPException) as exc:
        await agents_api.remove_agent_maintainer(
            agent_id=agent.id, user_id=ghost, current_user=admin, db=RecordingDB()
        )
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# _require_maintainer_agent — tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_require_maintainer_agent_cross_tenant_org_admin_forbidden(monkeypatch):
    org_admin = make_user(role="org_admin", tenant_id=uuid.uuid4())
    other_tenant = uuid.uuid4()
    agent = make_agent(make_user(tenant_id=other_tenant), tenant_id=other_tenant)

    _patch_agent(monkeypatch, agent)

    with pytest.raises(HTTPException) as exc:
        await agents_api._require_maintainer_agent(
            RecordingDB(), org_admin, agent.id
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_require_maintainer_agent_platform_admin_cross_tenant_allowed(monkeypatch):
    platform_admin = make_user(role="platform_admin", tenant_id=None)
    other_tenant = uuid.uuid4()
    agent = make_agent(make_user(tenant_id=other_tenant), tenant_id=other_tenant)

    _patch_agent(monkeypatch, agent)

    resolved = await agents_api._require_maintainer_agent(
        RecordingDB(), platform_admin, agent.id
    )
    assert resolved is agent


@pytest.mark.asyncio
async def test_require_maintainer_agent_not_found(monkeypatch):
    admin = make_user()
    _patch_agent(monkeypatch, None)

    with pytest.raises(HTTPException) as exc:
        await agents_api._require_maintainer_agent(RecordingDB(), admin, uuid.uuid4())
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# get_current_admin — role gating (spec §3.5: admin passes, non-admin 403)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["org_admin", "platform_admin"])
async def test_get_current_admin_admits_admin_roles(role):
    user = make_user(role=role)
    assert await get_current_admin(current_user=user) is user


@pytest.mark.asyncio
async def test_get_current_admin_admits_identity_platform_admin():
    user = make_user(role="member")
    user.identity = Identity(is_platform_admin=True)
    assert await get_current_admin(current_user=user) is user


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["member", "agent_admin"])
async def test_get_current_admin_rejects_non_admin(role):
    user = make_user(role=role)
    with pytest.raises(HTTPException) as exc:
        await get_current_admin(current_user=user)
    assert exc.value.status_code == 403


def _async_return(value):
    async def _inner(*_args, **_kwargs):
        return value

    return _inner()
