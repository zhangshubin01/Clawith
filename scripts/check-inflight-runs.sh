#!/usr/bin/env bash
# check-inflight-runs.sh — 部署前检查是否有 in-flight（活跃）run，防止部署杀 run / 互踩。
#
# 用法:
#   scripts/check-inflight-runs.sh           # 报告模式：始终 exit 0，只打印清单
#   scripts/check-inflight-runs.sh --strict  # 严格模式：存在活跃 run 时 exit 1（供 CI / deploy.sh 接入）
#
# 判定口径（依据 2026-08-29 两次互踩事故复盘）:
#   - 活跃 run = agent_runs.updated_at 在最近 5 分钟内的 run（backend 进程驱动，是真正的 in-flight）
#   - 等待用户 = 最近事件为 waiting_started 且 2 小时内活动过（部署同样会中断其上下文）
#   - delivery_status='pending' 含大量历史僵尸数据，不作为 in-flight 信号
#   - clawith-exec-* 容器为辅助信号：有容器但无活跃 run = 泄漏（回收待办），不影响部署判定
set -euo pipefail

STRICT=0
[ "${1:-}" = "--strict" ] && STRICT=1

PG="docker exec clawith-agent-postgres-1 psql -U clawith -d clawith -t -A -P pager=off"

ACTIVE_SQL="
SELECT r.id || E'\\t' || left(regexp_replace(r.goal, E'[\\n\\r]+', ' ', 'g'), 52)
    || E'\\t' || r.run_kind || E'\\t' || e.event_type
    || E'\\t' || round(extract(epoch FROM (now() - r.updated_at)))::int || E's'
FROM agent_runs r
LEFT JOIN LATERAL (
  SELECT event_type FROM agent_run_events e
  WHERE e.run_id = r.id ORDER BY e.created_at DESC LIMIT 1
) e ON true
WHERE r.updated_at > now() - interval '5 minutes'
ORDER BY r.updated_at DESC;"

WAITING_SQL="
SELECT r.id || E'\\t' || left(regexp_replace(r.goal, E'[\\n\\r]+', ' ', 'g'), 52)
    || E'\\t' || round(extract(epoch FROM (now() - r.updated_at)))::int || E's'
FROM agent_runs r
WHERE r.updated_at > now() - interval '2 hours'
  AND EXISTS (
    SELECT 1 FROM agent_run_events w
    WHERE w.run_id = r.id AND w.event_type = 'waiting_started'
      AND w.created_at > now() - interval '2 hours'
  )
  AND NOT EXISTS (
    SELECT 1 FROM agent_run_events c
    WHERE c.run_id = r.id AND c.event_type IN ('run_completed','run_failed')
      AND c.created_at > COALESCE((
        SELECT max(w2.created_at) FROM agent_run_events w2
        WHERE w2.run_id = r.id AND w2.event_type = 'waiting_started'
      ), '-infinity')
  )
ORDER BY r.updated_at DESC;"

ZOMBIE_SQL="SELECT count(*) FROM agent_runs
WHERE delivery_status = 'pending' AND updated_at < now() - interval '5 minutes';"

echo "== 活跃 run（最近 5 分钟，部署前应为空）=="
ACTIVE_OUT="$($PG -c "$ACTIVE_SQL")" || ACTIVE_OUT="<查询失败，请手动检查>"
ACTIVE_N=0
if [ -n "$ACTIVE_OUT" ]; then
  ACTIVE_N=$(printf '%s\n' "$ACTIVE_OUT" | sed '/^$/d' | wc -l | tr -d ' ')
  printf '%s\n' "$ACTIVE_OUT" | awk -F'\t' '{printf "  RUN %s | %s | %s | last=%s | idle %s\n", substr($1,1,8), $2, $3, $4, $5}'
else
  echo "  (无)"
fi

echo
echo "== 等待用户回复的 run（waiting_user，2 小时内）=="
WAITING_OUT="$($PG -c "$WAITING_SQL")" || WAITING_OUT="<查询失败，请手动检查>"
if [ -n "$WAITING_OUT" ]; then
  printf '%s\n' "$WAITING_OUT" | awk -F'\t' '{printf "  RUN %s | %s | idle %s\n", substr($1,1,8), $2, $3}'
else
  echo "  (无)"
fi

echo
echo "== exec 容器（辅助信号；有容器无活跃 run = 泄漏）=="
EXEC_OUT=$(docker ps --filter name=clawith-exec --format '{{.Names}}\t{{.Status}}' 2>/dev/null || true)
if [ -n "$EXEC_OUT" ]; then
  printf '%s\n' "$EXEC_OUT" | sed 's/^/  /'
else
  echo "  (无)"
fi

ZOMBIE_N=$($PG -c "$ZOMBIE_SQL" | tr -d ' ' || echo "?")
echo
echo "== 历史 pending 僵尸数据 =="
echo "  ${ZOMBIE_N} 条 delivery_status=pending 但已超 5 分钟无活动（历史遗留，不影响部署判定）"

echo
if [ "$ACTIVE_N" -gt 0 ]; then
  echo "⚠️  存在 $ACTIVE_N 个活跃 run —— 部署会中断它们（杀 run → 重放发散 / 互踩失败码）。"
  echo "   建议：等其终态后再部署；若必须立即部署，先通知相关会话。"
  [ "$STRICT" = 1 ] && exit 1
else
  echo "✅ 无活跃 run，可以安全部署。"
fi
exit 0
