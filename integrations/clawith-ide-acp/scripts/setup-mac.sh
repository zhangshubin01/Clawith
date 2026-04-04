#!/usr/bin/env bash
# 在 macOS 上创建虚拟环境并安装依赖。用法：在仓库中执行 bash scripts/setup-mac.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if ! command -v python3 >/dev/null 2>&1; then
  echo "请先安装 Python 3.10+（例如 brew install python）" >&2
  exit 1
fi

python3 -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/pip" install -U pip
"$ROOT/.venv/bin/pip" install -r "$ROOT/requirements.txt"

if [[ ! -f "$ROOT/.env" ]]; then
  cp "$ROOT/env.example" "$ROOT/.env"
  echo "已创建 $ROOT/.env ，请编辑 CLAWITH_URL / CLAWITH_API_KEY 等变量。"
fi

echo "完成。运行: bash $ROOT/scripts/run-mac.sh"
echo "JetBrains: 将 jetbrains/acp.json.example 中的路径改为本机绝对路径后合并到 ~/.jetbrains/acp.json"
