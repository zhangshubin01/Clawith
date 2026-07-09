"""Layer0 统一压缩路由 — compress_one 真源。"""

from __future__ import annotations

import re

from loguru import logger

from .compression_config import (
    MIN_SIZE_BYTES,
    PROTECT_ERROR_CHARS,
    adaptive_min_ratio,
    exclude_tier,
    reduced,
    tier1_session_pressure_threshold,
)
from .compression_result import CompressionResult, Lossiness, unchanged
from .context_compressor import _IMPORTANCE_RE, _detect, _dispatch, _est_tokens_str, _log, _search
from .emit_guarded import emit_guarded
from .lossless_compaction import compact_lossless
from .relevance_split import BM25Scorer, build_relevance_query, plan_relevance_split
from .smart_crusher import smart_crush
_COMPRESS_MARKER = "<!-- ctx:trimmed -->"
_TOOL_TYPE_ROUTE: dict[str, str] = {
    "list_files": "list",
    "list_dir": "list",
    "list_focus_items": "list",
    "read_file": "code",
    "read_document": "code",
    "search_files": "search",
    "search_file": "search",
    "search_codebase": "search",
    "search_symbol": "search",
    "search_clawhub": "search",
    "run_in_terminal": "log",
    "execute_command": "log",
    "execute_code": "log",
    "execute_code_e2b": "log",
    "git_diff": "diff",
}


def _list_head_tail(content: str, head: int = 40, tail: int = 20) -> str:
    lines = content.split("\n")
    n = len(lines)
    if n <= head + tail:
        return content
    omitted = n - head - tail
    body = lines[:head] + [f"... [{omitted} 行已省略，共 {n} 行；完整列表见 CCR retrieve] ..."] + lines[-tail:]
    return "\n".join(body)

_GREP_RE = re.compile(r"^(?:\.{0,2}/)?[^\s:]+:\d+:", re.MULTILINE)
_SEARCH_FAITHFUL_CAP_LINES = 500


def _has_error_keywords(content: str) -> bool:
    return bool(_IMPORTANCE_RE.search(content))


def is_retrieved(content: str) -> bool:
    return isinstance(content, str) and "<!-- ccr:retrieved -->" in content


def _retrieve_tool_available(tools: list[dict] | None, messages: list | None = None) -> bool:
    try:
        from app.services.llm.ccr_store import RETRIEVE_CONTEXT_TOOL_NAME, messages_have_ccr
    except Exception:
        return False
    if any((t.get("function") or {}).get("name") == RETRIEVE_CONTEXT_TOOL_NAME for t in (tools or [])):
        return True
    if messages and messages_have_ccr(messages):
        return True
    return False


def _faithful_search(content: str) -> str | None:
    """grep/rg 形态且未超 cap → 字节原样。"""
    lines = content.strip().split("\n")
    if len(lines) > _SEARCH_FAITHFUL_CAP_LINES:
        return None
    head = lines[:10]
    if sum(1 for line in head if _GREP_RE.search(line)) < max(len(head) * 0.5, 2):
        return None
    return content


def _route(tool_name: str, content: str) -> str:
    detected = _detect(content)
    if detected == "json":
        return "json"
    return _TOOL_TYPE_ROUTE.get(tool_name) or detected


def _excluded_lossless(tool_name: str, content_type: str, result: str, model_name: str, path: str) -> str:
    # Tier2：优先尝试 search/log/diff/text 的 lossless compact
    if content_type not in {"search", "log", "diff", "text"}:
        logger.debug("[CTX-ROUTER] path={} tool={} excluded", path or "unknown", tool_name)
        return result
    compacted = compact_lossless(result, content_type)
    if compacted != result and _est_tokens_str(compacted, model_name) < _est_tokens_str(result, model_name):
        logger.debug(
            "[CTX-ROUTER] excluded-lossless tool={} type={} chars={}→{}",
            tool_name, content_type, len(result), len(compacted),
        )
        return compacted
    logger.debug("[CTX-ROUTER] path={} tool={} excluded lossless_no_gain", path or "unknown", tool_name)
    return result


def _compress_relevance_split(
    result: str,
    *,
    content_type: str,
    tool_name: str,
    user_query: str,
    tool_args_text: str,
    model_name: str,
) -> str | None:
    try:
        from app.config import get_settings

        settings = get_settings()
        if not getattr(settings, "CTX_RELEVANCE_SPLIT_ENABLED", True):
            return None
        query = build_relevance_query(user_query, tool_name, tool_args_text)
        if not query.strip():
            return None
        runs = plan_relevance_split(
            result,
            query,
            BM25Scorer(),
            threshold=float(getattr(settings, "CTX_RELEVANCE_THRESHOLD", 0.25)),
            adaptive=bool(getattr(settings, "CTX_RELEVANCE_ADAPTIVE_THRESHOLD", True)),
            max_records=int(getattr(settings, "CTX_RELEVANCE_MAX_RECORDS", 200)),
        )
        if len(runs) < 2 or all(keep for keep, _ in runs):
            return None
        parts: list[str] = []
        dropped = 0
        for keep, chunk in runs:
            if keep:
                parts.append(chunk)
                continue
            dropped += 1
            low = _search(chunk) if content_type == "search" else _log(chunk)
            compacted = compact_lossless(low, content_type)
            parts.append(compacted if _est_tokens_str(compacted, model_name) < _est_tokens_str(low, model_name) else low)
        candidate = "\n".join(
            [
                "<!-- ctx:trimmed -->",
                f"[相关性拆分：保留匹配片段，压缩低相关片段 {dropped} 段；完整原文见 CCR retrieve]",
                *parts,
            ]
        )
        if _est_tokens_str(candidate, model_name) >= _est_tokens_str(result, model_name):
            return None
        logger.debug(
            "[CTX-ROUTER] relevance_split tool={} type={} runs={} dropped={} chars={}→{}",
            tool_name, content_type, len(runs), dropped, len(result), len(candidate),
        )
        return candidate
    except Exception as e:
        logger.warning("[CTX-ROUTER] relevance_split failed tool={} err={}", tool_name, e)
        return None


def compress_one_result(
    result: str,
    *,
    tool_name: str = "",
    budget_tokens: int = 0,
    model_name: str = "",
    path: str = "",
    ctx_window: int = 100000,
    pressure: float = 0.0,
    session_pressure: float = 0.0,
    user_query: str = "",
    tool_args_text: str = "",
) -> CompressionResult:
    """同步 Layer0 压缩，返回结构化结果供 CCR gate 使用。"""
    before = _est_tokens_str(result, model_name)
    if _COMPRESS_MARKER in result or is_retrieved(result):
        logger.info("[CTX-ROUTER] skip tool={} path={} reason=already_marked tokens={}", tool_name, path or "unknown", before)
        return unchanged(result, strategy="already_marked", tokens=before)

    content_type = _route(tool_name, result)
    tier = exclude_tier(tool_name)
    session_p = session_pressure or 0.0
    tier1_thr = tier1_session_pressure_threshold()

    if tier == 2:
        out = _excluded_lossless(tool_name, content_type, result, model_name, path)
        after = _est_tokens_str(out, model_name)
        if out != result and after < before:
            logger.info(
                "[CTX-ROUTER] compress tool={} path={} strategy=tier2_lossless tokens={}→{}",
                tool_name, path or "unknown", before, after,
            )
            return CompressionResult(
                content=out, changed=True, lossiness=Lossiness.LOSSLESS, recoverable=False,
                strategy="tier2_lossless", original_tokens=before, final_tokens=after,
            )
        if before <= budget_tokens:
            logger.info(
                "[CTX-ROUTER] skip tool={} path={} reason=tier2_under_budget tokens={}",
                tool_name, path or "unknown", before,
            )
            return unchanged(result, strategy="tier2_under_budget", tokens=before)
        logger.info(
            "[CTX-ROUTER] tier2 over_budget tool={} path={} tokens={} budget={} — fallthrough",
            tool_name, path or "unknown", before, budget_tokens,
        )
    elif tier == 1:
        if before <= budget_tokens and session_p < tier1_thr:
            logger.info(
                "[CTX-ROUTER] skip tool={} path={} reason=tier1_verbatim tokens={} session_p={:.2f}",
                tool_name, path or "unknown", before, session_p,
            )
            return unchanged(result, strategy="tier1_verbatim", tokens=before)
        if session_p < tier1_thr:
            logger.info(
                "[CTX-ROUTER] skip tool={} path={} reason=tier1_low_pressure tokens={} session_p={:.2f}<{:.2f}",
                tool_name, path or "unknown", before, session_p, tier1_thr,
            )
            return unchanged(result, strategy="tier1_low_pressure", tokens=before)
        logger.info(
            "[CTX-ROUTER] tier1 pressure compress tool={} path={} tokens={} session_p={:.2f}",
            tool_name, path or "unknown", before, session_p,
        )

    try:
        from app.config import get_settings
        if get_settings().CTX_LOSSLESS_ONLY:
            from .smart_crusher import _lossless_compact
            compact, ok = _lossless_compact(result)
            out = compact if ok else result
            after = _est_tokens_str(out, model_name)
            if out != result and after < before:
                return CompressionResult(
                    content=out, changed=True, lossiness=Lossiness.LOSSLESS, recoverable=False,
                    strategy="lossless_only", original_tokens=before, final_tokens=after,
                )
            return unchanged(result, strategy="lossless_only", tokens=before)
    except Exception as e:
        logger.warning("[CTX-ROUTER] lossless_only check failed err={} action=continue_lossy", e)

    if _has_error_keywords(result) and len(result) <= PROTECT_ERROR_CHARS:
        logger.info("[CTX-ROUTER] skip tool={} path={} reason=protect_error tokens={}", tool_name, path or "unknown", before)
        return unchanged(result, strategy="protect_error", tokens=before)

    if content_type in {"search", "log", "diff"} and len(result) >= MIN_SIZE_BYTES and before > budget_tokens and session_p >= 0.60:
        compacted = compact_lossless(result, content_type)
        if compacted != result and _est_tokens_str(compacted, model_name) < before:
            after = _est_tokens_str(compacted, model_name)
            logger.info(
                "[CTX-ROUTER] compress tool={} path={} strategy=lossless_pre_faithful tokens={}→{}",
                tool_name, path or "unknown", before, after,
            )
            return CompressionResult(
                content=compacted, changed=True, lossiness=Lossiness.LOSSLESS, recoverable=False,
                strategy="lossless_pre_faithful", original_tokens=before, final_tokens=after,
            )

    if content_type == "search":
        faithful = _faithful_search(result)
        if faithful is not None:
            logger.info("[CTX-ROUTER] skip tool={} path={} reason=faithful_search tokens={}", tool_name, path or "unknown", before)
            return unchanged(result, strategy="faithful_search", tokens=before)

    if content_type in {"search", "log", "diff"}:
        compacted = compact_lossless(result, content_type)
        if compacted != result and _est_tokens_str(compacted, model_name) < before:
            after = _est_tokens_str(compacted, model_name)
            logger.info(
                "[CTX-ROUTER] compress tool={} path={} strategy=lossless tokens={}→{}",
                tool_name, path or "unknown", before, after,
            )
            return CompressionResult(
                content=compacted, changed=True, lossiness=Lossiness.LOSSLESS, recoverable=False,
                strategy="lossless_compact", original_tokens=before, final_tokens=after,
            )

    if content_type in {"search", "log"}:
        relevance = _compress_relevance_split(
            result, content_type=content_type, tool_name=tool_name,
            user_query=user_query, tool_args_text=tool_args_text, model_name=model_name,
        )
        if relevance is not None:
            after = _est_tokens_str(relevance, model_name)
            logger.info(
                "[CTX-ROUTER] compress tool={} path={} strategy=relevance_split tokens={}→{}",
                tool_name, path or "unknown", before, after,
            )
            return CompressionResult(
                content=relevance, changed=True, lossiness=Lossiness.RELEVANCE_SPLIT, recoverable=True,
                strategy="relevance_split", original_tokens=before, final_tokens=after,
            )

    strategy_name = content_type
    if content_type == "search":
        compressed = _search(result)
    elif content_type == "list":
        compressed = _list_head_tail(result)
        strategy_name = "list_head_tail"
    elif content_type == "text":
        logger.info("[CTX-ROUTER] skip tool={} path={} reason=text_passthrough tokens={}", tool_name, path or "unknown", before)
        return unchanged(result, strategy="text_passthrough", tokens=before)
    elif content_type == "json":
        crushed, crush_strategy = smart_crush(result)
        if crush_strategy == "lossless":
            after = _est_tokens_str(crushed, model_name)
            if after < before:
                return CompressionResult(
                    content=crushed, changed=True, lossiness=Lossiness.LOSSLESS, recoverable=False,
                    strategy="json_lossless", original_tokens=before, final_tokens=after,
                )
            return unchanged(result, strategy="json_lossless_no_gain", tokens=before)
        compressed = crushed if crush_strategy != "passthrough" else _dispatch(result, "json")
        strategy_name = f"json_{crush_strategy}"
    elif content_type == "log":
        compressed = _log(result)
    else:
        crushed, crush_strategy = smart_crush(result)
        compressed = crushed if crush_strategy != "passthrough" else _dispatch(result, content_type)
        strategy_name = crush_strategy

    after = _est_tokens_str(compressed, model_name)
    if after >= before:
        logger.info("[CTX] path={} never_worse tool={} tokens={}→{}", path or "unknown", tool_name, before, after)
        return unchanged(result, strategy="never_worse", tokens=before)

    min_ratio = adaptive_min_ratio(pressure or (budget_tokens / max(ctx_window, 1)))
    if reduced(len(result), len(compressed)) > min_ratio:
        logger.info(
            "[CTX-ROUTER] skip tool={} path={} reason=marginal_reject ratio={:.2f} min={:.2f} tokens={}",
            tool_name, path or "unknown", reduced(len(result), len(compressed)), min_ratio, before,
        )
        return unchanged(result, strategy="marginal_reject", tokens=before)

    body = emit_guarded(compressed, None, result, model_name, ctx_path=path)
    if body == result:
        return unchanged(result, strategy="emit_never_worse", tokens=before)

    if content_type == "json" and result.strip().startswith(("{", "[")):
        final_body = body
    elif _COMPRESS_MARKER in body:
        final_body = body
    else:
        final_body = _COMPRESS_MARKER + "\n" + body

    final_after = _est_tokens_str(final_body, model_name)
    lossiness = Lossiness.SUMMARY if content_type == "json" else Lossiness.TAIL
    if content_type == "search":
        lossiness = Lossiness.SUMMARY
    logger.info(
        "[CTX-ROUTER] compress tool={} path={} strategy={} tokens={}→{} lossiness={}",
        tool_name, path or "unknown", strategy_name, before, final_after, lossiness,
    )
    return CompressionResult(
        content=final_body, changed=True, lossiness=lossiness, recoverable=True,
        strategy=strategy_name, original_tokens=before, final_tokens=final_after,
    )


def compress_one(
    result: str,
    *,
    tool_name: str = "",
    budget_tokens: int = 0,
    model_name: str = "",
    path: str = "",
    ctx_window: int = 100000,
    pressure: float = 0.0,
    session_pressure: float = 0.0,
    user_query: str = "",
    tool_args_text: str = "",
) -> str:
    """同步 Layer0 压缩。CCR store 由 caller._guarded_compress_with_ccr 负责。"""
    return compress_one_result(
        result,
        tool_name=tool_name,
        budget_tokens=budget_tokens,
        model_name=model_name,
        path=path,
        ctx_window=ctx_window,
        pressure=pressure,
        session_pressure=session_pressure,
        user_query=user_query,
        tool_args_text=tool_args_text,
    ).content
