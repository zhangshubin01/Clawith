"""CCR（Compressed Context Recovery）原文归档表。

Layer 0 工具结果有损压缩前，先将完整原文写入本表，压缩结果内嵌
`<!-- ccr:<hash> -->` marker + `retrieve_context(hash=...)` 提示。
Agent 需要完整内容时调用 retrieve_context 工具按 (session_id, content_hash) 取回。

设计取舍（与 docs/openspec/ctx-fidelity-compression 一致）：
- session_id / agent_id 用 String(64)，不设外键。原因：本层的 session_id 等价于
  chat_messages.conversation_id（字符串），且部分入口（瞬态会话）未必落 chat_sessions，
  硬外键会导致 store 失败 → reversibility gate 回退原文 → 丢失压缩收益。
  清理依赖 TTL（expires_at）+ 单会话行数上限，而非级联删除。
- content_hash 为完整 64 位 SHA256 hex，UNIQUE(session_id, content_hash) 保证幂等。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, Index, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CtxCcrEntry(Base):
    """单条 CCR 归档：某工具结果的完整原文 + token 元数据。"""

    __tablename__ = "ctx_ccr_entries"
    __table_args__ = (
        # 幂等键：同会话同内容只存一行
        UniqueConstraint("session_id", "content_hash", name="uq_ctx_ccr_session_hash"),
        # retrieve 查询主路径
        # purge_expired 扫描
        Index("ix_ctx_ccr_expires_at", "expires_at"),
        Index("ix_ctx_ccr_session_created", "session_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 等价 chat_messages.conversation_id（字符串），非外键，见模块 docstring
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 完整 64 位 SHA256 hex
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    # 归档的完整原文
    content: Mapped[str] = mapped_column(Text, nullable=False)
    original_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    compressed_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 压缩发生端：acp | ws | feishu
    path: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # created_at + CTX_CCR_TTL_HOURS，由 ccr_store 写入（索引见 __table_args__，此处不重复声明）
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
