#!/bin/bash
# Docker entrypoint: optionally run DB migrations, then start the app.

set -e

PROCESS_ROLE="${PROCESS_ROLE:-all}"
ALLOW_MIGRATION_FAILURE="${ALLOW_MIGRATION_FAILURE:-false}"
START_COMMAND="${START_COMMAND:-uvicorn app.main:app --host 0.0.0.0 --port 8000}"

role_contains() {
    case ",${PROCESS_ROLE}," in
        *,all,*|*,"$1",*) return 0 ;;
        *) return 1 ;;
    esac
}

# --- Permission fixing and privilege dropping ---
if [ "$(id -u)" = '0' ]; then
    echo "[entrypoint] Detected root user, checking permissions..."
    TARGET_DIR="${AGENT_DATA_DIR:-/data/agents}"
    if [ -d "${TARGET_DIR}" ]; then
        CURRENT_OWNER=$(stat -c '%U:%G' "${TARGET_DIR}" 2>/dev/null || echo "")
        if [ "${CURRENT_OWNER}" != "clawith:clawith" ]; then
            echo "[entrypoint] Directory ${TARGET_DIR} owner is '${CURRENT_OWNER}', fixing permissions..."
            chown -R clawith:clawith "${TARGET_DIR}"
        else
            echo "[entrypoint] Directory ${TARGET_DIR} is already owned by clawith:clawith, skipping chown."
        fi
    fi

    # 允许 clawith 用户访问宿主 Docker socket（/var/run/docker.sock）
    # socket 由宿主挂载，GID 可能不是 docker 组→动态获取
    if [ -S /var/run/docker.sock ]; then
        DOCKER_GID=$(stat -c '%g' /var/run/docker.sock 2>/dev/null || echo "")
        if [ -n "${DOCKER_GID}" ] && [ "${DOCKER_GID}" != "0" ]; then
            # 非 root 组: 创建 docker-sock 组并加入 clawith
            groupadd --gid "${DOCKER_GID}" docker-sock 2>/dev/null || true
            usermod -a -G "${DOCKER_GID}" clawith
            echo "[entrypoint] Docker socket GID=${DOCKER_GID}, added clawith to group"
        elif [ "${DOCKER_GID}" = "0" ]; then
            # macOS Docker Desktop: socket 属于 root:root
            # gosu 保留附加组, 这里把 clawith 加入 gid 0(root) 组
            usermod -a -G 0 clawith
            echo "[entrypoint] Docker socket owned by root, added clawith to root group"
        fi
    fi

    echo "[entrypoint] Dropping privileges to 'clawith' and re-executing..."
    exec gosu clawith /bin/bash "$0" "$@"
fi
# -------------------------------------------------------

if [ -z "${INSTANCE_ID:-}" ]; then
    SAFE_PROCESS_ROLE="${PROCESS_ROLE//,/-}"
    export INSTANCE_ID="${SAFE_PROCESS_ROLE}-$(hostname)"
fi
echo "[entrypoint] INSTANCE_ID=${INSTANCE_ID}"

if role_contains "bootstrap"; then
    echo "[entrypoint] Step 1: Running alembic migrations for PROCESS_ROLE=${PROCESS_ROLE}..."
    set +e
    ALEMBIC_OUTPUT=$(alembic upgrade head 2>&1)
    ALEMBIC_EXIT=$?
    set -e

    if [ $ALEMBIC_EXIT -ne 0 ]; then
        echo ""
        echo "========================================================================"
        echo "[entrypoint] ERROR: Alembic migration FAILED (exit code $ALEMBIC_EXIT)"
        echo "========================================================================"
        echo ""
        echo "$ALEMBIC_OUTPUT"
        echo ""
        if [ "$ALLOW_MIGRATION_FAILURE" = "true" ]; then
            echo "[entrypoint] Continuing because ALLOW_MIGRATION_FAILURE=true"
        else
            exit $ALEMBIC_EXIT
        fi
    else
        echo "[entrypoint] Alembic migrations completed successfully."
    fi
else
    echo "[entrypoint] Step 1: Skipping alembic for PROCESS_ROLE=${PROCESS_ROLE}"
fi

echo "[entrypoint] Step 2: Starting uvicorn..."
exec /bin/bash -lc "$START_COMMAND"
