#!/usr/bin/env bash
set -euo pipefail

# 说明：
# 这个脚本用于从 .dump 备份恢复 PostgreSQL 数据库。
# 设计原则：
# - 默认保守：不会主动删库、不会自动清空 schema（除非显式加参数）
# - 先校验再恢复：先检查 dump catalog，再执行真正恢复
# - 提供 dry-run：可在不改动数据的前提下验证恢复计划

usage() {
  cat <<'EOF'
Usage:
  scripts/restore_from_backup.sh --file backups/xxx.dump [options]

Options:
  --file <path>         Required. pg_dump custom format file (.dump)
  --host <host>         Default: 127.0.0.1
  --port <port>         Default: 5432
  --user <user>         Default: clawith
  --db <name>           Default: clawith
  --create-db           若数据库不存在则自动创建（连接 postgres 库执行）
  --reset-public        先 DROP/CREATE public schema（危险操作，请谨慎）
  --dry-run             只做校验与计划输出，不实际恢复
  -h, --help            Show this help
EOF
}

DB_HOST="127.0.0.1"
DB_PORT="5432"
DB_USER="clawith"
DB_NAME="clawith"
DUMP_FILE=""
CREATE_DB="0"
RESET_PUBLIC="0"
DRY_RUN="0"

# 参数解析：支持显式选项，不使用交互式输入，便于自动化执行（CI/运维脚本）。
while [[ $# -gt 0 ]]; do
  case "$1" in
    --file) DUMP_FILE="${2:-}"; shift 2 ;;
    --host) DB_HOST="${2:-}"; shift 2 ;;
    --port) DB_PORT="${2:-}"; shift 2 ;;
    --user) DB_USER="${2:-}"; shift 2 ;;
    --db) DB_NAME="${2:-}"; shift 2 ;;
    --create-db) CREATE_DB="1"; shift ;;
    --reset-public) RESET_PUBLIC="1"; shift ;;
    --dry-run) DRY_RUN="1"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1"; usage; exit 1 ;;
  esac
done

# 必须提供备份文件路径。
if [[ -z "$DUMP_FILE" ]]; then
  echo "Error: --file is required"
  usage
  exit 1
fi

# 备份文件存在性检查，防止误写路径导致空恢复。
if [[ ! -f "$DUMP_FILE" ]]; then
  echo "Error: dump file not found: $DUMP_FILE"
  exit 1
fi

# 第一步：校验备份 catalog（不改库）。
echo "[restore] validating dump catalog"
pg_restore -l "$DUMP_FILE" >/tmp/clawith_restore_catalog.txt
if rg -n "agents|chat_sessions|chat_messages|users|tenants" /tmp/clawith_restore_catalog.txt >/dev/null; then
  echo "[restore] catalog validation OK"
else
  echo "[restore] WARNING: core tables not found in catalog"
fi

echo "[restore] target: $DB_USER@$DB_HOST:$DB_PORT/$DB_NAME"

# dry-run 模式：到这里直接退出，不产生任何数据库写操作。
if [[ "$DRY_RUN" == "1" ]]; then
  echo "[restore] dry-run mode; no changes applied"
  exit 0
fi

# 可选：自动建库（数据库不存在时）。
if [[ "$CREATE_DB" == "1" ]]; then
  echo "[restore] ensuring database exists: $DB_NAME"
  psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d postgres -v ON_ERROR_STOP=1 \
    -c "SELECT 'CREATE DATABASE \"$DB_NAME\"' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '$DB_NAME')\\gexec"
fi

# 可选：清空 public schema。
# 风险提示：该操作会删除当前库 public schema 下现有对象，请仅在明确要“全量覆盖恢复”时使用。
if [[ "$RESET_PUBLIC" == "1" ]]; then
  echo "[restore] resetting public schema in $DB_NAME"
  psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 \
    -c "DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;"
fi

# 执行恢复：
# --clean --if-exists：恢复前尝试清理同名对象（若存在）
# --no-owner --no-privileges：避免不同环境角色/权限不一致导致失败
echo "[restore] running pg_restore"
pg_restore \
  -h "$DB_HOST" \
  -p "$DB_PORT" \
  -U "$DB_USER" \
  -d "$DB_NAME" \
  --no-owner \
  --no-privileges \
  --clean \
  --if-exists \
  "$DUMP_FILE"

echo "[restore] done"
