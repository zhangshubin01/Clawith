"""Unit tests for the GitLab binding API: validation, token safety, permissions."""

import asyncio
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


def test_put_schema_accepts_full_url():
    data = api.GitlabBindingPut(
        token="t", project_path="http://192.168.5.254/zhangshubin/mydome1"
    )
    assert data.project_path == "zhangshubin/mydome1"
    assert data.base_url == "http://192.168.5.254"


def test_put_schema_full_url_with_port_subgroups_and_git_suffix():
    data = api.GitlabBindingPut(token="t", project_path="https://git.example.com:8443/a/b/c.git/")
    assert data.project_path == "a/b/c"
    assert data.base_url == "https://git.example.com:8443"


def test_put_schema_full_url_strips_leading_slash_path():
    data = api.GitlabBindingPut(token="t", project_path="http://h/zhangshubin/mydome1")
    assert data.project_path == "zhangshubin/mydome1"


def test_put_schema_rejects_full_url_with_credentials():
    with pytest.raises(Exception):
        api.GitlabBindingPut(token="t", project_path="http://user:pass@h/g/r")


def test_put_schema_rejects_full_url_with_query_or_fragment():
    for bad in ("http://h/g/r?ref=x", "http://h/g/r#frag"):
        with pytest.raises(Exception):
            api.GitlabBindingPut(token="t", project_path=bad)


def test_put_schema_rejects_full_url_without_path():
    for bad in ("http://h", "http://h/", "https://h"):
        with pytest.raises(Exception):
            api.GitlabBindingPut(token="t", project_path=bad)


def test_put_schema_rejects_non_http_scheme():
    with pytest.raises(Exception):
        api.GitlabBindingPut(token="t", project_path="ftp://h/g/r")


def test_put_schema_ignores_client_supplied_base_url():
    # base_url 是派生字段，客户端直接传入必须被丢弃（信任边界）
    data = api.GitlabBindingPut(token="t", project_path="g/r", base_url="http://evil.example.com")
    assert data.base_url is None
    assert data.project_path == "g/r"


def test_put_schema_strips_git_suffix():
    data = api.GitlabBindingPut(token="t", project_path="g/r.git/")
    assert data.project_path == "g/r"


def test_put_schema_rejects_unsafe_last_segment():
    for bad in ("g/..", "g/.git", "g/.tmp", "g/."):
        with pytest.raises(Exception):
            api.GitlabBindingPut(token="t", project_path=bad)


def test_put_schema_accepts_cjk_project_name():
    data = api.GitlabBindingPut(token="t", project_path="group/测试仓库")
    assert data.project_path == "group/测试仓库"


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


class _CaptureDb(FakeDb):
    """最小 Fake：add 捕获新行，commit 为空操作（与真实 AsyncSession 解耦）。"""

    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass


def _raising_decrypt(ciphertext, key):
    raise ValueError("bad ciphertext")


def _run_put(monkeypatch, agent_id, data, *, existing=None, in_flight=False):
    async def fake_load(db, aid):
        return existing

    scheduled = []

    def fake_schedule(agent_id, project_path, default_branch, pat, base_url=None):
        scheduled.append((project_path, default_branch, base_url))
        return True

    monkeypatch.setattr(api, "_require_manage", _fake_manage_ok)
    monkeypatch.setattr(api, "_load_binding", fake_load)
    monkeypatch.setattr(api, "schedule_gitlab_workspace_init", fake_schedule)
    monkeypatch.setattr(api, "init_in_flight", lambda aid: in_flight)
    db = _CaptureDb()
    asyncio.run(api.put_binding(agent_id, data, current_user=_fake_user(), db=db))
    return db, scheduled


# ── PUT full-URL normalization + fail-early states ───────────


def test_put_full_url_stores_normalized_base_url_and_schedules(monkeypatch):
    agent_id = uuid.uuid4()
    db, scheduled = _run_put(
        monkeypatch,
        agent_id,
        api.GitlabBindingPut(token="glpat-x", project_path="http://192.168.5.254:8080/zhangshubin/mydome1.git/"),
    )
    row = db.added[0]
    assert row.extra_config["project_path"] == "zhangshubin/mydome1"
    assert row.extra_config["base_url"] == "http://192.168.5.254:8080"
    assert row.extra_config["init_status"] == "pending"
    assert scheduled == [("zhangshubin/mydome1", "f_android_ai", "http://192.168.5.254:8080")]


def test_put_bare_path_leaves_base_url_none(monkeypatch):
    agent_id = uuid.uuid4()
    db, _ = _run_put(monkeypatch, agent_id, api.GitlabBindingPut(token="glpat-x", project_path="g/r"))
    assert db.added[0].extra_config["base_url"] is None


def test_put_decrypt_failure_marks_failed_without_scheduling(monkeypatch):
    agent_id = uuid.uuid4()
    existing = ChannelConfig(
        agent_id=agent_id,
        channel_type="gitlab",
        app_secret="garbage",
        is_configured=True,
        extra_config={"project_path": "g/r", "default_branch": "f_android_ai", "init_status": "done"},
    )
    monkeypatch.setattr(api, "decrypt_data", _raising_decrypt)
    db, scheduled = _run_put(
        monkeypatch, agent_id, api.GitlabBindingPut(token=None, project_path="g/r"), existing=existing
    )
    assert existing.extra_config["init_status"] == "failed"
    assert "解密" in existing.extra_config["init_error"]
    assert scheduled == []


def test_put_no_token_after_unbind_marks_failed(monkeypatch):
    agent_id = uuid.uuid4()
    existing = ChannelConfig(
        agent_id=agent_id,
        channel_type="gitlab",
        app_secret=None,
        is_configured=False,
        extra_config={"project_path": "g/r", "init_status": "unbound"},
    )
    db, scheduled = _run_put(
        monkeypatch, agent_id, api.GitlabBindingPut(token=None, project_path="g/r"), existing=existing
    )
    assert existing.extra_config["init_status"] == "failed"
    assert "Token" in existing.extra_config["init_error"]
    assert scheduled == []


def test_put_inflight_keeps_init_status_untouched(monkeypatch):
    agent_id = uuid.uuid4()
    monkeypatch.setattr(api, "decrypt_data", lambda c, k: "pat")
    existing = ChannelConfig(
        agent_id=agent_id,
        channel_type="gitlab",
        app_secret="enc",
        is_configured=True,
        extra_config={"project_path": "g/r", "init_status": "initializing"},
    )
    _, scheduled = _run_put(
        monkeypatch,
        agent_id,
        api.GitlabBindingPut(token=None, project_path="g/r"),
        existing=existing,
        in_flight=True,
    )
    assert existing.extra_config["init_status"] == "initializing"
    assert scheduled == [("g/r", "f_android_ai", None)]
