"""ACP 能力开关 — 通过环境变量 ACP_FEATURES 控制 document/nes/providers 等扩展。"""

from __future__ import annotations

import os

_DEFAULT_FEATURES = frozenset({"document", "nes", "providers"})


def acp_feature_enabled(name: str) -> bool:
    """检查某项 ACP 扩展是否启用；空 env 表示全部关闭。"""
    raw = os.getenv("ACP_FEATURES", "document,nes,providers")
    if not raw.strip():
        return False
    enabled = {part.strip().lower() for part in raw.split(",") if part.strip()}
    return name.strip().lower() in enabled


def enabled_features() -> frozenset[str]:
    """返回当前启用的特性名集合（调试用）。"""
    raw = os.getenv("ACP_FEATURES", "document,nes,providers")
    if not raw.strip():
        return frozenset()
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())
