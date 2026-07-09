"""输出 token shaping — 独立 initiative，默认关闭。

仅在 mechanical 报告场景追加简短 suffix；由 caller 单点注入，不在 ws/feishu/acp 分散实现。
日志前缀 [CTX-SHAPER]。
"""

from __future__ import annotations

from loguru import logger

_DETAIL_KEYWORDS = (
    "详细", "逐步", "完整", "审查", "安全分析", "explain in detail", "step by step", "full review",
)


def build_output_shaping_suffix(
    *,
    path: str,
    user_query: str,
    recent_tool_count: int,
    model_name: str,
) -> str:
    """构建可选的输出简洁 suffix；默认关闭时由 caller 短路不调用。"""
    try:
        from app.config import get_settings

        settings = get_settings()
        if not getattr(settings, "CTX_OUTPUT_SHAPER_ENABLED", False):
            return ""
        allowed = {
            p.strip()
            for p in getattr(settings, "CTX_OUTPUT_SHAPER_PATHS", "acp,ws,feishu").split(",")
            if p.strip()
        }
        if path not in allowed:
            return ""
        q = (user_query or "").lower()
        if any(kw in q for kw in _DETAIL_KEYWORDS):
            logger.debug("[CTX-SHAPER] skip path={} reason=detail_requested", path)
            return ""
        max_chars = int(getattr(settings, "CTX_OUTPUT_SHAPER_MAX_SUFFIX_CHARS", 500))
        suffix = (
            "\n\n[回复约束：简洁要点，避免重复工具输出全文；"
            "用户未要求详细说明时不要展开长报告。]"
        )
        if len(suffix) > max_chars:
            suffix = suffix[:max_chars]
        logger.info("[CTX-SHAPER] apply path={} tools_recent={} model={}", path, recent_tool_count, model_name)
        return suffix
    except Exception as e:
        logger.warning("[CTX-SHAPER] build failed path={} err={}", path, e)
        return ""
