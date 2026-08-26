#!/bin/bash
# Docker entrypoint: optionally run DB migrations, then start the app.

set -e

PROCESS_ROLE="${PROCESS_ROLE:-all}"
ALLOW_MIGRATION_FAILURE="${ALLOW_MIGRATION_FAILURE:-false}"
APP_WORKERS="${APP_WORKERS:-1}"
DEFAULT_UVICORN_WORKERS="1"
case ",${PROCESS_ROLE}," in
    *,api,*|*,all,*)
        DEFAULT_UVICORN_WORKERS="${APP_WORKERS}"
        ;;
esac
START_COMMAND="${START_COMMAND:-uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${DEFAULT_UVICORN_WORKERS}}"

role_contains() {
    case ",${PROCESS_ROLE}," in
        *,all,*|*,"$1",*) return 0 ;;
        *) return 1 ;;
    esac
}

# --- Fail-fast on placeholder secrets (2026-08-26 SECRET_KEY 事故) ---
# Deploying without a real .env makes SECRET_KEY fall back to the compose
# placeholder; api_key_encrypted then "decrypts" to garbage and every model
# call fails with HTTP 401 "invalid key" (the key itself is fine). Refuse to
# boot with an actionable message instead of failing 10 minutes later.
if [ -z "${SECRET_KEY:-}" ] || [ "${SECRET_KEY}" = "change-me-in-production" ]; then
    echo "[entrypoint] FATAL: SECRET_KEY is missing or the placeholder 'change-me-in-production'." >&2
    echo "[entrypoint] The deploy directory has no .env (or it was not loaded)." >&2
    echo "[entrypoint] Copy the repo-root .env into the deploy directory, then re-run compose." >&2
    exit 1
fi
if [ -z "${JWT_SECRET_KEY:-}" ] || [ "${JWT_SECRET_KEY}" = "change-me-jwt-secret" ]; then
    echo "[entrypoint] FATAL: JWT_SECRET_KEY is missing or the placeholder 'change-me-jwt-secret'." >&2
    echo "[entrypoint] Copy the repo-root .env into the deploy directory, then re-run compose." >&2
    exit 1
fi

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

    # Grant clawith access to the Docker socket via its owning group.
    # docker-compose mounts /var/run/docker.sock and adds its group via
    # group_add, but gosu strips supplementary groups the target user is
    # not a member of in /etc/group. We detect the socket's actual GID,
    # ensure a matching group exists, and add clawith to it.
    if [ -S /var/run/docker.sock ]; then
        SOCK_GID=$(stat -c '%g' /var/run/docker.sock 2>/dev/null || echo "")
        if [ -z "$SOCK_GID" ]; then
            echo "[entrypoint] WARNING: Cannot determine docker.sock GID — skipping Docker access setup"
        elif [ "$SOCK_GID" = "0" ]; then
            SOCK_GROUP="root"
        else
            # Non-root group — create if missing and add clawith
            if ! getent group "$SOCK_GID" >/dev/null 2>&1; then
                echo "[entrypoint] Creating group gid=$SOCK_GID for Docker socket access..."
                groupadd -g "$SOCK_GID" docker_sock_group
            fi
            SOCK_GROUP=$(getent group "$SOCK_GID" | cut -d: -f1)
        fi
        if [ -n "${SOCK_GROUP:-}" ] && ! id -nG clawith 2>/dev/null | grep -qwF "$SOCK_GROUP"; then
            echo "[entrypoint] Adding clawith to '$SOCK_GROUP' group for Docker socket access..."
            usermod -a -G "$SOCK_GROUP" clawith
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

        echo "[entrypoint] Step 2: Installing LangGraph checkpoint tables..."
        set +e
        # 超时保护: 该步骤偶发挂起(详见 docs/technical-plans 启动问题排查记录),
        # SIGABRT + faulthandler 让超时现场以 Python 堆栈形式留在 CHECKPOINT_OUTPUT 里。
        CHECKPOINT_OUTPUT=$(timeout -s ABRT ${CHECKPOINT_SETUP_TIMEOUT_SECONDS:-120} python -X faulthandler -m app.scripts.setup_langgraph_checkpoints 2>&1)
        CHECKPOINT_EXIT=$?
        set -e

        if [ $CHECKPOINT_EXIT -ne 0 ]; then
            echo ""
            echo "========================================================================"
            echo "[entrypoint] ERROR: LangGraph checkpoint setup FAILED (exit code $CHECKPOINT_EXIT)"
            echo "========================================================================"
            echo ""
            echo "$CHECKPOINT_OUTPUT"
            echo ""
            if [ "$ALLOW_MIGRATION_FAILURE" = "true" ]; then
                echo "[entrypoint] Continuing because ALLOW_MIGRATION_FAILURE=true"
            else
                exit $CHECKPOINT_EXIT
            fi
        else
            echo "[entrypoint] LangGraph checkpoint tables are ready."
        fi
    fi
else
    echo "[entrypoint] Step 1: Skipping alembic for PROCESS_ROLE=${PROCESS_ROLE}"
    echo "[entrypoint] Step 2: Skipping LangGraph checkpoint setup for PROCESS_ROLE=${PROCESS_ROLE}"
fi

echo "[entrypoint] Step 3: Starting uvicorn..."

# --- Clean shutdown for restart-policy self-heal (2026-08-26) ---
# After an OrbStack/docker daemon restart, containers that shut down with a
# non-zero exit code (e.g. 127: redis is already gone and lifespan teardown
# fails) are NOT auto-recovered by restart: unless-stopped, while exit-0
# containers are. Trap SIGTERM/SIGINT, forward it to the app, and exit 0
# whenever the stop came from a signal, so backend/frontend come back by
# themselves after daemon restarts exactly like postgres/redis do.
TERM_FLAG=0
_term_handler() {
    TERM_FLAG=1
    echo "[entrypoint] Received SIGTERM/SIGINT, forwarding to app (pid ${APP_PID:-unknown})..."
    kill -TERM "${APP_PID}" 2>/dev/null || true
}
trap _term_handler TERM INT

/bin/bash -lc "$START_COMMAND" &
APP_PID=$!
set +e
wait "${APP_PID}"
APP_EXIT=$?
set -e
if [ "${TERM_FLAG}" = "1" ]; then
    echo "[entrypoint] App exited after signal; exiting 0 so restart: unless-stopped can recover us."
    exit 0
fi
echo "[entrypoint] App exited with code ${APP_EXIT}."
exit "${APP_EXIT}"
