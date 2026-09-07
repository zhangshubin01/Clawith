"""Resolve orphaned agent-scoped delete_files approvals left behind by the Maintainer gate.

The Maintainer gate (feat ``aa539999``) replaced the L3 approval flow for
agent-scoped ``delete_file`` with an explicit maintainer list. Any
``ApprovalRequest(action_type=delete_files)`` that was still ``pending`` at
that point became an orphan: nothing consumes it anymore, so its Run stays in
``waiting_user`` forever.

This script reconciles those orphans. For every pending, agent-scoped
``delete_files`` approval it resolves the request as ``rejected`` (by the
agent's creator) and resumes the waiting Run with a message telling the model
the gate has taken over. Group-scoped deletes still flow through the L3 flow
and are **never** touched.

Design: docs/technical-plans/20260907-resolve-orphaned-delete-approvals-fix-plan.md

Safety rails:
  1. Only ``pending`` + ``action_type=delete_files`` + ``workspace_scope=agent``
     rows are touched (group-scoped approvals are excluded).
  2. ``--dry-run`` is the default; resolutions require ``--apply``.
  3. Each approval resolves and its resume command enqueue commit atomically in
     one transaction — a failure rolls back that approval and leaves it
     ``pending`` for retry (never a half-resolved orphan).
  4. Idempotent: the resume command is keyed ``approval:{id}:rejected``, and an
     already-resolved approval is skipped on re-run.

Usage (inside the backend container):
    python -m app.scripts.resolve_orphaned_delete_approvals --dry-run
    python -m app.scripts.resolve_orphaned_delete_approvals --apply
"""

from __future__ import annotations

import argparse
import asyncio

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.agent import Agent
from app.models.audit import ApprovalRequest
from app.models.user import User
from app.services.autonomy_service import autonomy_service

RESUME_MESSAGE = (
    "文件删除的 L3 审批流程已被维护者门控取代，此前的删除申请已关闭且未执行。"
    "如仍需删除，请重新发起删除工具调用，系统将按维护者名单自动判定。"
)


def _is_orphan_target(approval: ApprovalRequest) -> bool:
    """True for agent-scoped approvals (group-scoped = L3 still live).

    The caller pre-filters to ``pending`` + ``action_type=delete_files`` rows;
    this only distinguishes agent-scoped from group-scoped scope.
    """
    scope = (approval.details or {}).get("runtime_scope")
    return isinstance(scope, dict) and scope.get("workspace_scope") == "agent"


def _describe(approval: ApprovalRequest) -> str:
    details = approval.details or {}
    args = details.get("args")
    path = args.get("path") if isinstance(args, dict) else None
    run_id = (details.get("runtime_scope") or {}).get("run_id")
    return f"approval={approval.id} agent={approval.agent_id} run={run_id} path={path}"


async def run(dry_run: bool) -> int:
    async with async_session() as db:
        result = await db.execute(
            select(ApprovalRequest).where(
                ApprovalRequest.action_type == "delete_files",
                ApprovalRequest.status == "pending",
            )
        )
        targets = [a for a in result.scalars().all() if _is_orphan_target(a)]

    logger.info("orphan agent-scoped delete_files approvals (pending): {}", len(targets))

    resolved = 0
    failed = 0
    for approval in targets:
        async with async_session() as db:
            agent = (
                await db.execute(select(Agent).where(Agent.id == approval.agent_id))
            ).scalar_one_or_none()
            if agent is None:
                logger.warning("skip {}: agent missing", _describe(approval))
                continue
            creator = (
                await db.execute(select(User).where(User.id == agent.creator_id))
            ).scalar_one_or_none()
            if creator is None:
                logger.warning("skip {}: creator missing", _describe(approval))
                continue

            if dry_run:
                logger.info("DRY-RUN would reject + resume {}", _describe(approval))
                continue

            try:
                await autonomy_service.resolve_approval(
                    db,
                    approval.id,
                    creator,
                    "reject",
                    resume_message=RESUME_MESSAGE,
                )
                await db.commit()
                resolved += 1
                logger.info("resolved -> rejected {}", _describe(approval))
            except Exception as exc:  # noqa: BLE001 — per-approval isolation
                await db.rollback()
                failed += 1
                logger.error("failed to resolve {}: {}", _describe(approval), exc)

    logger.info("done: {} resolved, {} failed, {} target(s)", resolved, failed, len(targets))
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="apply", action="store_false", help="report only (default)")
    mode.add_argument("--apply", dest="apply", action="store_true", help="actually resolve")
    parser.set_defaults(apply=False)
    args = parser.parse_args()
    print(f"=== {'APPLY' if args.apply else 'DRY-RUN'}: orphaned delete_files approvals ===")
    return asyncio.run(run(dry_run=not args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
