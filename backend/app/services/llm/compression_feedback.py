"""Retrieve 反馈闭环 — 默认关闭，待 stats 数据积累后灰度。

日志前缀 [CTX-FEEDBACK]。
"""

from __future__ import annotations

from dataclasses import dataclass

from loguru import logger


@dataclass(frozen=True)
class ToolCompressionPolicy:
    relevance_threshold_delta: float = 0.0
    keep_head_lines: int = 40
    keep_tail_lines: int = 20
    allow_relevance_split: bool = True
    allow_summary: bool = True


def get_tool_policy(tool_name: str, content_type: str) -> ToolCompressionPolicy:
    """返回 per-tool 压缩策略；feedback 关闭时恒为默认。"""
    try:
        from app.config import get_settings
        if not getattr(get_settings(), "CTX_COMPRESSION_FEEDBACK_ENABLED", False):
            return ToolCompressionPolicy()
    except Exception:
        return ToolCompressionPolicy()
    logger.debug("[CTX-FEEDBACK] policy tool={} type={} (stub)", tool_name, content_type)
    return ToolCompressionPolicy()


def record_feedback(tool_name: str, lossiness: str, event: str) -> None:
    """记录 retrieve/压缩事件；首版 stub 仅 debug。"""
    try:
        from app.config import get_settings
        if not getattr(get_settings(), "CTX_COMPRESSION_FEEDBACK_ENABLED", False):
            return
    except Exception:
        return
    logger.debug("[CTX-FEEDBACK] event tool={} lossiness={} event={}", tool_name, lossiness, event)
