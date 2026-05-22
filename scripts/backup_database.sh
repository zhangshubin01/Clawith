#!/usr/bin/env bash
# 备份 Clawith PostgreSQL（优先本机 5432，否则用 Docker 容器内 pg_dump）
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-$ROOT_DIR/backups}"
STAMP="$(date +%F_%H%M%S)"
OUT_DIR="$BACKUP_DIR/snapshot_${STAMP}"
mkdir -p "$OUT_DIR"

DB_HOST="${DB_HOST:-127.0.0.1}"
DB_PORT="${DB_PORT:-5432}"
DB_USER="${DB_USER:-clawith}"
DB_NAME="${DB_NAME:-clawith}"
PGPASSWORD="${PGPASSWORD:-clawith}"
export PGPASSWORD

DUMP_FILE="$OUT_DIR/${DB_NAME}.dump"
META_FILE="$OUT_DIR/${DB_NAME}.meta.txt"
AGENT_TAR="$OUT_DIR/agents_data.tar.gz"
DOCKER_DUMP="$OUT_DIR/docker_${DB_NAME}.dump"

pg_dump_cmd() {
  local host="$1" port="$2" out="$3"
  if command -v pg_dump >/dev/null 2>&1; then
    pg_dump -Fc -h "$host" -p "$port" -U "$DB_USER" -d "$DB_NAME" -f "$out"
    return 0
  fi
  docker run --rm -e PGPASSWORD="$PGPASSWORD" postgres:15-alpine \
    pg_dump -Fc -h "$host" -p "$port" -U "$DB_USER" -d "$DB_NAME" >"$out"
}

echo "[backup] output: $OUT_DIR"

SOURCE="unknown"
if pg_dump_cmd "$DB_HOST" "$DB_PORT" "$DUMP_FILE" 2>/dev/null; then
  SOURCE="host:${DB_HOST}:${DB_PORT}"
elif pg_dump_cmd host.docker.internal "$DB_PORT" "$DUMP_FILE" 2>/dev/null; then
  SOURCE="host.docker.internal:${DB_PORT}"
else
  echo "[backup] WARN: cannot reach PostgreSQL on host port ${DB_PORT}"
  LATEST="$(ls -t "$BACKUP_DIR"/*.dump 2>/dev/null | head -1 || true)"
  if [[ -n "$LATEST" ]]; then
    cp "$LATEST" "$OUT_DIR/local_pre_docker_legacy.dump"
    cp "$LATEST" "$DUMP_FILE"
    SOURCE="archived:$(basename "$LATEST")"
    echo "[backup] copied latest archive -> local_pre_docker_legacy.dump"
  else
    echo "[backup] ERROR: no running Postgres and no *.dump in $BACKUP_DIR"
    exit 1
  fi
fi

if docker ps --format '{{.Names}}' | grep -q '^clawith-postgres-1$'; then
  echo "[backup] docker postgres snapshot -> $DOCKER_DUMP"
  docker exec -e PGPASSWORD=clawith clawith-postgres-1 \
    pg_dump -Fc -U clawith -d clawith >"$DOCKER_DUMP" || true
fi

if [[ -d "${HOME}/.clawith/data/agents" ]]; then
  echo "[backup] agent files -> $AGENT_TAR"
  tar -czf "$AGENT_TAR" -C "${HOME}/.clawith/data" agents
fi

if command -v pg_restore >/dev/null 2>&1; then
  pg_restore -l "$DUMP_FILE" >"$META_FILE"
elif docker ps --format '{{.Names}}' | grep -q '^clawith-postgres-1$'; then
  docker exec -i clawith-postgres-1 pg_restore -l <"$DUMP_FILE" >"$META_FILE"
fi

cat >"$OUT_DIR/README.txt" <<EOF
Clawith backup snapshot
Created: ${STAMP}
DB source: ${SOURCE}
Files:
  - ${DB_NAME}.dump              (live DB at backup time: host or Docker)
  - local_pre_docker_legacy.dump (copied from newest backups/*.dump if host DB unreachable)
  - ${DB_NAME}.meta.txt
  - agents_data.tar.gz
  - docker_${DB_NAME}.dump       (Docker Postgres snapshot)

Restore into Docker Postgres:
  docker cp ${DB_NAME}.dump clawith-postgres-1:/tmp/restore.dump
  docker exec clawith-postgres-1 psql -U clawith -d clawith -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
  docker exec clawith-postgres-1 pg_restore -U clawith -d clawith --no-owner --no-privileges /tmp/restore.dump
  docker compose restart backend
EOF

echo "[backup] done ($SOURCE)"
echo "  dir: $OUT_DIR"
ls -lh "$OUT_DIR"
