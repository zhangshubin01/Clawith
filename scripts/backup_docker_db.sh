#!/usr/bin/env bash
# Clawith Docker PostgreSQL 定时备份
# 直接通过 docker exec 备份，不依赖宿主机端口映射
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-$ROOT_DIR/backups}"
STAMP="$(date +%F_%H%M%S)"
OUT_DIR="$BACKUP_DIR/snapshot_${STAMP}"

mkdir -p "$OUT_DIR"
DUMP_FILE="$OUT_DIR/clawith.dump"
META_FILE="$OUT_DIR/clawith.meta.txt"

echo "[$(date '+%F %T')] backup starting -> $OUT_DIR"

# 1) 从 Docker 容器内导出数据库
docker exec -e PGPASSWORD=clawith clawith-postgres-1 \
    pg_dump -Fc -U clawith -d clawith > "$DUMP_FILE"

# 2) 导出元数据目录
docker exec -i clawith-postgres-1 pg_restore -l < "$DUMP_FILE" > "$META_FILE"

# 3) 备份 agent 工作区文件（如有）
AGENT_DIR="${HOME}/.clawith/data/agents"
if [[ -d "$AGENT_DIR" ]]; then
    tar -czf "$OUT_DIR/agents_data.tar.gz" -C "${HOME}/.clawith/data" agents 2>/dev/null || true
fi

# 4) 生成 README
cat > "$OUT_DIR/README.txt" <<EOF
Clawith backup snapshot
Created: ${STAMP}
Source: Docker postgres (clawith-postgres-1)

Files:
  - clawith.dump       (pg_dump -Fc)
  - clawith.meta.txt   (table catalog)
  - agents_data.tar.gz (agent workspaces)

Restore:
  docker cp clawith.dump clawith-postgres-1:/tmp/restore.dump
  docker exec clawith-postgres-1 psql -U clawith -d clawith -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
  docker exec clawith-postgres-1 pg_restore -U clawith -d clawith --no-owner --no-privileges /tmp/restore.dump
  docker compose restart backend
EOF

# 5) 保留最近 7 天的备份，删除更旧的
find "$BACKUP_DIR" -maxdepth 1 -type d -name "snapshot_*" -mtime +7 -exec rm -rf {} \; 2>/dev/null || true

echo "[$(date '+%F %T')] backup done  size=$(du -sh "$DUMP_FILE" | cut -f1)"
ls -lh "$OUT_DIR"
