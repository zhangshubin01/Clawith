"""Run-scoped materialized workspace lifecycle for local sandboxes."""

import asyncio
import logging
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass
class TempWorkspaceManifestEntry:
    """Publication manifest entry for one materialized workspace path.

    Lives in this leaf module (rather than agent_tools) so the run-workspace
    refresh seam and the higher-level tool layer share one contract type
    without a reverse dependency.
    """

    rel_path: str
    storage_key: str
    base_version_token: str
    base_hash: str
    size: int


class RunWorkspace(Protocol):
    """Minimum interface required for a run-scoped materialized workspace."""

    root: Path
    manifest: dict[str, TempWorkspaceManifestEntry]

    def cleanup(self) -> None: ...


@dataclass(frozen=True)
class RunWorkspaceIdentity:
    """Configuration that must remain stable throughout one Agent loop."""

    agent_id: str
    tenant_id: str | None
    session_id: str | None
    workspace_mode: str
    materialized_paths: tuple[str, ...]
    publish_paths: tuple[str, ...]


@dataclass
class _RunWorkspaceState:
    identity: RunWorkspaceIdentity
    workspace: RunWorkspace
    lock: asyncio.Lock
    closed: bool = False


_run_workspace_tasks: dict[str, asyncio.Task[_RunWorkspaceState]] = {}


async def _create_state(
    identity: RunWorkspaceIdentity,
    factory: Callable[[], Awaitable[RunWorkspace]],
) -> _RunWorkspaceState:
    return _RunWorkspaceState(
        identity=identity,
        workspace=await factory(),
        lock=asyncio.Lock(),
    )


async def _get_or_create_state(
    run_id: str,
    identity: RunWorkspaceIdentity,
    factory: Callable[[], Awaitable[RunWorkspace]],
) -> _RunWorkspaceState:
    task = _run_workspace_tasks.get(run_id)
    if task is None:
        task = asyncio.create_task(_create_state(identity, factory))
        _run_workspace_tasks[run_id] = task
    try:
        state = await asyncio.shield(task)
    except BaseException:
        if task.done() and _run_workspace_tasks.get(run_id) is task:
            _run_workspace_tasks.pop(run_id, None)
        raise
    if state.identity != identity:
        raise RuntimeError("Agent-loop sandbox workspace identity changed")
    return state


@asynccontextmanager
async def use_run_workspace(
    *,
    run_id: str | None,
    identity: RunWorkspaceIdentity,
    factory: Callable[[], Awaitable[RunWorkspace]],
) -> AsyncIterator[RunWorkspace]:
    """Materialize once per Run, while preserving one-shot legacy behavior."""
    if not run_id:
        workspace = await factory()
        try:
            yield workspace
        finally:
            workspace.cleanup()
        return

    state = await _get_or_create_state(run_id, identity, factory)
    async with state.lock:
        yield state.workspace


async def close_run_workspace(run_id: str) -> None:
    """Discard the materialized workspace owned by one settled Agent loop."""
    task = _run_workspace_tasks.pop(run_id, None)
    if task is None:
        return
    try:
        state = await asyncio.shield(task)
    except (asyncio.CancelledError, Exception):
        return
    async with state.lock:
        state.closed = True
        state.workspace.cleanup()


async def refresh_run_workspace_path(
    run_id: str,
    agent_id: str,
    rel_path: str,
    *,
    data: bytes | None,
    version_token: str = "",
    size: int = 0,
    skip_workspace: RunWorkspace | None = None,
) -> None:
    """Refresh one path of the run-scoped workspace after a direct storage write.

    Keeps the materialized temp files and their publication manifest in sync
    after a direct-write tool (write_file/edit_file/move_file/delete_file) or a
    separate per-call temp workspace flush has changed storage (ADR 0011).
    ``data=None`` marks a deletion (temp file removed, manifest entry dropped).

    Serializes against in-flight sandbox executions through the workspace's
    lock, and skips the refresh when the caller's workspace IS the run
    workspace (the flush of the run workspace itself already owns both views).
    Best-effort by design: failures are logged and the previous conflict
    protection remains the safety net.
    """
    task = _run_workspace_tasks.get(run_id)
    if task is None:
        logger.debug("[RunWorkspaceRefresh] no run workspace: run_id=%s", run_id)
        return
    try:
        state = await asyncio.shield(task)
    except (asyncio.CancelledError, Exception):
        logger.warning("[RunWorkspaceRefresh] state unavailable: run_id=%s", run_id)
        return
    if state.workspace is skip_workspace:
        return
    async with state.lock:
        if state.closed:
            return
        workspace = state.workspace
        root = workspace.root.resolve()
        target = (workspace.root / rel_path).resolve()
        if not target.is_relative_to(root):
            logger.warning(
                "[RunWorkspaceRefresh] traversal rejected: run_id=%s path=%s",
                run_id,
                rel_path,
            )
            return
        manifest = workspace.manifest
        if data is None:
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            prefix = rel_path.rstrip("/") + "/"
            for existing in [
                key for key in manifest if key == rel_path or key.startswith(prefix)
            ]:
                manifest.pop(existing, None)
            logger.info(
                "[RunWorkspaceRefresh] removed path: run_id=%s path=%s",
                run_id,
                rel_path,
            )
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        # Late imports: this module is imported while app.config is still
        # initializing, so importing the storage_runtime package here at module
        # level would re-enter app.config and create a circular import.
        from app.services.storage_runtime.base import content_hash_bytes
        from app.services.storage_runtime.utils import normalize_storage_key

        existing = manifest.get(rel_path)
        manifest[rel_path] = TempWorkspaceManifestEntry(
            rel_path=rel_path,
            storage_key=(
                existing.storage_key
                if existing is not None
                else normalize_storage_key(f"{agent_id}/{rel_path}")
            ),
            base_version_token=version_token,
            base_hash=content_hash_bytes(data),
            size=size,
        )
        logger.info(
            "[RunWorkspaceRefresh] refreshed path: run_id=%s path=%s size=%s",
            run_id,
            rel_path,
            size,
        )
