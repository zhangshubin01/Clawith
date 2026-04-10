#!/usr/bin/env bash
# 打企业分发 zip：不含 .venv / .env，便于内网上传。
# 用法：在 integrations/clawith-ide-acp 目录执行  bash scripts/package-release.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VERSION="$(tr -d '[:space:]' < VERSION)"
NAME="clawith-ide-acp-${VERSION}"
OUT_DIR="$ROOT/releases"
STAGE="$(mktemp -d)"

cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT

mkdir -p "$OUT_DIR"
DEST="$STAGE/$NAME"
mkdir -p "$DEST"

rsync -a \
  --exclude '.venv' \
  --exclude '.env' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '.pytest_cache' \
  --exclude 'releases' \
  "$ROOT/" "$DEST/"

ZIP_PATH="$OUT_DIR/${NAME}.zip"
( cd "$STAGE" && zip -r -q "$ZIP_PATH" "$NAME" )

echo "Built: $ZIP_PATH"
echo "SHA256 (optional):"
shasum -a 256 "$ZIP_PATH" 2>/dev/null || sha256sum "$ZIP_PATH" 2>/dev/null || true
