"""Unit tests for the GitLab workspace init service (three-state logic)."""

import asyncio


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


def test_guide_content_written(tmp_path):
    gw._write_guide(tmp_path, "liuyl/wwg1b", "f_android_ai", adopt_note=False)
    text = (tmp_path / "GITLAB_GUIDE.md").read_text(encoding="utf-8")
    assert "f_android_ai" in text
    assert "merge_request.create" in text
    assert "main 只能经 MR" in text
    assert "liuyl/wwg1b" in text

    gw._write_guide(tmp_path, "liuyl/wwg1b", "f_android_ai", adopt_note=True)
    text = (tmp_path / "GITLAB_GUIDE.md").read_text(encoding="utf-8")
    assert "尚未发现 `main`" in text
