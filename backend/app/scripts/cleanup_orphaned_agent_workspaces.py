"""Remove workspace directories whose Agent rows no longer exist.

The platform deletes Agents logically, so orphaned workspaces only appear when
rows are removed out-of-band (manual SQL, or a future hard-delete). This script
sweeps the workspace root, keeps every directory whose id exists in the
``agents`` table (including soft-deleted rows), and removes the rest — dry-run
by default.

Usage:
  docker exec clawith-agent-backend-1 python3 -m app.scripts.cleanup_orphaned_agent_workspaces
  docker exec clawith-agent-backend-1 python3 -m app.scripts.cleanup_orphaned_agent_workspaces --apply --older-than-days 7
  cd backend && python3 -m app.scripts.cleanup_orphaned_agent_workspaces --apply
"""

import argparse
import asyncio
import time
import uuid
from pathlib import Path

from loguru import logger


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually remove orphaned workspaces (default is dry-run)",
    )
    parser.add_argument(
        "--older-than-days",
        type=float,
        default=7.0,
        help="Only consider directories untouched for more than N days (default 7)",
    )
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()

    from sqlalchemy import select

    from app.database import async_session
    from app.models.agent import Agent
    from app.services.agent_workspace_cleanup import (
        remove_agent_workspace,
        workspace_root_path,
    )

    root = workspace_root_path()
    logger.info("Workspace root: {} (apply={})", root, args.apply)

    async with async_session() as db:
        known_ids = set((await db.execute(select(Agent.id))).scalars().all())
    logger.info("Known agent ids in DB (incl. soft-deleted): {}", len(known_ids))

    cutoff = time.time() - args.older_than_days * 86400
    candidates: list[Path] = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        try:
            agent_id = uuid.UUID(entry.name)
        except ValueError:
            # Not an agent workspace dir (e.g. the 'runtime' helper dir).
            continue
        if agent_id in known_ids:
            continue
        if args.older_than_days and entry.stat().st_mtime > cutoff:
            continue
        candidates.append(entry)

    logger.info(
        "Orphaned workspace dirs older than {}d: {}",
        args.older_than_days,
        len(candidates),
    )
    removed = 0
    for entry in candidates:
        if not args.apply:
            logger.info("  [dry-run] would remove {} ({})", entry.name, entry)
            continue
        try:
            if remove_agent_workspace(uuid.UUID(entry.name), root=root):
                removed += 1
        except OSError as exc:
            logger.error("  Failed to remove {}: {}", entry.name, exc)

    logger.info("Done. removed={}, apply={}", removed, args.apply)


if __name__ == "__main__":
    asyncio.run(main())
