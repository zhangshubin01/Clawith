"""rtk 风格输出门控。

压缩正文和恢复提示拼接后仍必须比原文更省 token，否则回退原文。
"""

from __future__ import annotations

from .truncate_caps import never_worse


def emit_guarded(
    compressed: str,
    hint: str | None,
    original: str,
    model_name: str = "",
    *,
    ctx_path: str = "",
) -> str:
    """拼接 hint 后执行 never_worse（可按 path 灰度打 RTK 日志）。"""
    return never_worse(compressed, hint, original, model_name, ctx_path=ctx_path)
