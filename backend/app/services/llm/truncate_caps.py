"""CTX rtk 风格 caps：never_worse 门控、路径灰度、跨 load 重复 read hint。"""

from __future__ import annotations

import json
import re

from loguru import logger

from .context_compressor import _est_tokens_str

_CCR_RE = re.compile(r"<!-- ccr:([a-f0-9]{64}) -->")


def _parse_paths_csv(raw: str) -> set[str]:
    return {p.strip() for p in (raw or "").split(",") if p.strip()}


def is_rtk_invariant_enabled(ctx_path: str) -> bool:
    """CTX_RTK_INVARIANT_PATHS 灰度：空=关闭。"""
    from app.config import get_settings

    paths = _parse_paths_csv(getattr(get_settings(), "CTX_RTK_INVARIANT_PATHS", "") or "")
    key = (ctx_path or "history").strip()
    return bool(paths) and key in paths


def never_worse(
    compressed: str,
    hint: str | None,
    original: str,
    model_name: str = "",
    *,
    ctx_path: str = "",
) -> str:
    """有损输出必须比原文省 token，否则回退原文。"""
    body = f"{hint}\n{compressed}" if hint else compressed
    if _est_tokens_str(body, model_name) >= _est_tokens_str(original, model_name):
        if body != original and is_rtk_invariant_enabled(ctx_path):
            logger.info(
                "[CTX-RTK] lossy_unrecoverable path={} orig_chars={} out_chars={}",
                ctx_path,
                len(original),
                len(body),
            )
        return original
    return body


def _tool_call_name_for_id(messages: list[dict], tool_call_id: str) -> str:
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            if tc.get("id") == tool_call_id:
                fn = tc.get("function") or {}
                return str(fn.get("name") or "")
    return ""


def _read_path_from_tool_call(messages: list[dict], tool_call_id: str) -> str:
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            if tc.get("id") != tool_call_id:
                continue
            fn = tc.get("function") or {}
            if fn.get("name") not in ("read_file", "read_text_file"):
                return ""
            try:
                args = fn.get("arguments") or "{}"
                parsed = json.loads(args) if isinstance(args, str) else args
                return str((parsed or {}).get("path") or (parsed or {}).get("file") or "")
            except Exception:
                return ""
    return ""


def apply_cross_session_read_hints(messages: list[dict], *, ctx_path: str) -> list[dict]:
    """C2：history load 链重复 read 同 path 追加 tail hint。"""
    if not is_rtk_invariant_enabled(ctx_path) or not messages:
        return messages

    seen_paths: dict[str, str] = {}
    out: list[dict] = []

    for msg in messages:
        if msg.get("role") != "tool":
            out.append(msg)
            continue
        content = str(msg.get("content") or "")
        tc_id = str(msg.get("tool_call_id") or "")
        tool_name = _tool_call_name_for_id(messages, tc_id)
        if tool_name not in ("read_file", "read_text_file"):
            out.append(msg)
            continue
        path_key = _read_path_from_tool_call(messages, tc_id)
        if not path_key:
            out.append(msg)
            continue
        m = _CCR_RE.search(content)
        ccr_hash = m.group(1) if m else ""
        if path_key in seen_paths:
            hint_hash = ccr_hash or seen_paths[path_key]
            if hint_hash and hint_hash not in content:
                hint = f"\n[勿重复 read `{path_key}`；可用 retrieve_context hash={hint_hash[:12]}…]"
                new_msg = dict(msg)
                new_msg["content"] = content + hint
                logger.info("[CTX-RTK] read_tail_hint path={} file={}", ctx_path, path_key[:80])
                out.append(new_msg)
                continue
        if ccr_hash:
            seen_paths[path_key] = ccr_hash
        out.append(msg)
    return out
