#!/bin/bash
# ============================================================
# Clawith 部署脚本 — 从干净 worktree 部署 backend（可选 frontend）
#
# 用法:
#   scripts/deploy.sh                 # 部署 backend（默认，构建镜像）
#   scripts/deploy.sh --frontend      # 同时重建 frontend 容器
#   scripts/deploy.sh --no-build      # 跳过镜像构建（代码未变时省 ~3 分钟）
#   scripts/deploy.sh --commit <ref>  # 部署指定 commit（默认 HEAD）
#   scripts/deploy.sh --no-wait       # 部署锁被占用时立即失败（默认排队 600s）
#   scripts/deploy.sh --strict        # 存在未部署提交时中止（默认仅提示）
#
# 流程: 部署锁 → 预检 → tip 对比 → worktree → .env 校验 → 回滚标签 → build → up → 验证
#
# 红线（2026-08-26 事故教训固化的）:
#   1. worktree 目录是运行容器的 bind-mount 源，绝不删除 /tmp/clawith-deploy-*
#   2. up 前必须确认 .env 存在且 SECRET_KEY/JWT_SECRET_KEY 非占位符
#      （占位符会让 api_key_encrypted 解出垃圾 → 全模型 401）
#   3. 回滚标签必须在 build/up 之前立刻打——运行中镜像随时可能被并行会话 prune
#   4. 挂载源用绝对路径（CLAWITH_SS_NODES_JSON / CLAWITH_NGINX_TEMPLATE），
#      脱离 worktree 存活依赖
#   5. 部署与回滚一律经全局部署锁串行化（ADR 0003）；提交前
#      `git diff --cached --stat` 复核、只用 pathspec 提交本任务文件——
#      共享 index 的提交窗口竞态无法机制化，只能靠这条协议。
# ============================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_PROJECT="clawith-agent"
BACKEND_PORT="${BACKEND_PORT:-8008}"
FRONTEND_PORT="${FRONTEND_PORT:-3008}"
COMMIT="HEAD"
BUILD=1
WITH_FRONTEND=0
NO_WAIT=0
STRICT=0

usage() {
    # 打印两个 "=====" 围栏之间的头注释，避免行号与注释行数耦合。
    awk '/^# =+$/{n++; next} n==1{print substr($0,3)} n==2{exit}' "$0"
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --frontend) WITH_FRONTEND=1; shift ;;
        --no-build) BUILD=0; shift ;;
        --no-wait) NO_WAIT=1; shift ;;
        --strict) STRICT=1; shift ;;
        --commit) COMMIT="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "未知参数: $1"; usage; exit 1 ;;
    esac
done

cd "$REPO_ROOT"

# ── 0.5) 部署锁（ADR 0003 多会话部署避让）────────────────────
# 全局一把 fcntl 内核锁串行化所有部署/回滚；持有者进程死亡自动释放。
# CLAWITH_DEPLOY_LOCKED 标记防重入；锁覆盖整段脚本（含回滚标签与验证）。
PY="python3"
[ -x "$REPO_ROOT/backend/.venv/bin/python" ] && PY="$REPO_ROOT/backend/.venv/bin/python"
STATE_DIR="$REPO_ROOT/.clawith-deploy"
LOCK_TIMEOUT="${CLAWITH_DEPLOY_LOCK_TIMEOUT:-600}"
if [ "$NO_WAIT" = 1 ]; then LOCK_TIMEOUT=0; fi
if [ "${CLAWITH_DEPLOY_LOCKED:-}" != "1" ]; then
    SCOPE="backend"
    if [ "$WITH_FRONTEND" = 1 ]; then SCOPE="backend+frontend"; fi
    # 用绝对路径重入（$0 可能是相对路径，cd 后已失效）。
    CLAWITH_DEPLOY_LOCKED=1 exec "$PY" "$REPO_ROOT/scripts/deploy_guard.py" lock \
        "$STATE_DIR" "$LOCK_TIMEOUT" "$(git rev-parse --short "$COMMIT")" "$SCOPE" -- \
        "$REPO_ROOT/scripts/deploy.sh" "$@"
fi

# ── 0) 预检 ────────────────────────────────────────────────
UNCOMMITTED=$(git status --porcelain | wc -l | tr -d ' ')
if [ "$UNCOMMITTED" -gt 0 ]; then
    echo "⚠️  工作区有 ${UNCOMMITTED} 个未提交改动（可能属并行会话）——只部署已提交内容，绝不带入工作区脏文件"
fi

# ── 1) 提交解析 + worktree ─────────────────────────────────
SHORT=$(git rev-parse --short "$COMMIT")
WORKTREE="/tmp/clawith-deploy-${SHORT}"

# ── 1.5) tip 对比（ADR 0003）：展示将随本次部署上线的提交 ────
set +e
"$PY" "$REPO_ROOT/scripts/deploy_guard.py" check "$STATE_DIR" "$SHORT"
TIP_RC=$?
set -e
if [ "$TIP_RC" -ne 0 ] && [ "$STRICT" = 1 ]; then
    echo "❌ --strict：存在未部署提交，中止（去掉 --strict 可继续）" >&2
    exit 1
fi

if [ -d "$WORKTREE" ]; then
    echo "→ worktree 已存在: $WORKTREE"
else
    echo "→ 创建 worktree: $WORKTREE"
    git worktree add "$WORKTREE" "$SHORT"
fi
cd "$WORKTREE"

# ── 2) .env 准备与校验（红线 2）────────────────────────────
if [ ! -f "$WORKTREE/.env" ]; then
    cp "$REPO_ROOT/.env" "$WORKTREE/.env"
    echo "→ 已从仓库根复制 .env"
fi
if grep -qE '^SECRET_KEY=change-me-in-production[[:space:]]*$' "$WORKTREE/.env"; then
    echo "❌ SECRET_KEY 仍是占位符 'change-me-in-production'，拒绝部署" >&2
    exit 1
fi
if grep -qE '^JWT_SECRET_KEY=change-me-jwt-secret[[:space:]]*$' "$WORKTREE/.env"; then
    echo "❌ JWT_SECRET_KEY 仍是占位符 'change-me-jwt-secret'，拒绝部署" >&2
    exit 1
fi
# 兼容默认相对路径：worktree 内 ss-nodes.json 符号链接到仓库根
if [ ! -e "$WORKTREE/ss-nodes.json" ]; then
    ln -s "$REPO_ROOT/ss-nodes.json" "$WORKTREE/ss-nodes.json"
fi

# ── 3) 稳定挂载源 ──────────────────────────────────────────
export CLAWITH_SS_NODES_JSON="$REPO_ROOT/ss-nodes.json"
export CLAWITH_NGINX_TEMPLATE="$REPO_ROOT/frontend/nginx.conf.template"

# ── 4) 回滚标签（红线 3：build/up 之前立刻打）──────────────
OLD_IMG=$(docker inspect "${COMPOSE_PROJECT}-backend-1" --format '{{.Image}}' 2>/dev/null || true)
if [ -n "$OLD_IMG" ]; then
    ROLLBACK_TAG="clawith-agent-backend:pre-${SHORT}-${OLD_IMG:7:12}"
    if docker tag "$OLD_IMG" "$ROLLBACK_TAG" 2>/dev/null; then
        echo "→ 回滚标签: ${ROLLBACK_TAG}"
    else
        echo "⚠️  无法给当前运行镜像打回滚标签（镜像可能已被并行会话清理）——回滚=从旧 worktree 重建"
    fi
else
    echo "⚠️  未找到运行中的 backend 容器，跳过回滚标签"
fi

# ── 5) 构建（默认）——阿里云 pip 源为必填 build-arg ─────────
# 阿里云 mirrors.aliyun.com：清华 tuna 对 aarch64 wheel 下载会卡 ~20min，
# 阿里云秒过（2026-08-26 构建插曲教训）。
if [ "$BUILD" = 1 ]; then
    export CLAWITH_PIP_INDEX_URL="${CLAWITH_PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}"
    export CLAWITH_PIP_TRUSTED_HOST="${CLAWITH_PIP_TRUSTED_HOST:-mirrors.aliyun.com}"
    echo "→ 构建 backend 镜像..."
    docker compose --env-file "$WORKTREE/.env" -p "$COMPOSE_PROJECT" -f docker-compose.yml build backend
fi

# ── 6) up ──────────────────────────────────────────────────
echo "→ 重建 backend 容器..."
docker compose --env-file "$WORKTREE/.env" -p "$COMPOSE_PROJECT" -f docker-compose.yml up -d --no-deps backend
if [ "$WITH_FRONTEND" = 1 ]; then
    echo "→ 重建 frontend 容器..."
    docker compose --env-file "$WORKTREE/.env" -p "$COMPOSE_PROJECT" -f docker-compose.yml up -d --no-deps frontend
fi

# ── 7) 验证 ────────────────────────────────────────────────
sleep 12
HEALTH=$(curl -s -m 3 "http://localhost:${BACKEND_PORT}/api/health" || true)
case "$HEALTH" in
    *'"status":"ok"'*) echo "✅ /api/health 200" ;;
    *) echo "❌ /api/health 异常: ${HEALTH}" >&2; exit 1 ;;
esac
docker exec "${COMPOSE_PROJECT}-backend-1" sh -c \
    '[ "$SECRET_KEY" = "change-me-in-production" ] && echo "❌ SECRET_KEY 是占位符" || echo "✅ SECRET_KEY 非占位符"'
docker exec "${COMPOSE_PROJECT}-backend-1" grep -q 'TERM_FLAG' /app/entrypoint.sh \
    && echo "✅ entrypoint 守卫在镜像内" || echo "❌ entrypoint 守卫缺失" >&2
docker logs --tail 200 "${COMPOSE_PROJECT}-backend-1" 2>&1 | grep -q "Alembic migrations completed" \
    && echo "✅ alembic 迁移成功" || echo "⚠️  未在日志中找到 alembic 成功标记（PROCESS_ROLE 非 bootstrap 时正常）"
if [ "$WITH_FRONTEND" = 1 ]; then
    curl -s -o /dev/null -w "✅ frontend ${FRONTEND_PORT}=%{http_code}\n" -m 3 "http://localhost:${FRONTEND_PORT}"
fi

# ── 8) 结果标记：deploy_guard 在退出时读入注册表（ADR 0003）──
NEW_IMG=$(docker inspect "${COMPOSE_PROJECT}-backend-1" --format '{{.Image}}' 2>/dev/null || true)
if [ -n "$NEW_IMG" ]; then
    printf '{"image_sha":"%s"}\n' "$NEW_IMG" > "$STATE_DIR/pending-result.json"
fi

echo ""
echo "✅ 部署完成。当前部署 worktree=${WORKTREE}（红线：勿删）"
