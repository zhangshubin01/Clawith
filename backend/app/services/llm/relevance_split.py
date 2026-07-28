"""search/log 相关性拆分: 只产出 KEEP/DROP 计划, 不负责 CCR marker。"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field


@dataclass
class RelevanceScore:
    score: float
    reason: str = ""
    matched_terms: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.score = max(0.0, min(1.0, self.score))


def build_relevance_query(user_query: str, tool_name: str = "", tool_args_text: str = "") -> str:
    """把用户意图和工具参数合成同一个信息需求。"""
    parts = [p.strip() for p in (user_query, tool_name, tool_args_text) if p and p.strip()]
    return "\n".join(parts)


def segment(content: str, *, window: int = 8, max_chars: int = 1200) -> list[str]:
    """保序分段; 保证 `"".join(segment(content)) == content`。"""
    lines = content.splitlines(keepends=True)
    if len(lines) <= 1:
        return [content] if content else []

    blocks: list[list[str]] = []
    cur: list[str] = []
    for line in lines:
        cur.append(line)
        if line.strip() == "":
            blocks.append(cur)
            cur = []
    if cur:
        blocks.append(cur)

    segments: list[str] = []
    for block in blocks:
        if len(block) <= window and sum(len(line) for line in block) <= max_chars:
            segments.append("".join(block))
            continue
        i = 0
        while i < len(block):
            j = min(i + window, len(block))
            while j < len(block) and block[j][:1] in (" ", "\t"):
                j += 1
            segments.append("".join(block[i:j]))
            i = j
    return segments


def _otsu_threshold(values: list[float]) -> float:
    xs = sorted(values)
    n = len(xs)
    total = sum(xs)
    w0 = 0.0
    sum0 = 0.0
    best_t = xs[0]
    best_var = -1.0
    for i in range(n - 1):
        w0 += 1
        sum0 += xs[i]
        w1 = n - w0
        m0 = sum0 / w0
        m1 = (total - sum0) / w1
        between = w0 * w1 * (m0 - m1) ** 2
        if between > best_var:
            best_var = between
            best_t = (xs[i] + xs[i + 1]) / 2.0
    return best_t


def adaptive_threshold(values: list[float], floor: float) -> float:
    if not values:
        return floor
    if len({round(v, 9) for v in values}) < 2:
        return floor
    return max(_otsu_threshold(values), floor)


class BM25Scorer:
    """零依赖 BM25; 适合精确 token、路径、ID、UUID。"""

    _TOKEN_PATTERN = re.compile(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
        r"|\b\d{4,}\b"
        r"|[a-zA-Z0-9_.-]+"
    )

    def __init__(self, *, k1: float = 1.5, b: float = 0.75, max_score: float = 10.0):
        self.k1 = k1
        self.b = b
        self.max_score = max_score

    def _tokenize(self, text: str) -> list[str]:
        return self._TOKEN_PATTERN.findall((text or "").lower())

    @staticmethod
    def _idf(term: str, doc_count: int, doc_freq: int) -> float:
        if doc_freq <= 0:
            return 0.0
        return math.log((doc_count - doc_freq + 0.5) / (doc_freq + 0.5) + 1.0)

    def _score_tokens(
        self,
        doc_tokens: list[str],
        query_tokens: list[str],
        *,
        avg_doc_len: float,
        idf_map: dict[str, float],
    ) -> tuple[float, list[str]]:
        if not doc_tokens or not query_tokens:
            return 0.0, []
        doc_freq = Counter(doc_tokens)
        query_freq = Counter(query_tokens)
        doc_len = len(doc_tokens)
        matched: list[str] = []
        score = 0.0
        for term, qf in query_freq.items():
            freq = doc_freq.get(term, 0)
            if not freq:
                continue
            matched.append(term)
            idf = idf_map.get(term, math.log(2.0))
            numerator = freq * (self.k1 + 1)
            denominator = freq + self.k1 * (1 - self.b + self.b * doc_len / max(avg_doc_len, 1.0))
            score += (idf * numerator / denominator) * qf
        return score, matched

    def score_batch(self, items: list[str], query: str) -> list[RelevanceScore]:
        query_tokens = self._tokenize(query)
        if not query_tokens:
            return [RelevanceScore(score=0.0, reason="empty query") for _ in items]
        all_tokens = [self._tokenize(item) for item in items]
        avg_len = sum(len(tokens) for tokens in all_tokens) / max(len(all_tokens), 1)
        doc_count = len(all_tokens)
        doc_freq_across: Counter[str] = Counter()
        for tokens in all_tokens:
            doc_freq_across.update(set(tokens))
        idf_map = {
            term: self._idf(term, doc_count, doc_freq_across[term])
            for term in set(query_tokens)
            if term in doc_freq_across
        }

        results: list[RelevanceScore] = []
        for tokens in all_tokens:
            raw, matched = self._score_tokens(
                tokens,
                query_tokens,
                avg_doc_len=avg_len,
                idf_map=idf_map,
            )
            normalized = min(1.0, raw / self.max_score)
            if any(len(term) >= 8 for term in matched):
                normalized = min(1.0, normalized + 0.3)
            results.append(
                RelevanceScore(
                    score=normalized,
                    reason=f"BM25: {len(matched)} terms" if matched else "BM25: no matches",
                    matched_terms=matched[:5],
                )
            )
        return results


def plan_relevance_split(
    content: str,
    query: str,
    scorer: BM25Scorer,
    *,
    threshold: float,
    adaptive: bool = True,
    window: int = 8,
    max_chars: int = 1200,
    max_records: int | None = None,
) -> list[tuple[bool, str]]:
    """按相关性返回有序 `(keep, text)` runs; 不压缩、不写 store。"""
    if not query.strip():
        return [(True, content)]
    segs = segment(content, window=window, max_chars=max_chars)
    if len(segs) < 2 or (max_records and len(segs) > max_records):
        return [(True, content)]

    scores = scorer.score_batch(segs, query)
    cut = adaptive_threshold([score.score for score in scores], threshold) if adaptive else threshold
    runs: list[tuple[bool, str]] = []
    for seg, score in zip(segs, scores, strict=True):
        keep = score.score >= cut
        if runs and runs[-1][0] == keep:
            runs[-1] = (keep, runs[-1][1] + seg)
        else:
            runs.append((keep, seg))
    return runs
