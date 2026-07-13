"""Static registry and query projection for durable Agent runs."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AgentRun(Base):
    """Product-owned identity, delivery facts, and rebuildable projections for a run."""

    __tablename__ = "agent_runs"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_agent_runs"),
        CheckConstraint(
            "source_type IN ('chat', 'trigger', 'task', 'a2a', 'heartbeat')",
            name="ck_agent_runs_source_type",
        ),
        CheckConstraint(
            "run_kind IN ('foreground', 'background', 'delegated', 'orchestration')",
            name="ck_agent_runs_run_kind",
        ),
        CheckConstraint(
            "runtime_type IN ('legacy', 'langgraph')",
            name="ck_agent_runs_runtime_type",
        ),
        CheckConstraint(
            "projected_execution_status IS NULL OR projected_execution_status IN "
            "('created', 'queued', 'running', 'waiting_user', 'waiting_external', "
            "'waiting_agent', 'verifying', 'completed', 'failed', 'cancelled')",
            name="ck_agent_runs_projected_execution_status",
        ),
        CheckConstraint(
            "delivery_status IN ('not_required', 'pending', 'delivered', 'failed')",
            name="ck_agent_runs_delivery_status",
        ),
        CheckConstraint(
            "runtime_type <> 'langgraph' OR model_id IS NOT NULL",
            name="ck_agent_runs_langgraph_model",
        ),
        CheckConstraint(
            "lane_held = false OR scheduling_lane_key IS NOT NULL",
            name="ck_agent_runs_lane_holder_key",
        ),
        CheckConstraint(
            "(scheduling_lane_key IS NULL AND scheduling_position_created_at IS NULL "
            "AND scheduling_position_id IS NULL) OR "
            "(scheduling_lane_key IS NOT NULL AND scheduling_position_created_at IS NOT NULL "
            "AND scheduling_position_id IS NOT NULL)",
            name="ck_agent_runs_lane_position",
        ),
        CheckConstraint(
            "(run_kind = 'orchestration' AND agent_id IS NULL "
            "AND system_role = 'group_planning' AND model_id IS NOT NULL) OR "
            "(run_kind <> 'orchestration' AND agent_id IS NOT NULL AND system_role IS NULL)",
            name="ck_agent_runs_orchestration_identity",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "session_id"],
            ["chat_sessions.tenant_id", "chat_sessions.id"],
            name="fk_agent_runs_tenant_session_chat_sessions",
        ),
        UniqueConstraint("runtime_thread_id", name="uq_agent_runs_runtime_thread_id"),
        UniqueConstraint("tenant_id", "id", name="uq_agent_runs_tenant_id_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", name="fk_agent_runs_tenant_id_tenants", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", name="fk_agent_runs_agent_id_agents", ondelete="CASCADE"),
        nullable=True,
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "chat_sessions.id",
            name="fk_agent_runs_session_id_chat_sessions",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_execution_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    origin_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", name="fk_agent_runs_origin_user_id_users", ondelete="SET NULL"),
        nullable=True,
    )
    origin_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", name="fk_agent_runs_origin_agent_id_agents", ondelete="SET NULL"),
        nullable=True,
    )
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_runs.id", name="fk_agent_runs_parent_run_id_agent_runs", ondelete="SET NULL"),
        nullable=True,
    )
    root_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_runs.id", name="fk_agent_runs_root_run_id_agent_runs", ondelete="SET NULL"),
        nullable=True,
    )
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    run_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    system_role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("llm_models.id", name="fk_agent_runs_model_id_llm_models", ondelete="RESTRICT"),
        nullable=True,
    )
    runtime_type: Mapped[str] = mapped_column(String(24), nullable=False)
    runtime_thread_id: Mapped[str] = mapped_column(String(255), nullable=False)
    graph_name: Mapped[str] = mapped_column(String(100), nullable=False)
    graph_version: Mapped[str] = mapped_column(String(64), nullable=False)
    scheduling_lane_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    scheduling_position_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    scheduling_position_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    lane_held: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    lane_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    projected_execution_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    projected_waiting_type: Mapped[str | None] = mapped_column(String(24), nullable=True)
    projected_waiting_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    projected_result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    projected_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    projected_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    projected_checkpoint_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    projection_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivery_status: Mapped[str] = mapped_column(String(24), nullable=False)
    delivery_target: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    projected_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    projected_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


Index(
    "ix_agent_runs_tenant_agent_projected_status",
    AgentRun.tenant_id,
    AgentRun.agent_id,
    AgentRun.projected_execution_status,
)
Index("ix_agent_runs_session_created_at", AgentRun.session_id, AgentRun.created_at.desc())
Index("ix_agent_runs_parent_run_id", AgentRun.parent_run_id)
Index("ix_agent_runs_root_run_id", AgentRun.root_run_id)
Index("ix_agent_runs_source", AgentRun.source_type, AgentRun.source_id)
Index(
    "uq_agent_runs_source_execution",
    AgentRun.source_type,
    AgentRun.source_execution_id,
    unique=True,
    postgresql_where=AgentRun.source_execution_id.is_not(None),
)
Index(
    "uq_agent_runs_active_lane",
    AgentRun.scheduling_lane_key,
    unique=True,
    postgresql_where=(AgentRun.scheduling_lane_key.is_not(None) & AgentRun.lane_held.is_(True)),
)
Index(
    "ix_agent_runs_lane_candidate_order",
    AgentRun.scheduling_lane_key,
    AgentRun.scheduling_position_created_at,
    AgentRun.scheduling_position_id,
    AgentRun.created_at,
    AgentRun.id,
    postgresql_where=AgentRun.scheduling_lane_key.is_not(None),
)
