#!/bin/bash
# Docker entrypoint: optionally run DB migrations, then start the app.

set -e

PROCESS_ROLE="${PROCESS_ROLE:-all}"
ALLOW_MIGRATION_FAILURE="${ALLOW_MIGRATION_FAILURE:-false}"
START_COMMAND="${START_COMMAND:-uvicorn app.main:app --host 0.0.0.0 --port 8000 --ws-ping-interval 15 --ws-ping-timeout 10}"

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
        CHECKPOINT_OUTPUT=$(python -m app.scripts.setup_langgraph_checkpoints 2>&1)
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
exec /bin/bash -lc "$START_COMMAND"
