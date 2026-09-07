"""Maintainer permission gate for agent workspace/skills file modifications.

The Maintainer gate replaces the L3 delete approval flow with an explicit
maintainer list: only the agent creator (implicit, never stored) plus rows in
``agent_maintainers`` may drive write_file/edit_file/delete_file/move_file into
``workspace/`` and ``skills/``.

Design anchors (docs/technical-plans/20260905-maintainer-gate-g3-g4-production-plan.md §3.3):
  1. a2a (actor_agent_id non-empty) -> NOT_GATED
  2. group-scoped -> NOT_GATED (group flow owns its approval)
  3. tool not in the four document tools -> NOT_GATED
  4. path bucket: workspace/skills -> gate; memory/ -> open; soul.md/tasks.json/
     enterprise_info -> DEFER (tool description already rejects these)
  5. actor_user_id empty -> fall back to agent.creator_id
  6. creator (implicit maintainer) or is_maintainer(actor) -> GATED_ALLOWED /
     GATED_DENIED
"""

import uuid
from enum import Enum
from typing import Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent, AgentMaintainer
from app.services.builtin_tool_definitions import _PATH_CONVENTION_PARAMS
from app.services.workspace_collaboration import normalize_workspace_path


class FileModifyDecision(Enum):
    """Outcome of the Maintainer gate for a file-modifying tool call."""

    GATED_ALLOWED = "gated_allowed"
    GATED_DENIED = "gated_denied"
    NOT_GATED = "not_gated"
    DEFER = "defer"


# The four document tools the Maintainer gate covers. Group tools
# (group_write_workspace_file / group_delete_workspace_file) and group-scoped
# write/edit/delete are handled by the group flow, never here.
FILE_MODIFY_TOOL_NAMES = frozenset({"delete_file", "edit_file", "write_file", "move_file"})


def coerce_actor(value: object) -> uuid.UUID | None:
    """Coerce an actor id (uuid | str | None) to uuid.UUID, None on failure."""
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str) and value:
        try:
            return uuid.UUID(value)
        except ValueError:
            return None
    return None


def _classify_path(normalized: str) -> str:
    """Classify a normalized agent-relative path into gated / defer / open."""
    if (
        normalized in {"workspace", "skills"}
        or normalized.startswith("workspace/")
        or normalized.startswith("skills/")
    ):
        return "gated"
    if normalized in {"soul.md", "tasks.json"}:
        return "defer"
    if normalized == "enterprise_info" or normalized.startswith("enterprise_info/"):
        return "defer"
    return "open"


def _path_bucket(tool_name: str, arguments: Mapping[str, object]) -> str:
    """Highest-priority bucket across all path params of a file tool.

    Priority: gated > defer > open. memory/ and any other prefix map to "open".

    ``normalize_workspace_path`` already neutralizes absolute paths and ``..``
    traversal, so the prefix judgment cannot be escaped by string tricks. It is
    intentionally NOT symlink-aware (``safe_agent_path`` would ``.resolve()``):
    the gate is a governance layer, not hard security (grill decision 1 / §3.1
    G-4), and a symlink must first be planted via ``execute_code`` — an accepted
    bypass surface. Symlink-aware prefix classification is a separate hardening
    if that surface ever closes.
    """
    best = "open"
    for param in _PATH_CONVENTION_PARAMS.get(tool_name, ()):
        raw = arguments.get(param)
        if not isinstance(raw, str) or not raw:
            continue
        bucket = _classify_path(normalize_workspace_path(raw))
        if bucket == "gated":
            return "gated"
        if bucket == "defer":
            best = "defer"
    return best


class MaintainerService:
    """Resolve whether a user may drive an agent's workspace/skills file tool."""

    async def is_maintainer(
        self, db: AsyncSession, agent_id: uuid.UUID, user_id: uuid.UUID
    ) -> bool:
        result = await db.execute(
            select(AgentMaintainer.id)
            .where(
                AgentMaintainer.agent_id == agent_id,
                AgentMaintainer.user_id == user_id,
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def resolve_file_modify_permission(
        self,
        db: AsyncSession,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
        agent: Agent,
        actor_user_id: object,
        actor_agent_id: object = None,
        is_group_scoped: bool = False,
    ) -> FileModifyDecision:
        """Decide whether ``tool_name`` on ``arguments`` is gated and allowed."""
        actor_user_id = coerce_actor(actor_user_id)
        actor_agent_id = coerce_actor(actor_agent_id)
        if actor_agent_id is not None:
            return FileModifyDecision.NOT_GATED
        if is_group_scoped:
            return FileModifyDecision.NOT_GATED
        if tool_name not in FILE_MODIFY_TOOL_NAMES:
            return FileModifyDecision.NOT_GATED

        bucket = _path_bucket(tool_name, arguments)
        if bucket == "open":
            return FileModifyDecision.NOT_GATED
        if bucket == "defer":
            return FileModifyDecision.DEFER

        actor = actor_user_id or agent.creator_id
        # The agent creator is an implicit maintainer (M-1 / §3.2): never
        # stored in ``agent_maintainers``, cannot be removed, and always allowed.
        # This also covers the heartbeat/trigger case where actor_user_id is
        # empty and we fall back to the creator (grill decision 3).
        if actor == agent.creator_id:
            return FileModifyDecision.GATED_ALLOWED
        if await self.is_maintainer(db, agent.id, actor):
            return FileModifyDecision.GATED_ALLOWED
        return FileModifyDecision.GATED_DENIED


maintainer_service = MaintainerService()
