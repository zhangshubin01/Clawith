#!/usr/bin/env python3
"""Deploy lock + registry guard — ADR 0003 (multi-session deploy avoidance).

两个子命令，供 deploy.sh / restart.sh 使用（stdlib only）：

  deploy_guard.py lock <state_dir> <timeout_seconds> <commit> <scope> -- <cmd...>
      对 <state_dir>/deploy.lock 取 fcntl 排他锁（timeout=0 即 fail-fast），
      写注册表 active 条目，然后运行 <cmd>（SIGTERM/SIGINT 转发给子进程）。
      cmd 结束后清 active、追加 last_deploys（保留 20 条）、以 cmd 的退出码退出。
      锁在进程死亡时由内核自动释放（部署进程被杀不留陈旧锁）。

  deploy_guard.py check <state_dir> <target_commit>
      取注册表最近一次部署 commit 作基线，展示 git log baseline..target；
      有未部署提交 exit 1（供 --strict 中止），否则 exit 0。无基线/ git 不可用
      或基线已消失（rebase）时不阻塞，exit 0。

注册表 <state_dir>/registry.json：
  {"active": {pid, started_at, target_commit, scope} | null,
   "last_deploys": [{at, commit, image_sha, scope, success}]}
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# 撞锁/等待超时的固定退出码（deploy.sh 靠 set -e 传播）。
LOCK_EXIT = 9
_KEEP_LAST_DEPLOYS = 20


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_registry(state_dir: Path) -> dict:
    path = state_dir / "registry.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_registry(state_dir: Path, data: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / "registry.json.tmp"
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(state_dir / "registry.json")


def _read_result_marker(state_dir: Path) -> str | None:
    """deploy.sh 在 up 成功后写入 {image_sha}，供注册表记录。"""
    marker = state_dir / "pending-result.json"
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        marker.unlink()
    except OSError:
        pass
    image_sha = data.get("image_sha") if isinstance(data, dict) else None
    return image_sha if isinstance(image_sha, str) else None


def _lock(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "deploy.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)

    deadline = time.monotonic() + args.timeout
    acquired = False
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            break
        except OSError:
            # 锁被他人持有（EAGAIN/EWOULDBLOCK）——唯一预期路径，继续轮询。
            if args.timeout == 0 or time.monotonic() >= deadline:
                break
            time.sleep(0.2)

    if not acquired:
        if args.timeout == 0:
            print(
                f"[deploy-guard] 部署锁被占用（{lock_path}），--no-wait 立即失败。",
                file=sys.stderr,
            )
        else:
            print(
                f"[deploy-guard] 部署锁被占用（{lock_path}），等待 {args.timeout}s 超时。",
                file=sys.stderr,
            )
        os.close(fd)
        return LOCK_EXIT

    registry = _load_registry(state_dir)
    registry["active"] = {
        "pid": os.getpid(),
        "started_at": _now(),
        "target_commit": args.commit,
        "scope": args.scope,
    }
    _save_registry(state_dir, registry)

    child: subprocess.Popen[str] | None = None

    def _forward(signum: int, _frame: object) -> None:
        if child is not None and child.poll() is None:
            child.send_signal(signum)

    prev_term = signal.signal(signal.SIGTERM, _forward)
    prev_int = signal.signal(signal.SIGINT, _forward)
    rc: int = 1
    try:
        child = subprocess.Popen(args.cmd)
        rc = child.wait()
    finally:
        image_sha = _read_result_marker(state_dir)
        registry = _load_registry(state_dir)
        registry["active"] = None
        entries = registry.get("last_deploys") or []
        entries.insert(
            0,
            {
                "at": _now(),
                "commit": args.commit,
                "image_sha": image_sha,
                "scope": args.scope,
                "success": rc == 0,
            },
        )
        registry["last_deploys"] = entries[:_KEEP_LAST_DEPLOYS]
        _save_registry(state_dir, registry)
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        signal.signal(signal.SIGTERM, prev_term)
        signal.signal(signal.SIGINT, prev_int)
    return rc


def _tip_check(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    entries = _load_registry(state_dir).get("last_deploys") or []
    # 基线=最近一次**成功**部署；失败部署并未改变运行镜像，不能当基线
    # （否则下一次 check 会漏报真正将上线的提交）。
    baseline = next(
        (
            entry["commit"]
            for entry in entries
            if isinstance(entry.get("commit"), str)
            and entry["commit"]
            and entry.get("success") is True
        ),
        None,
    )
    if baseline is None:
        print("[deploy-guard] 注册表无基线（首次部署或换机），跳过 tip 对比。")
        return 0
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", f"{baseline}..{args.target_commit}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        print("[deploy-guard] git 不可用，跳过 tip 对比。")
        return 0
    if result.returncode != 0:
        print(
            f"[deploy-guard] git log 失败（baseline={baseline[:8]} 可能已被 rebase 移除），跳过对比。"
        )
        return 0
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        print("[deploy-guard] 无未部署提交：运行镜像与目标 commit 链一致。")
        return 0
    print(
        f"[deploy-guard] ⚠️ 自上次部署（{baseline[:8]}）以来有 {len(lines)} 个提交将随本次部署上线："
    )
    for line in lines:
        print(f"    {line}")
    return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    lock = sub.add_parser("lock", help="持有部署锁并执行命令")
    lock.add_argument("state_dir")
    lock.add_argument("timeout", type=float, help="等待秒数，0 = 立即失败（--no-wait）")
    lock.add_argument("commit")
    lock.add_argument("scope")
    lock.add_argument("cmd", nargs=argparse.REMAINDER)

    check = sub.add_parser("check", help="对比上次部署与目标 commit")
    check.add_argument("state_dir")
    check.add_argument("target_commit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "lock":
        if not args.cmd:
            print("[deploy-guard] lock 需要 -- <命令...>", file=sys.stderr)
            return 2
        return _lock(args)
    return _tip_check(args)


if __name__ == "__main__":
    raise SystemExit(main())
