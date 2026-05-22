#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

echo "==> Checking Docker daemon..."
for i in $(seq 1 60); do
  if docker info >/dev/null 2>&1; then
    break
  fi
  if [[ $i -eq 1 ]]; then
    echo "Docker is not running. Please start Docker Desktop, then press Enter..."
    open -a Docker 2>/dev/null || open "/Applications/Docker.app" 2>/dev/null || true
  fi
  if [[ $i -eq 60 ]]; then
    echo "ERROR: Docker daemon not available after 120s."
    exit 1
  fi
  sleep 2
done

if [[ ! -f .env ]]; then
  echo "==> Creating .env from .env.example"
  cp .env.example .env
fi

if [[ ! -f ss-nodes.json ]]; then
  echo "[]" > ss-nodes.json
fi

export DOCKER_BUILDKIT=1
export COMPOSE_DOCKER_CLI_BUILD=1

echo "==> Building and starting services..."
docker compose up -d --build

echo "==> Waiting for postgres/redis..."
docker compose ps

echo "==> Running database migrations..."
BACKEND="$(docker compose ps -q backend)"
if [[ -n "$BACKEND" ]]; then
  docker exec "$BACKEND" alembic upgrade heads || true
fi

echo ""
echo "Deployment complete."
PORT="${FRONTEND_PORT:-3008}"
echo "  本机:     http://localhost:${PORT}"
echo "  局域网 (按访问方所在网段选一个):"
for ip in $(ifconfig | awk '/inet / && $2 !~ /^127\./ {print $2}'); do
  echo "            http://${ip}:${PORT}"
done
echo "  日志:     docker compose logs -f"
echo "  状态:     docker compose ps"
