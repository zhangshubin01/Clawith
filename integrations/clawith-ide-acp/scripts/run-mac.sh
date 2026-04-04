#!/usr/bin/env bash
# 加载 .env 并启动 ACP 瘦客户端（供 JetBrains 子进程或本地调试）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="$ROOT/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "未找到虚拟环境，请先运行: bash scripts/setup-mac.sh" >&2
  exit 1
fi

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ROOT/.env"
  set +a
fi

exec "$PY" "$ROOT/server.py"
