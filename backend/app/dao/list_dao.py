"""DAO for structured agent list items (清单线).

Mirrors ``focus_dao`` with one extra ``project`` scope dimension. The one
deliberate divergence from focus is atomic number issuance: because the
visible number IS the identity (unlike focus, where ``key`` is the identity
and ``sort_order`` is an internal sort), ``upsert_item`` allocates
``max+1`` under a savepoint + ``(agent_id, project, sort_order)`` unique
constraint, retrying on ``IntegrityError`` (the pattern established in
``participant_identity.create_participant``).
"""

from datetime import datetime
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from app.dao.base import BaseDAO
from app.models.list import AgentListItem


class ListDAO(BaseDAO[AgentListItem]):
    """Persistence operations for agent list state."""

    def __init__(self) -> None:
        super().__init__(AgentListItem)

    async def count_by_project(self, agent_id: Any, project: str) -> int:
        """Count list items for one (agent, project) scope."""
        async with self.session(readonly=True) as db:
            result = await db.scalar(
                select(func.count())
                .select_from(AgentListItem)
                .where(AgentListItem.agent_id == agent_id, AgentListItem.project == project)
            )
            return int(result or 0)

    async def max_sort_order(self, agent_id: Any, project: str) -> int:
        """Highest sort_order for one (agent, project) scope; 0 when empty."""
        async with self.session(readonly=True) as db:
            result = await db.scalar(
                select(func.max(AgentListItem.sort_order)).where(
                    AgentListItem.agent_id == agent_id,
                    AgentListItem.project == project,
                )
            )
            return int(result or 0)

    async def bulk_insert_legacy_rows(self, rows: list[dict[str, Any]]) -> int:
        """Insert migrated legacy rows, ignoring existing agent/project/key rows."""
        if not rows:
            return 0
        async with self.session() as db:
            stmt = insert(AgentListItem).values(rows)
            stmt = stmt.on_conflict_do_nothing(index_elements=["agent_id", "project", "key"])
            result = await db.execute(stmt)
            await db.flush()
            return result.rowcount or 0

    async def list_by_project(
        self,
        *,
        agent_id: Any,
        project: str,
        include_completed: bool,
    ) -> Sequence[AgentListItem]:
        """List items in stable sort_order (never renumbered)."""
        async with self.session(readonly=True) as db:
            stmt = select(AgentListItem).where(
                AgentListItem.agent_id == agent_id,
                AgentListItem.project == project,
            )
            if not include_completed:
                stmt = stmt.where(AgentListItem.status != "completed")
            stmt = stmt.order_by(
                AgentListItem.sort_order.asc(),
                AgentListItem.created_at.asc(),
            )
            result = await db.execute(stmt)
            return result.scalars().all()

    async def upsert_item(
        self,
        *,
        agent_id: Any,
        project: str,
        key: str,
        title: str | None,
        description: str,
        status: str,
        completed_at: datetime | None,
    ) -> AgentListItem:
        """Create or update a list item by (agent, project, key).

        New items get ``sort_order = max+1`` per (agent, project), issued
        atomically: a concurrent insert that wins the same number raises
        ``IntegrityError`` on the savepoint, and the caller retries the
        allocation. Existing items keep their ``sort_order`` forever.
        """
        async with self.session() as db:
            existing = await self._find_item(db, agent_id, project, key)
            if existing is not None:
                if title is not None:
                    existing.title = title
                existing.description = description or existing.description or key
                existing.status = status
                existing.completed_at = completed_at
                await db.flush()
                await db.refresh(existing)
                return existing
            return await self._insert_new(
                db,
                agent_id=agent_id,
                project=project,
                key=key,
                title=title,
                description=description,
                status=status,
                completed_at=completed_at,
            )

    @staticmethod
    async def _find_item(
        db,
        agent_id: Any,
        project: str,
        key: str,
    ) -> AgentListItem | None:
        result = await db.execute(
            select(AgentListItem).where(
                AgentListItem.agent_id == agent_id,
                AgentListItem.project == project,
                AgentListItem.key == key,
            )
        )
        return result.scalar_one_or_none()

    async def _insert_new(
        self,
        db,
        *,
        agent_id: Any,
        project: str,
        key: str,
        title: str | None,
        description: str,
        status: str,
        completed_at: datetime | None,
    ) -> AgentListItem:
        item = AgentListItem(
            agent_id=agent_id,
            project=project,
            key=key,
            title=title,
            description=description or key,
            status=status,
            sort_order=0,
            completed_at=completed_at,
        )
        try:
            async with db.begin_nested():
                max_order = await db.scalar(
                    select(func.max(AgentListItem.sort_order)).where(
                        AgentListItem.agent_id == agent_id,
                        AgentListItem.project == project,
                    )
                )
                item.sort_order = (max_order or 0) + 1
                db.add(item)
                await db.flush()
        except IntegrityError:
            # A concurrent insert claimed this sort_order; retry the whole
            # allocation. The max re-read inside the fresh savepoint yields a
            # free number, so a single retry loop is bounded and correct.
            async with db.begin_nested():
                max_order = await db.scalar(
                    select(func.max(AgentListItem.sort_order)).where(
                        AgentListItem.agent_id == agent_id,
                        AgentListItem.project == project,
                    )
                )
                item.sort_order = (max_order or 0) + 1
                db.add(item)
                await db.flush()
        await db.refresh(item)
        return item

    async def complete_item(
        self,
        *,
        agent_id: Any,
        project: str,
        key: str,
        completed_at: datetime,
    ) -> AgentListItem | None:
        """Mark a list item completed by key; ``None`` when not found."""
        async with self.session() as db:
            item = await self._find_item(db, agent_id, project, key)
            if item is None:
                return None
            item.status = "completed"
            item.completed_at = completed_at
            await db.flush()
            await db.refresh(item)
            return item


list_dao = ListDAO()
