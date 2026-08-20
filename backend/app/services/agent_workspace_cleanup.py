"""Remove a per-agent workspace directory with strict path containment.

Agents are deleted logically by the platform: the row keeps ``deleted_at`` and
the workspace is intentionally retained so history stays readable. A workspace
directory only becomes garbage when an Agent row is physically removed — e.g.
out-of-band DB cleanup, or a future hard-delete flow.

This module is the single safe primitive for that removal:

- ``agent_id`` must be a :class:`uuid.UUID`; the target path is
  ``<workspace root>/<str(agent_id)>``, so an id can never introduce ``..``
  or absolute path components.
- The resolved target must be a strict subdirectory of the resolved root.
- A symlink at the target is refused, never followed.
"""

from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)


def workspace_root_path() -> Path:
    """Return the resolved per-agent workspace root (same source as ``agent_tools.WORKSPACE_ROOT``)."""
    from app.config import get_settings

    settings = get_settings()
    return Path(settings.STORAGE_LOCAL_ROOT or settings.AGENT_DATA_DIR).resolve()


def agent_workspace_path(agent_id: uuid.UUID, *, root: Path | None = None) -> Path:
    """Resolve the per-agent workspace path, raising ``ValueError`` if it escapes the root."""
    root_resolved = (root or workspace_root_path()).resolve()
    target = (root_resolved / str(agent_id)).resolve()
    target.relative_to(root_resolved)
    return target


def remove_agent_workspace(agent_id: uuid.UUID, *, root: Path | None = None) -> bool:
    """Delete the per-agent workspace directory.

    Returns ``True`` when a directory was removed, ``False`` when there was
    nothing to remove. Refuses (logs a warning, returns ``False``) when the
    target is a symlink or the id would escape the workspace root; real
    filesystem failures raise ``OSError``.
    """
    try:
        target = agent_workspace_path(agent_id, root=root)
    except ValueError:
        logger.warning(
            "Workspace removal refused: agent id %s escapes the workspace root",
            agent_id,
        )
        return False

    if target.is_symlink():
        logger.warning("Workspace removal refused: %s is a symlink", target)
        return False
    if not target.exists():
        return False
    if not target.is_dir():
        raise NotADirectoryError(f"Workspace path is not a directory: {target}")

    shutil.rmtree(target)
    logger.info("Removed workspace for agent %s at %s", agent_id, target)
    return True
