"""Experience Library models.

The team experience library replaces the old Plaza social feed: human-curated,
AI-consumed private knowledge. Each entry is a fixed four-part structure and only
becomes retrievable once a human publishes it.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ExperienceEntry(Base):
    """A single curated experience entry.

    Status lifecycle:
        draft     — AI-generated or human-authored, not yet retrievable
        published — human-reviewed, injected into agent context directory
        retired   — marked stale; excluded from retrieval
    """

    __tablename__ = "experience_entries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)

    # ── P0-3: fixed four-part structure (all four required to publish) ──
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")  # shown in the directory
    scenario: Mapped[str] = mapped_column(Text, nullable=False, default="")        # 场景
    problem: Mapped[str] = mapped_column(Text, nullable=False, default="")         # 遇到的问题
    solution: Mapped[str] = mapped_column(Text, nullable=False, default="")        # 解决方式
    applicability: Mapped[str] = mapped_column(Text, nullable=False, default="")   # 适用条件与失效信号 (required)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft", index=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)  # P1-2

    # ── P0-6: visibility (mirrors AgentPermission.scope_type / scope_id) ──
    # scope: company | department | user
    # scope_id: null for company, department_id for department, user_id for user
    visibility_scope: Mapped[str] = mapped_column(String(16), nullable=False, default="company", index=True)
    visibility_scope_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    # Provenance of the entry itself: normal distillation vs imported legacy Plaza data.
    # legacy_plaza entries are hard-isolated — never returned by search_experience.
    origin: Mapped[str] = mapped_column(String(20), nullable=False, default="chat", index=True)  # chat | legacy_plaza

    # ── Provenance & governance ──
    origin_session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # source conversation
    origin_agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))    # agent present when distilled
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)  # initiator (chat participant)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))          # who approved publish
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # P1-3

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
