"""anchor_crush 启发式压缩（Python port，非 Rust Kneedle）。"""

from __future__ import annotations

import json
import re
import statistics
from typing import Any

_JSON_TRAILING_COMMA = re.compile(r",\s*([}\]])")


def parse_json_tiered(content: str) -> tuple[Any | None, str]:
    """三层 JSON 解析降级：strict → relaxed（去尾逗号）→ text 路由。"""
    s = content.strip()
    if not s or s[0] not in ("{", "["):
        return None, "text"
    try:
        return json.loads(s), "strict"
    except json.JSONDecodeError:
        pass
    relaxed = _JSON_TRAILING_COMMA.sub(r"\1", s)
    try:
        return json.loads(relaxed), "relaxed"
    except json.JSONDecodeError:
        return None, "text"


from .compression_config import (
    FIRST_FRACTION,
    JSON_PROTECT_KEYS,
    LAST_FRACTION,
    LOSSLESS_MIN_SAVINGS,
    MAX_ITEMS_AFTER_CRUSH,
    MUST_KEEP_RE,
    VARIANCE_THRESHOLD,
)
from .context_compressor import CCR_SENTINEL_KEY

_GREP_RE = re.compile(r"^(?:\.{0,2}/)?[^\s:]+:\d+:", re.MULTILINE)


def _lossless_compact(content: str) -> tuple[str, bool]:
    s = content.strip()
    if not s or s[0] not in ("{", "["):
        return content, False
    data, tier = parse_json_tiered(s)
    if tier == "text" or data is None:
        return content, False
    compact = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    orig_b = len(s.encode("utf-8"))
    new_b = len(compact.encode("utf-8"))
    if new_b >= orig_b:
        return content, False
    if 1.0 - new_b / max(orig_b, 1) < LOSSLESS_MIN_SAVINGS:
        return content, False
    return compact, True


def _score_important_rows(lines: list[str]) -> list[tuple[int, float, str]]:
    lengths = [len(line) for line in lines if line.strip()]
    mean_len = statistics.mean(lengths) if lengths else 0.0
    stdev = statistics.pstdev(lengths) if len(lengths) > 1 else 0.0
    scored: list[tuple[int, float, str]] = []
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        score = 100.0 if MUST_KEEP_RE.search(line) else 0.0
        if stdev > 0 and abs(len(line) - mean_len) > VARIANCE_THRESHOLD * stdev:
            score += 10.0
        scored.append((i, score, line))
    scored.sort(key=lambda x: (-x[1], x[0]))
    return scored


def anchor_crush(content: str, ccr_hint: str | None = None) -> str:
    lines = content.split("\n")
    n = len(lines)
    if n <= MAX_ITEMS_AFTER_CRUSH:
        return content
    head_n = max(1, int(n * FIRST_FRACTION))
    tail_n = max(1, int(n * LAST_FRACTION))
    head = lines[:head_n]
    tail = lines[-tail_n:]
    head_tail_idx = set(range(head_n)) | set(range(n - tail_n, n))
    picked: list[str] = []
    for idx, _score, line in _score_important_rows(lines):
        if idx in head_tail_idx:
            continue
        picked.append(line)
        if len(head) + len(picked) + len(tail) >= MAX_ITEMS_AFTER_CRUSH:
            break
    omitted = n - len(head) - len(tail) - len(picked)
    body = list(head)
    if picked:
        body.append(f"--- Highlights ({len(picked)}) ---")
        body.extend(picked)
    if omitted > 0:
        body.append(f"... [{omitted} 行已省略] ...")
    body.extend(tail)
    if ccr_hint:
        body.append(ccr_hint)
    return "\n".join(body)


def crush_json_array(content: str, ccr_hint: str | None = None) -> str | None:
    data, tier = parse_json_tiered(content)
    if tier == "text" or data is None:
        return None
    if not isinstance(data, list):
        compact, ok = _lossless_compact(content)
        return compact if ok else None
    compact, ok = _lossless_compact(content)
    if ok:
        return compact
    n = len(data)
    if n <= 20:
        return None
    errors = [
        i for i, item in enumerate(data[: min(n, 500)])
        if isinstance(item, dict) and any(kw in str(item).lower() for kw in ("error", "fail", "exception"))
    ]
    keys = {
        k for item in data[: min(n, 100)] if isinstance(item, dict) for k in item if k not in JSON_PROTECT_KEYS
    }
    kept: list[Any] = list(data[:5])
    if errors:
        kept.append({"_pinned_errors": len(errors)})
    kept.extend(data[-5:])
    dropped = n - len(kept)
    if dropped > 0 and ccr_hint:
        kept.append({CCR_SENTINEL_KEY: ccr_hint})
    return json.dumps({"_total": n, "_fields": sorted(keys)[:20], "_sample": kept}, ensure_ascii=False)


def smart_crush(content: str, ccr_hint: str | None = None) -> tuple[str, str]:
    compact, ok = _lossless_compact(content)
    if ok:
        return compact, "lossless"
    s = content.strip()
    if s.startswith("["):
        crushed = crush_json_array(content, ccr_hint=ccr_hint)
        if crushed and crushed != content:
            return crushed, "json"
    crushed = anchor_crush(content, ccr_hint=ccr_hint)
    if crushed != content:
        return crushed, "anchor"
    return content, "passthrough"
