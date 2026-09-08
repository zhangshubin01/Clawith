"""Structured list items for agent working state (清单线 focus 范式迁移).

The numbered-list channel is migrated off free-text extraction onto the same
structured model focus uses, with one extra ``project`` scope dimension. The
list identity lives in ``sort_order`` (stable, monotonic, never renumbered —
the visible number the user references) and ``key`` (semantic slug), so the
presented / stored / injected numbers are all projections of one value.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AgentListItem(Base):
    """One structured list entry tracked per (agent, project).

    ``project`` is the list scope, platform-derived from the workspace name
    (never model-provided), so the same workspace merges into one list across
    Runs. ``sort_order`` is the stable visible number — per-(agent, project)
    monotonic and never renumbered; a second unique constraint on
    ``(agent_id, project, sort_order)`` guards concurrent number allocation.
    """

    __tablename__ = "agent_list_items"
    __table_args__ = (
        UniqueConstraint("agent_id", "project", "key", name="uq_agent_list_items_agent_project_key"),
        UniqueConstraint("agent_id", "project", "sort_order", name="uq_agent_list_items_agent_project_sort"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", index=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
