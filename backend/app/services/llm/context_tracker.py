"""多轮 CCR 上下文跟踪 — 按查询相关性推荐 retrieve_context 预展开。

参考 headroom context_tracker；Clawith 不自动拉取 PG 原文，仅向 system prompt 注入
「建议 retrieve 的 hash」提示，由 Agent 显式调用 retrieve_context。
"""

from __future__ import annotations

import re
import time
from collections import OrderedDict
from dataclasses import dataclass

from loguru import logger

_STOP_WORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "must", "can", "need", "to", "of", "in",
    "for", "on", "with", "at", "by", "from", "as", "and", "but", "if", "or",
    "this", "that", "what", "which", "who", "how", "when", "where", "why",
    "you", "your", "me", "my", "i", "it", "its",
})


@dataclass
class CompressedContext:
    hash_key: str
    turn_number: int
    timestamp: float
    tool_name: str | None
    sample_content: str
    workspace_key: str
    query_context: str = ""


@dataclass
class ExpansionRecommendation:
    hash_key: str
    reason: str
    relevance_score: float


@dataclass
class ContextTrackerConfig:
    enabled: bool = True
    max_tracked_contexts: int = 100
    relevance_threshold: float = 0.3
    max_context_age_seconds: float = 300.0
    proactive_expansion: bool = True
    max_proactive_expansions: int = 2


class ContextTracker:
    def __init__(self, config: ContextTrackerConfig | None = None):
        self.config = config or ContextTrackerConfig()
        self._contexts: dict[str, CompressedContext] = {}
        self._turn_order: list[str] = []
        self._current_turn: int = 0

    def track_compression(
        self,
        hash_key: str,
        turn_number: int,
        tool_name: str | None,
        *,
        workspace_key: str,
        query_context: str = "",
        sample_content: str = "",
    ) -> None:
        if not self.config.enabled or not hash_key:
            return
        ctx = CompressedContext(
            hash_key=hash_key,
            turn_number=turn_number,
            timestamp=time.time(),
            tool_name=tool_name,
            sample_content=(sample_content or "")[:2000],
            workspace_key=workspace_key,
            query_context=(query_context or "")[:500],
        )
        if hash_key in self._contexts:
            self._turn_order.remove(hash_key)
        self._contexts[hash_key] = ctx
        self._turn_order.append(hash_key)
        while len(self._contexts) > self.config.max_tracked_contexts:
            oldest = self._turn_order.pop(0)
            self._contexts.pop(oldest, None)
        self._current_turn = max(self._current_turn, turn_number)
        logger.debug(
            "[CTX-TRACKER] track hash={} tool={} workspace={}",
            hash_key[:12], tool_name, workspace_key[:16] if workspace_key else "",
        )

    def analyze_query(
        self,
        query: str,
        current_turn: int | None = None,
        *,
        workspace_key: str,
    ) -> list[ExpansionRecommendation]:
        if not self.config.enabled or not self.config.proactive_expansion:
            return []
        if not workspace_key:
            return []
        if current_turn is not None:
            self._current_turn = current_turn
        now = time.time()
        recs: list[ExpansionRecommendation] = []
        for hash_key, ctx in self._contexts.items():
            if ctx.workspace_key != workspace_key:
                continue
            age = now - ctx.timestamp
            if age > self.config.max_context_age_seconds:
                continue
            relevance = self._calculate_relevance(query, ctx)
            age_factor = 1.0 - (age / self.config.max_context_age_seconds) * 0.5
            relevance *= age_factor
            if relevance >= self.config.relevance_threshold:
                recs.append(
                    ExpansionRecommendation(
                        hash_key=hash_key,
                        reason=self._generate_reason(query, ctx, relevance),
                        relevance_score=relevance,
                    )
                )
        recs.sort(key=lambda r: r.relevance_score, reverse=True)
        return recs[: self.config.max_proactive_expansions]

    def _calculate_relevance(self, query: str, context: CompressedContext) -> float:
        query_words = set(self._extract_keywords(query.lower()))
        if not query_words:
            return 0.0
        score = 0.0
        sample_lower = context.sample_content.lower()
        sample_words = set(self._extract_keywords(sample_lower))
        if sample_words:
            overlap = query_words & sample_words
            score += len(overlap) / len(query_words) * 0.5
            for word in query_words:
                if len(word) >= 4 and word in sample_lower:
                    score += 0.2
        if context.query_context:
            ctx_words = set(self._extract_keywords(context.query_context.lower()))
            if ctx_words:
                score += len(query_words & ctx_words) / len(query_words) * 0.3
        if context.tool_name:
            tool_lower = context.tool_name.lower()
            if any(w in tool_lower for w in ("find", "glob", "search", "grep", "list")):
                if any(w in query.lower() for w in ("file", "where", "find", "show", "list")):
                    score += 0.1
        return min(score, 1.0)

    def _extract_keywords(self, text: str) -> list[str]:
        words = re.findall(r"\b[a-z][a-z0-9_.-]*[a-z0-9]\b|\b[a-z]{2,}\b", text)
        return [w for w in words if w not in _STOP_WORDS and len(w) >= 2]

    def _generate_reason(self, query: str, context: CompressedContext, relevance: float) -> str:
        tool = context.tool_name or "tool"
        return f"与当前问题相关 (score={relevance:.2f})，来自 {tool} 的归档内容"


_MAX_SESSION_TRACKERS = 200
_session_trackers: OrderedDict[str, ContextTracker] = OrderedDict()


def _get_tracker(session_id: str) -> ContextTracker:
    if session_id in _session_trackers:
        _session_trackers.move_to_end(session_id)
        return _session_trackers[session_id]
    tracker = ContextTracker()
    _session_trackers[session_id] = tracker
    while len(_session_trackers) > _MAX_SESSION_TRACKERS:
        _session_trackers.popitem(last=False)
    return tracker


def track_session_compression(
    *,
    session_id: str,
    hash_key: str,
    tool_name: str,
    sample_content: str,
    workspace_key: str,
    query_context: str = "",
    turn_number: int = 0,
) -> None:
    """store_entry 成功后登记压缩事件。"""
    try:
        from app.config import get_settings
        if not getattr(get_settings(), "CTX_TRACKER_ENABLED", True):
            return
    except Exception:
        pass
    if not session_id or not hash_key:
        return
    _get_tracker(session_id).track_compression(
        hash_key,
        turn_number,
        tool_name or None,
        workspace_key=workspace_key,
        query_context=query_context,
        sample_content=sample_content,
    )


def build_proactive_hints(
    session_id: str,
    workspace_key: str,
    user_query: str,
    *,
    turn_number: int | None = None,
) -> str:
    """根据用户最新问题生成 CCR 预展开提示（追加到 dynamic system）。"""
    try:
        from app.config import get_settings
        if not getattr(get_settings(), "CTX_TRACKER_ENABLED", True):
            return ""
    except Exception:
        return ""
    if not session_id or not user_query.strip() or not workspace_key:
        return ""
    recs = _get_tracker(session_id).analyze_query(
        user_query, current_turn=turn_number, workspace_key=workspace_key,
    )
    if not recs:
        return ""
    lines = ["\n## CCR 预展开建议", "以下归档可能与当前问题相关，需要细节时请 retrieve_context："]
    for r in recs:
        lines.append(
            f"- `hash={r.hash_key[:16]}...` — {r.reason}；"
            f'调用 retrieve_context(hash="{r.hash_key}")'
        )
    logger.info(
        "[CTX-TRACKER] proactive hints session={} count={}",
        session_id[:12], len(recs),
    )
    return "\n".join(lines) + "\n"
