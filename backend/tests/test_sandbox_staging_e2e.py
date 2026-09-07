"""End-to-end: a direct source write reflected into the sandbox staging tree
must be visible to ``git status`` inside a real bubblewrap sandbox.

This is the deferred integration test for direction 3a (commit 930458cb). The
sandbox staging tree (B) is a one-shot clone of the run workspace (A) at the
first execute_code; direct-write tools (edit_file / write_file / ...) update
storage + A but never B, so without the staging refresh the sandbox's
``git status`` stays clean and the agent loops re-doing its edits (run
764eb591's branch-switch loop).

The test drives the real ``clone_workspace_to_staging`` / register /
``refresh_sandbox_staging_path`` primitives and asserts the outcome through a
real bwrap sandbox (via the same ``_build_bwrap_command`` the subprocess
backend runs), so it is skipped where bubblewrap is unavailable — the macOS
dev host — and runs in the backend container or CI where bwrap exists.
"""

import asyncio
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from app.services.sandbox.config import SandboxConfig
from app.services.sandbox.local.shared import (
    clone_workspace_to_staging,
    refresh_sandbox_staging_path,
    register_sandbox_staging,
    unregister_sandbox_staging,
)
from app.services.sandbox.local.subprocess_backend import SubprocessBackend

pytestmark = pytest.mark.skipif(
    shutil.which("bwrap") is None,
    reason="requires bubblewrap (bwrap); run in the backend container or CI",
)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _init_repo(repo: Path) -> None:
    """Create a git repo with one committed source file at ``repo``."""
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    _git("config", "user.email", "t@t.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "a.kt").write_text("hello\n", encoding="utf-8")
    _git("add", "a.kt", cwd=repo)
    _git("commit", "-qm", "init", cwd=repo)


async def test_direct_write_visible_to_sandbox_git_status(tmp_path: Path) -> None:
    """edit_file 直写 → refresh_sandbox_staging_path → 沙箱 git status 显示 M a.kt。

    The sandbox must observe the direct source write instead of staying clean —
    the symptom behind run 764eb591's re-do loop.
    """
    # 1. run workspace A, materialized with a repo under workspace/myrepo
    work_path = tmp_path / "work"
    _init_repo(work_path / "workspace" / "myrepo")

    # 2. one-shot clone A → staging B, exactly as _start_persistent_session does
    staging = tmp_path / "staging"
    clone_workspace_to_staging(work_path, staging)

    # 3. register B, then reflect a source write (what edit_file triggers)
    run_id = str(uuid.uuid4())
    lock = asyncio.Lock()
    register_sandbox_staging(run_id, staging, lock)
    try:
        await refresh_sandbox_staging_path(run_id, "workspace/myrepo/a.kt", b"hello, edited by edit_file\n")

        # 4. run git status inside a real bwrap sandbox over the staging tree
        backend = SubprocessBackend(SandboxConfig())
        venv_path = tmp_path / ".venv"
        venv_path.mkdir()
        cmd = backend._build_bwrap_command(
            ["git", "-C", "/workspace/myrepo", "status", "--porcelain"],
            work_path=work_path,
            venv_path=venv_path,
            staging_path=staging,
        )
        assert cmd is not None  # guarded by pytestmark, but keep mypy honest
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, f"bwrap git status failed: {proc.stderr}"
        assert proc.stdout.strip() == "M a.kt", f"expected sandbox git status to show 'M a.kt', got {proc.stdout!r}"
    finally:
        unregister_sandbox_staging(run_id)
