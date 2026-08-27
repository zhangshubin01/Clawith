"""Unit tests for the GitLab workspace init service (three-state logic)."""

import asyncio

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
