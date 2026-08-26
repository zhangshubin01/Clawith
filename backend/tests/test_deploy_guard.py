"""Deploy lock + registry guard tests — ADR 0003 (multi-session deploy avoidance)."""

from __future__ import annotations

import importlib.util
import json
import signal
import subprocess
import sys
import time
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_GUARD = _REPO_ROOT / "scripts" / "deploy_guard.py"

# 导入 scripts/deploy_guard.py 以读取 LOCK_EXIT 常量（脚本不在包路径上）。
_spec = importlib.util.spec_from_file_location("deploy_guard", _GUARD)
assert _spec is not None and _spec.loader is not None
_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_guard)
LOCK_EXIT: int = _guard.LOCK_EXIT


def _run_guard(
    state_dir: Path,
    *args: str,
    cwd: Path | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_GUARD), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _registry(state_dir: Path) -> dict:
    return json.loads((state_dir / "registry.json").read_text(encoding="utf-8"))


def test_lock_executes_command_and_passes_exit_code(tmp_path: Path) -> None:
    ok = _run_guard(
        tmp_path,
        "lock",
        str(tmp_path),
        "30",
        "abc1234",
        "backend",
        "--",
        sys.executable,
        "-c",
        "import sys; sys.exit(0)",
    )
    assert ok.returncode == 0
    fail = _run_guard(
        tmp_path,
        "lock",
        str(tmp_path),
        "30",
        "abc1234",
        "backend",
        "--",
        sys.executable,
        "-c",
        "import sys; sys.exit(7)",
    )
    assert fail.returncode == 7


def test_lock_records_active_entry_while_running(tmp_path: Path) -> None:
    holder = subprocess.Popen(
        [
            sys.executable,
            str(_GUARD),
            "lock",
            str(tmp_path),
            "30",
            "abc1234",
            "backend",
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(1.2)",
        ],
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if (tmp_path / "registry.json").exists():
                break
            time.sleep(0.05)
        active = _registry(tmp_path)["active"]
        assert active is not None
        assert active["target_commit"] == "abc1234"
        assert active["scope"] == "backend"
        assert active["pid"] == holder.pid
        assert holder.poll() is None
    finally:
        holder.wait(timeout=10)
    assert _registry(tmp_path)["active"] is None


def test_lock_wait_succeeds_after_release(tmp_path: Path) -> None:
    holder = subprocess.Popen(
        [
            sys.executable,
            str(_GUARD),
            "lock",
            str(tmp_path),
            "30",
            "c1",
            "backend",
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(0.8)",
        ],
    )
    try:
        time.sleep(0.2)  # 确保 holder 已拿到锁
        waiter = _run_guard(tmp_path, "lock", str(tmp_path), "5", "c2", "backend", "--", sys.executable, "-c", "pass")
        assert waiter.returncode == 0
    finally:
        holder.wait(timeout=10)


def test_lock_no_wait_fails_fast_when_held(tmp_path: Path) -> None:
    holder = subprocess.Popen(
        [
            sys.executable,
            str(_GUARD),
            "lock",
            str(tmp_path),
            "30",
            "c1",
            "backend",
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(1.5)",
        ],
    )
    try:
        time.sleep(0.2)
        waiter = _run_guard(tmp_path, "lock", str(tmp_path), "0", "c2", "backend", "--", sys.executable, "-c", "pass")
        assert waiter.returncode == LOCK_EXIT
        assert "lock" in waiter.stderr.lower()
    finally:
        holder.wait(timeout=10)


def test_lock_timeout_expires(tmp_path: Path) -> None:
    holder = subprocess.Popen(
        [
            sys.executable,
            str(_GUARD),
            "lock",
            str(tmp_path),
            "30",
            "c1",
            "backend",
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(2.0)",
        ],
    )
    try:
        time.sleep(0.2)
        waiter = _run_guard(tmp_path, "lock", str(tmp_path), "0.5", "c2", "backend", "--", sys.executable, "-c", "pass")
        assert waiter.returncode == LOCK_EXIT
    finally:
        holder.wait(timeout=10)


def test_lock_auto_releases_when_holder_is_killed(tmp_path: Path) -> None:
    holder = subprocess.Popen(
        [
            sys.executable,
            str(_GUARD),
            "lock",
            str(tmp_path),
            "30",
            "c1",
            "backend",
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
        ],
    )
    try:
        time.sleep(0.3)
        assert holder.poll() is None
        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=10)
        # 内核锁随进程死亡自动释放：waiter 应立即拿到
        waiter = _run_guard(tmp_path, "lock", str(tmp_path), "2", "c2", "backend", "--", sys.executable, "-c", "pass")
        assert waiter.returncode == 0
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=10)


def test_last_deploys_recorded_on_success_and_failure(tmp_path: Path) -> None:
    _run_guard(tmp_path, "lock", str(tmp_path), "30", "good", "backend", "--", sys.executable, "-c", "pass")
    _run_guard(
        tmp_path, "lock", str(tmp_path), "30", "bad", "backend", "--", sys.executable, "-c", "import sys; sys.exit(3)"
    )
    entries = _registry(tmp_path)["last_deploys"]
    assert len(entries) == 2
    assert entries[0]["commit"] == "bad" and entries[0]["success"] is False
    assert entries[1]["commit"] == "good" and entries[1]["success"] is True


def test_check_reports_undeployed_commits(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)

    def _commit(msg: str) -> str:
        (repo / "f.txt").write_text(msg, encoding="utf-8")
        subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", msg], cwd=repo, check=True)
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()

    state_dir = tmp_path / "state"
    first = _commit("first")
    _run_guard(state_dir, "lock", str(state_dir), "30", first, "backend", "--", sys.executable, "-c", "pass")
    second = _commit("second undeployed")

    dirty = _run_guard(state_dir, "check", str(state_dir), second, cwd=repo)
    assert dirty.returncode == 1
    assert "second undeployed" in dirty.stdout

    clean = _run_guard(state_dir, "check", str(state_dir), first, cwd=repo)
    assert clean.returncode == 0


def test_check_without_baseline_is_benign(tmp_path: Path) -> None:
    result = _run_guard(tmp_path, "check", str(tmp_path), "anything")
    assert result.returncode == 0
