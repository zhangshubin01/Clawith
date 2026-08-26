"""Unit tests for the GitLab binding API: validation, token safety, permissions."""

import uuid

import pytest
from fastapi import HTTPException

from app.api import gitlab_binding as api
from app.models.channel_config import ChannelConfig


# ── schema validation ────────────────────────────────────────


def test_put_schema_rejects_empty_project_path():
    with pytest.raises(Exception):
        api.GitlabBindingPut(token="t", project_path="   ")


def test_put_schema_rejects_bad_branch():
    with pytest.raises(Exception):
        api.GitlabBindingPut(token="t", project_path="g/r", default_branch="bad branch!")


def test_put_schema_accepts_subgroup_path():
    data = api.GitlabBindingPut(token="t", project_path="group/subgroup/repo", default_branch="f_android_ai")
    assert data.project_path == "group/subgroup/repo"
    assert data.default_branch == "f_android_ai"


def test_put_schema_rejects_long_token():
    with pytest.raises(Exception):
        api.GitlabBindingPut(token="x" * 101, project_path="g/r")


# ── response shape ───────────────────────────────────────────


def test_to_response_never_exposes_token():
    config = ChannelConfig(
        agent_id=uuid.uuid4(),
        channel_type="gitlab",
        app_secret="encrypted-blob-of-the-pat",
        is_configured=True,
        extra_config={
            "project_path": "liuyl/wwg1b",
            "default_branch": "f_android_ai",
            "init_status": "done",
            "init_commit": "abc",
        },
    )
    out = api._to_response(config)
    payload = out.model_dump()
    assert payload["has_token"] is True
    assert "encrypted-blob" not in str(payload)
    assert "app_secret" not in payload
    assert payload["project_path"] == "liuyl/wwg1b"
    assert payload["init_status"] == "done"


def test_to_response_unbound_defaults():
    out = api._to_response(None).model_dump()
    assert out["configured"] is False
    assert out["has_token"] is False
    assert out["default_branch"] == "f_android_ai"


# ── PUT first-bind token requirement ─────────────────────────


class FakeDb:
    pass


def test_put_first_bind_without_token_422(monkeypatch):
    agent_id = uuid.uuid4()
    monkeypatch.setattr(api, "_require_manage", _fake_manage_ok)
    monkeypatch.setattr(api, "_load_binding", _fake_no_binding)

    import asyncio

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            api.put_binding(
                agent_id,
                api.GitlabBindingPut(token=None, project_path="g/r"),
                current_user=_fake_user(),
                db=FakeDb(),
            )
        )
    assert exc.value.status_code == 422


async def _fake_manage_ok(db, user, agent_id):
    return None


async def _fake_no_binding(db, agent_id):
    return None


def _fake_user():
    from app.models.user import User

    return User(id=uuid.uuid4(), role="org_admin")
