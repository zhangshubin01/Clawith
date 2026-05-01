#!/usr/bin/env bash
set -euo pipefail

# 说明：
# 这个脚本用于“升级/合并代码前”的数据库标准备份。
# 目标是把高风险操作前的数据状态固化下来，避免迁移失败或误操作导致无法回滚。
#
# 用法：
#   1) 使用默认连接参数（127.0.0.1:5432 / clawith / clawith）：
#      scripts/backup_before_upgrade.sh
#   2) 临时覆盖连接参数：
#      DB_HOST=127.0.0.1 DB_PORT=5432 DB_USER=clawith DB_NAME=clawith scripts/backup_before_upgrade.sh
#   3) 指定备份目录（默认是项目根目录 backups/）：
#      BACKUP_DIR=/path/to/backups scripts/backup_before_upgrade.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-$ROOT_DIR/backups}"

DB_HOST="${DB_HOST:-127.0.0.1}"
DB_PORT="${DB_PORT:-5432}"
DB_USER="${DB_USER:-clawith}"
DB_NAME="${DB_NAME:-clawith}"

# 确保备份目录存在；不存在则自动创建。
mkdir -p "$BACKUP_DIR"

# 使用时间戳命名，避免覆盖旧备份。
STAMP="$(date +%F_%H%M%S)"
OUT_FILE="$BACKUP_DIR/${DB_NAME}_${STAMP}.dump"
META_FILE="$BACKUP_DIR/${DB_NAME}_${STAMP}.meta.txt"

# 1) 生成 PostgreSQL 自定义格式备份（-Fc）
# 优点：体积更小，恢复时可选择性导入对象，常用于生产级备份。
echo "[backup] starting pg_dump -> $OUT_FILE"
pg_dump -Fc -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -f "$OUT_FILE"

# 2) 生成备份目录清单（catalog），用于快速校验备份内容是否完整。
echo "[backup] validating dump catalog"
pg_restore -l "$OUT_FILE" > "$META_FILE"

# 3) 基础健康检查：确认关键业务表至少出现在 catalog 里。
# 注意：这是“快速有效性校验”，不是逐行数据完整性校验。
if rg -n "agents|chat_sessions|chat_messages|users|tenants" "$META_FILE" >/dev/null; then
  echo "[backup] validation OK: core tables found in dump catalog"
else
  echo "[backup] WARNING: core tables not found in dump catalog, please inspect:"
  echo "         $META_FILE"
fi

echo "[backup] done"
echo "  dump: $OUT_FILE"
echo "  meta: $META_FILE"
echo
echo "Next suggested steps (recommended):"
echo "  1) git pull / merge"
echo "  2) alembic upgrade head"
echo "  3) restart services"
