"""Deploy lock + registry guard tests — ADR-0003 (multi-session deploy avoidance)."""

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


def _spawn_holder(state_dir: Path, hold_seconds: float) -> subprocess.Popen[str]:
    """以 guard 持锁并 sleep 的子进程（注册表 active 条目即「已拿到锁」的信号）。"""
    return subprocess.Popen(
        [
            sys.executable,
            str(_GUARD),
            "lock",
            str(state_dir),
            "30",
            "c1",
            "backend",
            "--",
            sys.executable,
            "-c",
            f"import time; time.sleep({hold_seconds})",
        ],
    )


def _wait_until_held(state_dir: Path, deadline: float = 5.0) -> None:
    """轮询注册表直至 holder 写入 active 条目（替代固定 sleep 的竞态假设）。"""
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        try:
            if _registry(state_dir)["active"] is not None:
                return
        except (OSError, KeyError, json.JSONDecodeError):
            pass
        time.sleep(0.05)
    raise AssertionError("holder 未在期限内拿到锁")


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
    holder = _spawn_holder(tmp_path, 1.2)
    try:
        _wait_until_held(tmp_path)
        active = _registry(tmp_path)["active"]
        assert active is not None
        assert active["target_commit"] == "c1"
        assert active["scope"] == "backend"
        assert active["pid"] == holder.pid
        assert holder.poll() is None
    finally:
        holder.wait(timeout=10)
    assert _registry(tmp_path)["active"] is None


def test_lock_wait_succeeds_after_release(tmp_path: Path) -> None:
    holder = _spawn_holder(tmp_path, 0.8)
    try:
        _wait_until_held(tmp_path)
        waiter = _run_guard(tmp_path, "lock", str(tmp_path), "5", "c2", "backend", "--", sys.executable, "-c", "pass")
        assert waiter.returncode == 0
    finally:
        holder.wait(timeout=10)


def test_lock_no_wait_fails_fast_when_held(tmp_path: Path) -> None:
    holder = _spawn_holder(tmp_path, 1.5)
    try:
        _wait_until_held(tmp_path)
        waiter = _run_guard(tmp_path, "lock", str(tmp_path), "0", "c2", "backend", "--", sys.executable, "-c", "pass")
        assert waiter.returncode == LOCK_EXIT
        assert "lock" in waiter.stderr.lower()
    finally:
        holder.wait(timeout=10)


def test_lock_timeout_expires(tmp_path: Path) -> None:
    holder = _spawn_holder(tmp_path, 2.0)
    try:
        _wait_until_held(tmp_path)
        waiter = _run_guard(tmp_path, "lock", str(tmp_path), "0.5", "c2", "backend", "--", sys.executable, "-c", "pass")
        assert waiter.returncode == LOCK_EXIT
    finally:
        holder.wait(timeout=10)


def test_lock_auto_releases_when_holder_is_killed(tmp_path: Path) -> None:
    holder = _spawn_holder(tmp_path, 30)
    try:
        _wait_until_held(tmp_path)
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
        tmp_path,
        "lock",
        str(tmp_path),
        "30",
        "bad",
        "backend",
        "--",
        sys.executable,
        "-c",
        "import sys; sys.exit(3)",
    )
    entries = _registry(tmp_path)["last_deploys"]
    assert len(entries) == 2
    assert entries[0]["commit"] == "bad" and entries[0]["success"] is False
    assert entries[1]["commit"] == "good" and entries[1]["success"] is True


def _init_git_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)


def _commit(repo: Path, msg: str) -> str:
    (repo / "f.txt").write_text(msg, encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=repo, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_check_reports_undeployed_commits(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    state_dir = tmp_path / "state"

    first = _commit(repo, "first")
    _run_guard(state_dir, "lock", str(state_dir), "30", first, "backend", "--", sys.executable, "-c", "pass")
    second = _commit(repo, "second undeployed")

    dirty = _run_guard(state_dir, "check", str(state_dir), second, cwd=repo)
    assert dirty.returncode == 1
    assert "second undeployed" in dirty.stdout

    clean = _run_guard(state_dir, "check", str(state_dir), first, cwd=repo)
    assert clean.returncode == 0


def test_check_baseline_skips_failed_deploys(tmp_path: Path) -> None:
    """失败部署未改变运行镜像，不能当基线：基线必须回退到最近一次成功。"""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    state_dir = tmp_path / "state"

    deployed = _commit(repo, "actually deployed")
    _run_guard(state_dir, "lock", str(state_dir), "30", deployed, "backend", "--", sys.executable, "-c", "pass")
    failed = _commit(repo, "committed but deploy failed")
    _run_guard(
        state_dir,
        "lock",
        str(state_dir),
        "30",
        failed,
        "backend",
        "--",
        sys.executable,
        "-c",
        "import sys; sys.exit(3)",
    )
    later = _commit(repo, "after the failed deploy")

    dirty = _run_guard(state_dir, "check", str(state_dir), later, cwd=repo)
    # 基线应是 deployed（成功），range 含 failed + later 两个未部署提交
    assert dirty.returncode == 1
    assert "committed but deploy failed" in dirty.stdout
    assert "after the failed deploy" in dirty.stdout


def test_check_without_baseline_is_benign(tmp_path: Path) -> None:
    result = _run_guard(tmp_path, "check", str(tmp_path), "anything")
    assert result.returncode == 0
