"""Layer0 压缩结构化结果 — 区分无损/可恢复有损，供 CCR gate 与 stats 使用。

日志前缀 [CTX-COMPRESS]。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Lossiness(StrEnum):
    """压缩有损程度分类。"""

    NONE = "none"
    LOSSLESS = "lossless"
    TAIL = "tail"
    SUMMARY = "summary"
    RELEVANCE_SPLIT = "relevance_split"
    HARD_CEIL = "hard_ceil"


@dataclass(frozen=True)
class CompressionResult:
    """单次 Layer0 压缩结果；caller 据此决定是否走 CCR store。"""

    content: str
    changed: bool
    lossiness: Lossiness
    recoverable: bool
    strategy: str
    original_tokens: int
    final_tokens: int
    recover_hint: str = ""


def requires_ccr(result: CompressionResult) -> bool:
    """仅可恢复有损路径需要 CCR 归档；无损/未变更不 store。"""
    if not result.changed:
        return False
    if result.lossiness in (Lossiness.NONE, Lossiness.LOSSLESS):
        return False
    return result.recoverable


def unchanged(original: str, *, strategy: str = "passthrough", tokens: int = 0) -> CompressionResult:
    """原文未变时的标准结果。"""
    t = tokens or 0
    return CompressionResult(
        content=original,
        changed=False,
        lossiness=Lossiness.NONE,
        recoverable=False,
        strategy=strategy,
        original_tokens=t,
        final_tokens=t,
    )
