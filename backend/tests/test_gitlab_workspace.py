"""Unit tests for the GitLab workspace init service (three-state logic)."""

import asyncio
import uuid

import pytest

from app.services import gitlab_workspace as gw


# ── sync helpers ──────────────────────────────────────────────


def test_detect_mode_three_states(tmp_path):
    assert gw._detect_mode(tmp_path / "missing") == "clone"

    junk = tmp_path / "junk"
    junk.mkdir()
    (junk / ".tmp").mkdir()
    assert gw._detect_mode(junk) == "clone"

    code = tmp_path / "code"
    code.mkdir()
    (code / "build.gradle").write_text("x", encoding="utf-8")
    assert gw._detect_mode(code) == "adopt"

    (code / ".git").mkdir()
    assert gw._detect_mode(code) == "inject"


def test_credential_rewrite_keeps_http_scheme():
    assert gw._credential_rewrite("http://192.168.5.254", "glpat-abc") == "http://oauth2:glpat-abc@192.168.5.254/"


def test_redact_replaces_pat_only():
    out = gw._redact("push to http://x failed glpat-abc123 done", "glpat-abc123")
    assert "glpat-abc123" not in out
    assert "glpat-****" in out
    assert "push to" in out


def test_repo_dir_name_last_segment():
    assert gw._repo_dir_name("zhangshubin/my-clawith-dome") == "my-clawith-dome"
    assert gw._repo_dir_name("group/subgroup/repo") == "repo"
    assert gw._repo_dir_name("group/repo/") == "repo"
    assert gw._repo_dir_name("group/我的项目") == "我的项目"  # CJK 允许


def test_repo_dir_name_rejects_unsafe_names():
    for bad in ("group/.", "group/..", "group/.git", "group/.tmp", ".", "/"):
        with pytest.raises(ValueError):
            gw._repo_dir_name(bad)


# ── async flows with a scripted git fake ─────────────────────


class FakeGit:
    """Record argv and answer by matching a marker substring."""

    def __init__(self, script: dict[str, tuple[int, str, str]]):
        self.script = script
        self.calls: list[list[str]] = []

    async def __call__(self, args, *, cwd=None, pat=None, timeout=60):
        joined = " ".join(args)
        self.calls.append(args)
        for marker, result in self.script.items():
            if marker in joined:
                return result
        return (0, "", "")


def _run(awaitable):
    return asyncio.run(awaitable)


def test_clone_mode_branch_exists(tmp_path, monkeypatch):
    fake = FakeGit(
        {
            "ls-remote --heads origin f_android_ai": (0, "abc123\trefs/heads/f_android_ai\n", ""),
            "rev-parse HEAD": (0, "deadbeef\n", ""),
        }
    )
    monkeypatch.setattr(gw, "_run_git", fake)
    root = tmp_path / "ws"
    root.mkdir()
    commit = _run(
        gw._clone_mode(
            root,
            "http://192.168.5.254/g/r.git",
            "f_android_ai",
            "http://oauth2:glpat-x@192.168.5.254/",
            "http://192.168.5.254/",
            "测试Agent",
            "agent-x@clawith.local",
            "glpat-x",
        )
    )
    joined = [" ".join(c) for c in fake.calls]
    assert any("symbolic-ref refs/remotes/origin/HEAD" in j for j in joined)
    assert any("ls-remote --heads origin f_android_ai" in j for j in joined)
    assert any("checkout -b f_android_ai origin/f_android_ai" in j for j in joined)
    assert not any("push -u origin f_android_ai" in j for j in joined)
    assert commit == "deadbeef"


def test_clone_mode_branch_missing_creates_and_pushes(tmp_path, monkeypatch):
    fake = FakeGit(
        {
            "symbolic-ref refs/remotes/origin/HEAD": (0, "refs/remotes/origin/main\n", ""),
            "ls-remote --heads origin f_android_ai": (1, "", ""),
            "rev-parse HEAD": (0, "cafe\n", ""),
        }
    )
    monkeypatch.setattr(gw, "_run_git", fake)
    root = tmp_path / "ws"
    root.mkdir()
    _run(
        gw._clone_mode(
            root,
            "http://192.168.5.254/g/r.git",
            "f_android_ai",
            "http://oauth2:glpat-x@192.168.5.254/",
            "http://192.168.5.254/",
            "a",
            "a@clawith.local",
            "glpat-x",
        )
    )
    joined = [" ".join(c) for c in fake.calls]
    assert any("checkout -b f_android_ai origin/main" in j for j in joined)
    assert any("push -u origin f_android_ai" in j for j in joined)


def test_adopt_mode_never_deletes_files_and_pushes(tmp_path, monkeypatch):
    fake = FakeGit(
        {
            "ls-remote --heads origin main": (0, "abc\n", ""),
            "rev-parse HEAD": (0, "beef\n", ""),
        }
    )
    monkeypatch.setattr(gw, "_run_git", fake)
    root = tmp_path / "ws"
    root.mkdir()
    (root / "build.gradle").write_text("x", encoding="utf-8")
    commit, main_missing = _run(
        gw._adopt_mode(
            root,
            "http://192.168.5.254/g/r.git",
            "f_android_ai",
            "http://oauth2:glpat-x@192.168.5.254/",
            "http://192.168.5.254/",
            "测试Agent",
            "agent-x@clawith.local",
            "glpat-x",
        )
    )
    assert (root / "build.gradle").exists()  # 用户文件一个不丢
    assert (root / ".gitignore").exists()
    assert ".tmp/" in (root / ".gitignore").read_text(encoding="utf-8")
    joined = [" ".join(c) for c in fake.calls]
    assert any("init -b f_android_ai" in j for j in joined)
    assert any("push -u origin f_android_ai" in j for j in joined)
    assert commit == "beef"
    assert main_missing is False


def test_adopt_mode_flags_missing_main(tmp_path, monkeypatch):
    fake = FakeGit(
        {
            "ls-remote --heads origin main": (1, "", ""),
            "rev-parse HEAD": (0, "beef\n", ""),
        }
    )
    monkeypatch.setattr(gw, "_run_git", fake)
    root = tmp_path / "ws"
    root.mkdir()
    (root / "f.txt").write_text("x", encoding="utf-8")
    _, main_missing = _run(
        gw._adopt_mode(
            root,
            "http://192.168.5.254/g/r.git",
            "f_android_ai",
            "http://oauth2:glpat-x@192.168.5.254/",
            "http://192.168.5.254/",
            "a",
            "a@clawith.local",
            "glpat-x",
        )
    )
    assert main_missing is True


def test_apply_repo_config_writes_identity_and_rewrite(tmp_path, monkeypatch):
    fake = FakeGit({})
    monkeypatch.setattr(gw, "_run_git", fake)
    root = tmp_path / "ws"
    root.mkdir()
    (root / ".git").mkdir()
    _run(
        gw._apply_repo_config(
            root,
            "http://oauth2:glpat-x@192.168.5.254/",
            "http://192.168.5.254/",
            "测试Agent",
            "agent-x@clawith.local",
            "glpat-x",
        )
    )
    joined = [" ".join(c) for c in fake.calls]
    assert any(
        "config --local url.http://oauth2:glpat-x@192.168.5.254/.insteadOf http://192.168.5.254/" in j for j in joined
    )
    assert any("config --local user.name 测试Agent" in j for j in joined)
    assert any("config --local user.email agent-x@clawith.local" in j for j in joined)


def test_apply_repo_config_unsets_stale_pat_key(tmp_path, monkeypatch):
    """token 轮换后，指向同一 host 的旧 PAT 残留键被 unset，当前键保留。"""
    fake = FakeGit(
        {
            "--get-regexp": (
                0,
                "url.http://oauth2:glpat-old@192.168.5.254/.insteadOf http://192.168.5.254/\n",
                "",
            )
        }
    )
    monkeypatch.setattr(gw, "_run_git", fake)
    root = tmp_path / "ws"
    root.mkdir()
    (root / ".git").mkdir()
    _run(
        gw._apply_repo_config(
            root,
            "http://oauth2:glpat-x@192.168.5.254/",
            "http://192.168.5.254/",
            "测试Agent",
            "agent-x@clawith.local",
            "glpat-x",
        )
    )
    joined = [" ".join(c) for c in fake.calls]
    assert any("--unset url.http://oauth2:glpat-old@192.168.5.254/.insteadOf" in j for j in joined)
    assert not any("--unset url.http://oauth2:glpat-x@192.168.5.254/.insteadOf" in j for j in joined)


# ── inject mode（凭证注入 + origin 自愈）───────────────────────


def _inject_call(root, fake):
    return _run(
        gw._inject_mode(
            root,
            "http://192.168.5.254/g/r.git",
            "http://oauth2:glpat-x@192.168.5.254/",
            "http://192.168.5.254/",
            "测试Agent",
            "agent-x@clawith.local",
            "glpat-x",
        )
    )


def test_inject_mode_repairs_drifted_origin(tmp_path, monkeypatch):
    fake = FakeGit(
        {
            "remote get-url origin": (0, "http://old-host/g/r.git\n", ""),
            "rev-parse HEAD": (0, "deadbeef\n", ""),
        }
    )
    monkeypatch.setattr(gw, "_run_git", fake)
    root = tmp_path / "ws"
    root.mkdir()
    (root / ".git").mkdir()
    commit = _inject_call(root, fake)
    joined = [" ".join(c) for c in fake.calls]
    assert any("remote set-url origin http://192.168.5.254/g/r.git" in j for j in joined)
    assert not any("remote add origin" in j for j in joined)
    assert commit == "deadbeef"


def test_inject_mode_adds_missing_origin(tmp_path, monkeypatch):
    fake = FakeGit(
        {
            "remote get-url origin": (2, "", "error: No such remote 'origin'"),
            "rev-parse HEAD": (0, "cafe\n", ""),
        }
    )
    monkeypatch.setattr(gw, "_run_git", fake)
    root = tmp_path / "ws"
    root.mkdir()
    (root / ".git").mkdir()
    commit = _inject_call(root, fake)
    joined = [" ".join(c) for c in fake.calls]
    assert any("remote add origin http://192.168.5.254/g/r.git" in j for j in joined)
    assert not any("remote set-url" in j for j in joined)
    assert commit == "cafe"


def test_inject_mode_keeps_matching_origin(tmp_path, monkeypatch):
    fake = FakeGit(
        {
            "remote get-url origin": (0, "http://192.168.5.254/g/r.git\n", ""),
            "rev-parse HEAD": (0, "beef\n", ""),
        }
    )
    monkeypatch.setattr(gw, "_run_git", fake)
    root = tmp_path / "ws"
    root.mkdir()
    (root / ".git").mkdir()
    commit = _inject_call(root, fake)
    joined = [" ".join(c) for c in fake.calls]
    assert not any("remote set-url" in j for j in joined)
    assert not any("remote add origin" in j for j in joined)
    assert commit == "beef"


# ── legacy layout migration（v2 根仓库 → 子目录）───────────────


def test_relocate_legacy_success_drops_root_git(tmp_path, monkeypatch):
    fake = FakeGit({})
    monkeypatch.setattr(gw, "_run_git", fake)
    root = tmp_path / "ws"
    root.mkdir()
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("x", encoding="utf-8")
    (root / "notes.txt").write_text("untracked", encoding="utf-8")  # 根下未跟踪文件保留
    repo = root / "my-repo"
    _run(gw._relocate_legacy(root, repo, "glpat-x"))
    assert not (root / ".git").exists()
    assert (root / "notes.txt").exists()
    joined = [" ".join(c) for c in fake.calls]
    assert any(f"clone --local --no-hardlinks {root} {repo}" in j for j in joined)


def test_relocate_legacy_failure_keeps_root_git_and_raises(tmp_path, monkeypatch):
    fake = FakeGit({"clone --local --no-hardlinks": (1, "", "clone failed")})
    monkeypatch.setattr(gw, "_run_git", fake)
    root = tmp_path / "ws"
    root.mkdir()
    (root / ".git").mkdir()
    repo = root / "my-repo"
    repo.mkdir()
    (repo / "half").write_text("x", encoding="utf-8")
    with pytest.raises(RuntimeError):
        _run(gw._relocate_legacy(root, repo, "glpat-x"))
    assert (root / ".git").exists()  # 失败不动根 .git
    assert not repo.exists()  # 半成品 repo 目录被清理


# ── guide ─────────────────────────────────────────────────────


def test_guide_content_written(tmp_path):
    gw._write_guide(tmp_path, "wwg1b", "liuyl/wwg1b", "f_android_ai", adopt_note=False)
    text = (tmp_path / "GITLAB_GUIDE.md").read_text(encoding="utf-8")
    assert "workspace/wwg1b/" in text
    assert "cd wwg1b" in text
    assert "git -C wwg1b" in text
    assert "不属于仓库" in text  # 根下其他文件不入库
    assert "f_android_ai" in text
    assert "merge_request.create" in text
    assert "main 只能经 MR" in text
    assert "liuyl/wwg1b" in text

    gw._write_guide(tmp_path, "wwg1b", "liuyl/wwg1b", "f_android_ai", adopt_note=True)
    text = (tmp_path / "GITLAB_GUIDE.md").read_text(encoding="utf-8")
    assert "尚未发现 `main`" in text


# ── 幂等调度（schedule_gitlab_workspace_init）─────────────────


def _schedule_scenario(monkeypatch):
    """在一个事件循环内跑完整调度场景。返回 (calls, results)。"""

    calls: list[tuple] = []

    async def _slow_init(agent_id, project_path, default_branch, pat, base_url=None):
        calls.append((project_path, default_branch, base_url))
        await asyncio.sleep(0.05)

    monkeypatch.setattr(gw, "run_gitlab_workspace_init", _slow_init)

    async def _run():
        agent_id = uuid.uuid4()
        results = []
        results.append(("first", gw.schedule_gitlab_workspace_init(agent_id, "g/r", "f_android_ai", "pat", "http://h")))
        # 在途同签名重存：复用，不重复排队
        results.append(("dup", gw.schedule_gitlab_workspace_init(agent_id, "g/r", "f_android_ai", "pat", "http://h")))
        assert gw.init_in_flight(agent_id) is True
        # 在途但签名变化：新任务排队（agent 锁串行）
        results.append(("changed", gw.schedule_gitlab_workspace_init(agent_id, "g/r2", "f_android_ai", "pat", "http://h")))
        await asyncio.sleep(0.2)  # 等两个任务完成并自清理
        assert gw.init_in_flight(agent_id) is False
        # 任务结束后再次相同保存：重新调度
        results.append(("after-done", gw.schedule_gitlab_workspace_init(agent_id, "g/r", "f_android_ai", "pat", "http://h")))
        await asyncio.sleep(0.1)
        return calls, results

    return asyncio.run(_run())


def test_schedule_dedups_identical_inflight_signature(monkeypatch):
    calls, results = _schedule_scenario(monkeypatch)
    assert results == [("first", True), ("dup", False), ("changed", True), ("after-done", True)]
    assert calls == [
        ("g/r", "f_android_ai", "http://h"),
        ("g/r2", "f_android_ai", "http://h"),
        ("g/r", "f_android_ai", "http://h"),
    ]


def test_schedule_signature_distinguishes_base_url(monkeypatch):
    calls: list[tuple] = []

    async def _counting_init(agent_id, project_path, default_branch, pat, base_url=None):
        calls.append((project_path, base_url))
        await asyncio.sleep(0.02)

    monkeypatch.setattr(gw, "run_gitlab_workspace_init", _counting_init)

    async def _run():
        agent_id = uuid.uuid4()
        assert gw.schedule_gitlab_workspace_init(agent_id, "g/r", "f_android_ai", "pat", "http://h1") is True
        assert gw.schedule_gitlab_workspace_init(agent_id, "g/r", "f_android_ai", "pat", "http://h2") is True
        await asyncio.sleep(0.1)

    asyncio.run(_run())
    assert calls == [("g/r", "http://h1"), ("g/r", "http://h2")]


# ── 物化时凭证重注入（sandbox .git 凭证补齐）──────────────────


def _fake_repo_copy(root, name="mydome1"):
    repo = root / "workspace" / name
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return repo


def test_inject_temp_workspace_no_git_skips_binding(tmp_path, monkeypatch):
    async def boom(agent_id):
        raise AssertionError("no .git in copy → binding lookup must be skipped")

    monkeypatch.setattr(gw, "_load_binding_credential", boom)
    ws = tmp_path / "ws"
    (ws / "workspace" / "proj").mkdir(parents=True)
    assert _run(gw.inject_credentials_into_temp_workspace(ws, uuid.uuid4())) is False


def test_inject_temp_workspace_no_binding_noop(tmp_path, monkeypatch):
    async def no_cred(agent_id):
        return None

    monkeypatch.setattr(gw, "_load_binding_credential", no_cred)
    ws = tmp_path / "ws"
    _fake_repo_copy(ws)
    assert _run(gw.inject_credentials_into_temp_workspace(ws, uuid.uuid4())) is False


def test_inject_temp_workspace_applies_repo_config(tmp_path, monkeypatch):
    async def fake_cred(agent_id):
        return ("glpat-test123", "http://192.168.5.254", "mydome1", "Android 工程师 07", "agent-abc@clawith.local")

    monkeypatch.setattr(gw, "_load_binding_credential", fake_cred)
    fake = FakeGit({})
    monkeypatch.setattr(gw, "_run_git", fake)
    ws = tmp_path / "ws"
    _fake_repo_copy(ws)
    assert _run(gw.inject_credentials_into_temp_workspace(ws, uuid.uuid4())) is True
    joined = [" ".join(c) for c in fake.calls]
    assert any("url.http://oauth2:glpat-test123@192.168.5.254/.insteadOf http://192.168.5.254/" in j for j in joined)
    assert any("user.name Android 工程师 07" in j for j in joined)
    assert any("user.email agent-abc@clawith.local" in j for j in joined)


def test_inject_temp_workspace_repo_name_mismatch_noop(tmp_path, monkeypatch):
    async def fake_cred(agent_id):
        return ("glpat-test123", "http://192.168.5.254", "other-repo", "n", "e@x")

    monkeypatch.setattr(gw, "_load_binding_credential", fake_cred)
    fake = FakeGit({})
    monkeypatch.setattr(gw, "_run_git", fake)
    ws = tmp_path / "ws"
    _fake_repo_copy(ws, name="mydome1")
    assert _run(gw.inject_credentials_into_temp_workspace(ws, uuid.uuid4())) is False
    assert fake.calls == []


def test_inject_temp_workspace_git_failure_returns_false(tmp_path, monkeypatch):
    async def fake_cred(agent_id):
        return ("glpat-test123", "http://192.168.5.254", "mydome1", "n", "e@x")

    monkeypatch.setattr(gw, "_load_binding_credential", fake_cred)
    monkeypatch.setattr(gw, "_run_git", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("git broken")))
    ws = tmp_path / "ws"
    _fake_repo_copy(ws)
    assert _run(gw.inject_credentials_into_temp_workspace(ws, uuid.uuid4())) is False


# ── _load_binding_credential（DB/解密层）──────────────────────


class _Scalar:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    """Answers the two queries in order: ChannelConfig, then Agent."""

    def __init__(self, config=None, agent=None):
        self._config = config
        self._agent = agent
        self._queries = 0

    async def execute(self, stmt):
        self._queries += 1
        return _Scalar(self._config if self._queries == 1 else self._agent)


class _FakeDaoSessionCtx:
    def __init__(self, inner):
        self._inner = inner

    async def __aenter__(self):
        return self._inner

    async def __aexit__(self, *exc):
        return False


class _FakeQueryDao:
    def __init__(self, inner):
        self._inner = inner

    def session(self, *, readonly=False):
        return _FakeDaoSessionCtx(self._inner)


def _patch_query_dao(monkeypatch, inner):
    from app.dao import query_dao  # QueryDAO 实例（包属性，非子模块）

    monkeypatch.setattr(query_dao, "session", _FakeQueryDao(inner).session)


def _patch_decrypt(monkeypatch, fn):
    import app.core.security as sec

    monkeypatch.setattr(sec, "decrypt_data", fn)


def test_load_binding_credential_no_binding_none(monkeypatch):
    _patch_query_dao(monkeypatch, _FakeSession(config=None))
    assert _run(gw._load_binding_credential(uuid.uuid4())) is None


def test_load_binding_credential_decrypt_failure_none(monkeypatch):
    from app.models.channel_config import ChannelConfig

    agent_id = uuid.uuid4()
    config = ChannelConfig(
        agent_id=agent_id,
        channel_type="gitlab",
        app_secret="garbage",
        is_configured=True,
        extra_config={
            "project_path": "zhangshubin/mydome1",
            "base_url": "http://192.168.5.254",
            "init_status": "done",
        },
    )
    _patch_query_dao(monkeypatch, _FakeSession(config=config))
    _patch_decrypt(monkeypatch, lambda c, k: (_ for _ in ()).throw(ValueError("bad ciphertext")))
    assert _run(gw._load_binding_credential(agent_id)) is None


def test_load_binding_credential_success(monkeypatch):
    from types import SimpleNamespace

    from app.models.channel_config import ChannelConfig

    agent_id = uuid.uuid4()
    config = ChannelConfig(
        agent_id=agent_id,
        channel_type="gitlab",
        app_secret="cipher",
        is_configured=True,
        extra_config={
            "project_path": "zhangshubin/mydome1",
            "base_url": "http://192.168.5.254/",
            "init_status": "done",
        },
    )
    _patch_query_dao(monkeypatch, _FakeSession(config=config, agent=SimpleNamespace(name="Android 工程师 07")))
    _patch_decrypt(monkeypatch, lambda c, k: "glpat-secret")
    assert _run(gw._load_binding_credential(agent_id)) == (
        "glpat-secret",
        "http://192.168.5.254",
        "mydome1",
        "Android 工程师 07",
        f"agent-{agent_id.hex[:8]}@clawith.local",
    )
