#!/usr/bin/env python3
"""Read-only gate-health weekly report — G4 门禁健康度周报（plan §5）。

沿 scripts/check-inflight-runs.sh 同款模式：通过 `docker exec psql` 直读
clawith-agent-postgres，只发 SELECT、零写入、零新埋点。stdlib only。

三条曲线（plan §5.1，口径以 PG 台账为准，不读 Langfuse）：

  1. 固化率 = 既写 `workspace/` 又写 `memory/` 的 run ÷ 写 `workspace/` 的 run
     （tool_name IN write_file/edit_file，按 sanitized_arguments.path 前缀判定，交集口径）
  2. 跳过率 = `memory_consolidation_skipped` 事件数 ÷ `run_completed` 的 run 数
  3. 拒绝率 = `tool_permission_denied` 失败执行计数 + actor 分布
     （error_code 落在 agent_tool_executions.result_metadata->>'error_code'，
       表无独立 error_code 列；actor 取 agent_runs.origin_user_id/origin_agent_id）

用法:
  scripts/memory_quality_report.py            # 默认最近 7 天
  scripts/memory_quality_report.py --days 30

连接可用环境变量覆盖（默认值对齐 check-inflight-runs.sh）:
  CLAWITH_PG_CONTAINER (默认 clawith-agent-postgres-1)
  CLAWITH_PG_USER      (默认 clawith)
  CLAWITH_PG_DB        (默认 clawith)
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys


def _psql(container: str, db_user: str, db_name: str, sql: str) -> list[str]:
    """Run one read-only SELECT via docker exec psql; return non-empty rows."""
    cmd = [
        "docker",
        "exec",
        container,
        "psql",
        "-U",
        db_user,
        "-d",
        db_name,
        "-t",
        "-A",
        "-F",
        "|",
        "-P",
        "pager=off",
        "-c",
        sql,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"psql 退出码 {proc.returncode}")
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _since_expr(days: int) -> str:
    # window start (inclusive), aligned to start of day in the session timezone
    return f"date_trunc('day', now()) - ({days} - 1) * interval '1 day'"


def _daily_consolidation(container: str, user: str, db: str, days: int) -> list[tuple[str, int, int]]:
    """Per-day (both_runs, ws_runs) for write_file/edit_file path prefixes.

    固化率 = 既写 workspace/ 又写 memory/ 的 run ÷ 写 workspace/ 的 run（交集口径，
    0-100%）。分母「有 workspace 写的 run」按 plan §5.1；分子取同 run 的
    memory/ 写，避免把「只固化 memory 的后台 run」算进分子导致 >100%。
    """
    sql = f"""
SELECT d::date AS day,
       COALESCE(b.c, 0) AS both_runs,
       COALESCE(w.c, 0) AS ws_runs
FROM generate_series({_since_expr(days)}, date_trunc('day', now()), interval '1 day') AS d
LEFT JOIN (
  SELECT date_trunc('day', t.completed_at)::date AS day, count(DISTINCT t.run_id) AS c
  FROM agent_tool_executions t
  WHERE t.tool_name IN ('write_file','edit_file')
    AND t.sanitized_arguments->>'path' LIKE 'workspace/%'
    AND t.completed_at >= {_since_expr(days)}
    AND EXISTS (
      SELECT 1 FROM agent_tool_executions m
      WHERE m.run_id = t.run_id
        AND m.tool_name IN ('write_file','edit_file')
        AND m.sanitized_arguments->>'path' LIKE 'memory/%'
        AND m.completed_at >= {_since_expr(days)}
    )
  GROUP BY 1
) b ON b.day = d::date
LEFT JOIN (
  SELECT date_trunc('day', completed_at)::date AS day, count(DISTINCT run_id) AS c
  FROM agent_tool_executions
  WHERE tool_name IN ('write_file','edit_file')
    AND sanitized_arguments->>'path' LIKE 'workspace/%'
    AND completed_at >= {_since_expr(days)}
  GROUP BY 1
) w ON w.day = d::date
ORDER BY d::date;
"""
    out: list[tuple[str, int, int]] = []
    for line in _psql(container, user, db, sql):
        day, both, ws = line.split("|")
        out.append((day, int(both), int(ws)))
    return out


def _daily_skip(container: str, user: str, db: str, days: int) -> list[tuple[str, int, int]]:
    """Per-day (skip_events, completed_runs)."""
    sql = f"""
SELECT d::date AS day,
       COALESCE(s.c, 0) AS skip_events,
       COALESCE(r.c, 0) AS completed_runs
FROM generate_series({_since_expr(days)}, date_trunc('day', now()), interval '1 day') AS d
LEFT JOIN (
  SELECT date_trunc('day', created_at)::date AS day, count(*) AS c
  FROM agent_run_events
  WHERE event_type = 'memory_consolidation_skipped'
    AND created_at >= {_since_expr(days)}
  GROUP BY 1
) s ON s.day = d::date
LEFT JOIN (
  SELECT date_trunc('day', created_at)::date AS day, count(DISTINCT run_id) AS c
  FROM agent_run_events
  WHERE event_type = 'run_completed'
    AND created_at >= {_since_expr(days)}
  GROUP BY 1
) r ON r.day = d::date
ORDER BY d::date;
"""
    out: list[tuple[str, int, int]] = []
    for line in _psql(container, user, db, sql):
        day, skips, done = line.split("|")
        out.append((day, int(skips), int(done)))
    return out


def _daily_denials(container: str, user: str, db: str, days: int) -> list[tuple[str, int]]:
    """Per-day count of tool_permission_denied failures."""
    sql = f"""
SELECT d::date AS day,
       COALESCE(x.c, 0) AS denials
FROM generate_series({_since_expr(days)}, date_trunc('day', now()), interval '1 day') AS d
LEFT JOIN (
  SELECT date_trunc('day', completed_at)::date AS day, count(*) AS c
  FROM agent_tool_executions
  WHERE status = 'failed'
    AND result_metadata->>'error_code' = 'tool_permission_denied'
    AND completed_at >= {_since_expr(days)}
  GROUP BY 1
) x ON x.day = d::date
ORDER BY d::date;
"""
    out: list[tuple[str, int]] = []
    for line in _psql(container, user, db, sql):
        day, denials = line.split("|")
        out.append((day, int(denials)))
    return out


def _top_denied_agents(container: str, user: str, db: str, days: int) -> list[tuple[str, int]]:
    """Which agents' workspace got the most denied file-modify attempts."""
    sql = f"""
SELECT r.agent_id::text AS agent_id, count(*) AS n
FROM agent_tool_executions t
JOIN agent_runs r ON r.id = t.run_id
WHERE t.status = 'failed'
  AND t.result_metadata->>'error_code' = 'tool_permission_denied'
  AND t.completed_at >= {_since_expr(days)}
GROUP BY 1
ORDER BY n DESC
LIMIT 10;
"""
    out: list[tuple[str, int]] = []
    for line in _psql(container, user, db, sql):
        agent_id, n = line.split("|")
        out.append((agent_id, int(n)))
    return out


def _top_denied_actors(container: str, user: str, db: str, days: int) -> list[tuple[str, str, int]]:
    """Which actors (user or originating agent) drove the denied attempts."""
    sql = f"""
SELECT r.origin_user_id::text AS user_id,
       r.origin_agent_id::text AS agent_id,
       count(*) AS n
FROM agent_tool_executions t
JOIN agent_runs r ON r.id = t.run_id
WHERE t.status = 'failed'
  AND t.result_metadata->>'error_code' = 'tool_permission_denied'
  AND t.completed_at >= {_since_expr(days)}
GROUP BY 1, 2
ORDER BY n DESC
LIMIT 10;
"""
    out: list[tuple[str, str, int]] = []
    for line in _psql(container, user, db, sql):
        parts = line.split("|")
        if len(parts) != 3:
            continue
        user_id, agent_id, n = parts
        out.append((user_id, agent_id, int(n)))
    return out


def _pct(num: int, den: int) -> str:
    if den <= 0:
        return "  —  "
    return f"{num / den * 100:5.1f}%"


def _short(uid: str) -> str:
    return uid[:8] if uid else "—"


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only gate-health weekly report (G4, plan §5)")
    parser.add_argument("--days", type=int, default=7, help="回看窗口天数（默认 7）")
    args = parser.parse_args()

    container = os.environ.get("CLAWITH_PG_CONTAINER", "clawith-agent-postgres-1")
    db_user = os.environ.get("CLAWITH_PG_USER", "clawith")
    db_name = os.environ.get("CLAWITH_PG_DB", "clawith")

    try:
        consolidations = _daily_consolidation(container, db_user, db_name, args.days)
        skips = _daily_skip(container, db_user, db_name, args.days)
        denials = _daily_denials(container, db_user, db_name, args.days)
        top_agents = _top_denied_agents(container, db_user, db_name, args.days)
        top_actors = _top_denied_actors(container, db_user, db_name, args.days)
    except RuntimeError as exc:
        print(f"❌ 查询失败（docker/psql 不可用？）：{exc}", file=sys.stderr)
        return 1

    n = len(denials)
    from_day = denials[0][0] if n else "—"
    to_day = denials[-1][0] if n else "—"
    print(f"== G4 门禁健康度周报（最近 {args.days} 天，{from_day} → {to_day}，只读 PG 台账）==")
    print()
    print(f"{'日期':<12} {'固化(both/ws)':<16} {'跳过(skip/done)':<18} {'拒绝数':>6}")
    print("-" * 56)
    for i in range(n):
        day, mem_runs, ws_runs = consolidations[i]
        _, skip_events, done_runs = skips[i]
        _, dcount = denials[i]
        consol = f"{_pct(mem_runs, ws_runs)} ({mem_runs}/{ws_runs})"
        skip = f"{_pct(skip_events, done_runs)} ({skip_events}/{done_runs})"
        print(f"{day:<12} {consol:<16} {skip:<18} {dcount:>6}")

    tot_mem = sum(r[1] for r in consolidations)
    tot_ws = sum(r[2] for r in consolidations)
    tot_skip = sum(r[1] for r in skips)
    tot_done = sum(r[2] for r in skips)
    tot_denials = sum(r[1] for r in denials)
    print("-" * 56)
    print(f"{'合计':<12} {_pct(tot_mem, tot_ws):<16} {_pct(tot_skip, tot_done):<18} {tot_denials:>6}")
    print()
    print(
        f"固化率合计 = {_pct(tot_mem, tot_ws)}（既写 workspace 又写 memory 的 run {tot_mem} ÷ 写 workspace 的 run {tot_ws}）"
    )
    print(
        f"跳过率合计 = {_pct(tot_skip, tot_done)}（memory_consolidation_skipped {tot_skip} ÷ run_completed {tot_done}）"
    )

    print()
    print("== 门控拒绝 Top agent（被改文件的 agent）==")
    if top_agents:
        for agent_id, cnt in top_agents:
            print(f"  {agent_id}  {cnt} 次")
    else:
        print("  （窗口内无 tool_permission_denied）")

    print()
    print("== 门控拒绝 Top actor（发起者 origin_user_id / origin_agent_id）==")
    if top_actors:
        for user_id, agent_id, cnt in top_actors:
            who = f"user={_short(user_id)}" if user_id else f"agent={_short(agent_id)}"
            print(f"  {who}  {cnt} 次")
    else:
        print("  （窗口内无 tool_permission_denied）")

    print()
    print("注：拒绝数 > 0 且 Top actor 含 creator 或维护人员 = 误伤信号（plan §7 / grill 决策 4），需回退或调边界。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
