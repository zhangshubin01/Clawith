"""IDE 插件配置存储（Clawith LSP4J 等插件的通用 KV 配置）。"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class IDEPluginConfig(Base):
    """IDE 插件键值配置表。

    scope_type + scope_id + config_key 唯一约束，支持 agent/user 级别的配置隔离。
    """

    __tablename__ = "ide_plugin_configs"
    __table_args__ = (
        UniqueConstraint("scope_type", "scope_id", "config_key", name="uq_plugin_config_scope_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scope_type: Mapped[str] = mapped_column(String(20), nullable=False)  # "agent" | "user"
    scope_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    config_key: Mapped[str] = mapped_column(String(100), nullable=False)
    config_value: Mapped[str] = mapped_column(Text, nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
