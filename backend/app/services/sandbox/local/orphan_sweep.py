"""Startup orphan sweep for per-Run docker sandbox containers and staging dirs.

Lifecycle ownership: ``DockerSessionBackend`` closes and removes a Run's
sandbox container via ``close_run()``, called from ``command_worker``'s
``finally`` block.  That teardown is process-local: ``_run_sessions`` is an
in-memory registry, so a backend restart (deploy, crash) orphans every
container that was alive at the time — nothing in the new process knows their
run id, ``sleep infinity`` keeps them running forever, and ``auto_remove``
never fires.  Observed 2026-08-30/31: two exec containers survived 33-34h
after their Runs went terminal, plus 10 stale staging dirs.

This module closes that gap: at startup (worker role, before the runtime
worker context is entered) the backend lists containers labelled
``clawith.sandbox=execute-code``, asks PG which Runs are still active — the
same 5-minute ``updated_at`` window as ``scripts/check-inflight-runs.sh`` —
and removes containers and staging dirs whose Run is not active.

Safety rails:
- A container whose Run is still active is **kept**: in a multi-replica
  deployment it may belong to another replica's live session, and in a
  single-instance restart it is re-driven by the durable runtime (which
  creates a fresh session lazily).  Known ceiling: an old container of a Run
  that stays active across a restart is swept only once the Run goes stale
  and the process restarts again — correctness over perfect cleanup.
- Containers registered in the current process's ``_run_sessions`` are never
  touched (guards against a future periodic call while sessions are live).
- Staging dirs are matched by their ``{run_id[:8]}-{hex8}`` name prefix; a
  prefix that matches **any** active Run id keeps the dir (conservative —
  prefix collisions and just-created dirs of re-driven Runs survive).
"""

from __future__ import annotations

import asyncio
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from docker import errors
from loguru import logger

# Same in-flight window as scripts/check-inflight-runs.sh — one authority for
# "Run is being driven by a live backend process".
ACTIVE_WINDOW_SECONDS = 300

_SANDBOX_LABEL = {"label": "clawith.sandbox=execute-code"}
_RUN_ID_LABEL = "clawith.run_id"


async def _fetch_active_run_ids(now: datetime) -> set[str]:
    """One bounded query for active Run ids (no per-container queries)."""
    from sqlalchemy import select

    from app.database import async_session
    from app.models.agent_run import AgentRun

    cutoff = now - timedelta(seconds=ACTIVE_WINDOW_SECONDS)
    async with async_session() as session:
        result = await session.execute(select(AgentRun.id).where(AgentRun.updated_at > cutoff))
        return {str(row[0]) for row in result}


async def sweep_orphan_sandboxes(
    *,
    active_run_ids: set[str] | None = None,
    staging_parent: Path | None = None,
    client: Any = None,
    now: datetime | None = None,
) -> dict:
    """Remove sandbox containers and staging dirs whose Run is not active.

    Never raises for docker-side errors it can tolerate (missing daemon,
    per-container NotFound): the caller logs and startup proceeds.  Raises
    only on errors the caller should see (e.g. a broken PG connection), so
    each failure mode stays attributable.
    """
    from app.services.sandbox.docker_client import get_docker_client
    from app.services.sandbox.local.docker_backend import (
        _STAGING_PARENT,
        DockerSessionBackend,
    )

    current_time = now or datetime.now(timezone.utc)
    if active_run_ids is None:
        active_run_ids = await _fetch_active_run_ids(current_time)
    active_prefixes = {rid[:8] for rid in active_run_ids}

    client = client or get_docker_client()
    try:
        containers = await _to_thread_list(client)
    except errors.DockerException as exc:
        logger.warning(f"[OrphanSweep] docker list failed: {exc}")
        return {"removed_containers": [], "kept_containers": [], "removed_staging": []}

    local_live = set(DockerSessionBackend._run_sessions)
    removed_containers: list[str] = []
    kept_containers: list[str] = []
    for container in containers:
        name = getattr(container, "name", None) or getattr(container, "id", "<unknown>")
        run_id = (container.labels or {}).get(_RUN_ID_LABEL)
        if run_id is None:
            # Every container this backend creates carries the label; an
            # unlabelled one cannot be attributed to any live Run.
            if _remove(container, name):
                removed_containers.append(name)
        elif run_id in local_live or run_id in active_run_ids:
            kept_containers.append(name)
        else:
            if _remove(container, name):
                removed_containers.append(name)

    removed_staging: list[str] = []
    parent = staging_parent if staging_parent is not None else _STAGING_PARENT
    if parent.is_dir():
        for entry in parent.iterdir():
            if not entry.is_dir():
                continue
            prefix = entry.name.split("-", 1)[0]
            if len(prefix) != 8:
                # Not shaped like {run_id[:8]}-{hex8}: left alone.
                continue
            if prefix in active_prefixes:
                continue
            try:
                shutil.rmtree(entry, ignore_errors=True)
                removed_staging.append(str(entry))
            except OSError as exc:
                logger.warning(f"[OrphanSweep] staging removal failed for {entry}: {exc}")

    return {
        "removed_containers": removed_containers,
        "kept_containers": kept_containers,
        "removed_staging": removed_staging,
    }


async def _to_thread_list(client: Any) -> list[Any]:
    return await asyncio.to_thread(client.containers.list, filters=_SANDBOX_LABEL)


def _remove(container: Any, name: str) -> bool:
    """Best-effort force-remove of one container; True when it is gone."""
    try:
        container.remove(force=True)
        logger.info(f"[OrphanSweep] removed orphan sandbox container {name}")
        return True
    except (errors.NotFound, errors.APIError) as exc:
        logger.warning(f"[OrphanSweep] failed to remove {name}: {exc}")
        return False
