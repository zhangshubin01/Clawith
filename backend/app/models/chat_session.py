"""Chat session model for grouping chat messages per participant-agent pair."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, ForeignKey, func, UniqueConstraint, Index, text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ChatSession(Base):
    """A named session grouping chat messages between a user and an agent.

    source_channel: 'web' | 'feishu' | 'discord' | 'slack'
    external_conv_id: original channel conversation ID (e.g. 'feishu_p2p_ou_xxx').
                      Unique per agent — used for reliable find-or-create without in-process caching.
    is_group: True for group chat sessions (Feishu group, WeCom group, Slack channel, etc.).
              Group sessions have user_id=NULL and only appear in the 'all sessions' view.
    group_name: Display name for group chat sessions (e.g. the group/channel name from IM platform).
    """

    __tablename__ = "chat_sessions"
    __table_args__ = (
        UniqueConstraint("agent_id", "external_conv_id", name="uq_chat_sessions_agent_ext_conv"),
        Index(
            "uq_chat_sessions_primary_platform",
            "agent_id",
            "user_id",
            unique=True,
            postgresql_where=text("is_primary = true AND source_channel = 'web' AND is_group = false"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    # user_id: for P2P sessions this is the user; for group sessions this is the agent creator (placeholder)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="New Session")
    source_channel: Mapped[str] = mapped_column(String(20), nullable=False, default="web")
    external_conv_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Group chat support: group sessions have user_id=NULL and show group_name instead
    is_group: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    group_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Participant identity (unified User/Agent identity)
    participant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("participants.id"), nullable=True)
    # For agent-to-agent sessions: the other agent in the conversation
    peer_agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=True)
    # Primary platform session: the long-lived first-party conversation that agent-initiated
    # messages should land in. User-created side-topic sessions remain temporary (`is_primary=false`).
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False, index=True)
    # Tracks when the owning platform user last opened/read this session. Unread badges are derived
    # from non-user messages created after this timestamp.
    last_read_at_by_user: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ORM 关系：chat_messages.conversation_id (String) ↔ chat_sessions.id (UUID)
    # 通过 cast 桥接两种类型，提供类型安全的 ORM join 替代 raw SQL cast（#61 修复）
    messages = relationship(
        "ChatMessage",
        primaryjoin="cast(ChatSession.id, String) == ChatMessage.conversation_id",
        viewonly=True,
    )

    # IDE 插件上下文（迁移 29f3f8de3ca0 + 25811072c8fd 添加，SQLAlchemy 模型需显式映射）
    client_type: Mapped[str] = mapped_column(String(20), nullable=False, default="web", server_default="web")
    # 客户端类型: 'web' | 'ide_plugin' | 'ide_lsp4j' — 区分 Web UI、ACP、LSP4J 来源
    project_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # IDE 项目根路径，用于文件索引构建和相对路径解析
    current_file: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # IDE 当前活动编辑文件路径
    open_files: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # IDE 当前打开的文件列表（JSON 数组），用于 LLM 上下文注入