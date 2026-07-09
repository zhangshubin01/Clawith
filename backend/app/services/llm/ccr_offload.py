"""Layer1/F2 历史 tool 块 offload — 老轮次摘要 + CCR marker。"""

from __future__ import annotations

import json

from loguru import logger

from .compression_config import LAYER1_PROTECT_ROUNDS, is_tier1_strict
from .ccr_store import ccr_marker, store_entry
from .context_compressor import _est_tokens_str
from .emit_guarded import emit_guarded


def _count_tool_rounds(messages: list) -> int:
    rounds = 0
    for msg in messages:
        role = getattr(msg, "role", None)
        if role == "assistant" and getattr(msg, "tool_calls", None):
            rounds += 1
    return rounds


def _tool_name_for_dropped_msg(messages: list[dict], msg: dict) -> str:
    tc_id = msg.get("tool_call_id", "") or ""
    if not tc_id:
        return ""
    for am in messages:
        if am.get("role") != "assistant":
            continue
        for tc in am.get("tool_calls") or []:
            if tc.get("id") == tc_id:
                return (tc.get("function") or {}).get("name", "") or ""
    return ""


def _content_as_str(content) -> str:
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "\n".join(parts)
    return str(content)


async def offload_old_tool_messages(
    messages: list,
    *,
    session_id: str,
    agent_id,
    ctx_path: str,
    model_name: str = "",
    protect_rounds: int = LAYER1_PROTECT_ROUNDS,
    tools_available: bool = True,
) -> tuple[list, int]:
    """最近 protect_rounds 轮 tool 结果 verbatim；更早的可压 tool 块 offload。返回 (messages, offload_count)。"""
    if not session_id or not messages or not tools_available:
        return messages, 0

    tool_round = 0
    tc_round: dict[str, int] = {}
    for msg in messages:
        if getattr(msg, "role", None) != "assistant":
            continue
        for tc in getattr(msg, "tool_calls", None) or []:
            tool_round += 1
            tc_id = tc.get("id", "")
            if tc_id:
                tc_round[tc_id] = tool_round

    current_round = tool_round
    from app.services.llm.client import LLMMessage
    offload_count = 0

    for i, msg in enumerate(messages):
        if getattr(msg, "role", None) != "tool":
            continue
        content = getattr(msg, "content", None)
        if not isinstance(content, str) or not content:
            continue
        if "<!-- ccr:" in content or "<!-- ccr:retrieved -->" in content:
            continue
        tc_id = getattr(msg, "tool_call_id", None) or ""
        round_no = tc_round.get(tc_id, current_round)
        if current_round - round_no < protect_rounds:
            continue

        tool_name = ""
        for am in messages:
            if getattr(am, "role", None) != "assistant":
                continue
            for tc in getattr(am, "tool_calls", None) or []:
                if tc.get("id") == tc_id:
                    tool_name = (tc.get("function") or {}).get("name", "")
        if is_tier1_strict(tool_name):
            continue
        if len(content) < 512:
            continue

        summary = f"[历史 tool 结果已归档 tool={tool_name or 'unknown'} round={round_no}]"
        before = _est_tokens_str(content, model_name)
        h = await store_entry(
            session_id=session_id,
            agent_id=agent_id,
            content=content,
            tool_name=tool_name,
            path=ctx_path,
            original_tokens=before,
            compressed_tokens=max(1, len(summary) // 4),
        )
        if not h:
            continue
        body = emit_guarded(summary, ccr_marker(h), content, model_name, ctx_path=ctx_path)
        messages[i] = LLMMessage(role="tool", content=body, tool_call_id=tc_id)
        offload_count += 1
        logger.debug("[CTX-OFFLOAD] layer=1 round={} tool={} path={}", round_no, tool_name, ctx_path)

    if offload_count:
        logger.info("[CTX-CCR] offload layer=1 count={} path={}", offload_count, ctx_path)
    return messages, offload_count


async def offload_truncated_prefix(
    all_messages: list[dict],
    kept_messages: list[dict],
    *,
    session_id: str,
    agent_id,
    ctx_path: str,
    model_name: str = "",
) -> tuple[list[dict], int]:
    """F2 无损丢弃：被截掉的前缀全角色 offload 到 CCR，边界注入可 discover 的 marker。"""
    if not session_id or len(all_messages) <= len(kept_messages):
        return kept_messages, 0
    dropped = all_messages[: len(all_messages) - len(kept_messages)]
    offload_count = 0
    markers: list[str] = []
    for msg in dropped:
        role = msg.get("role", "")
        if role not in ("user", "assistant", "tool"):
            continue
        content = _content_as_str(msg.get("content"))
        if not content.strip() or "<!-- ccr:" in content:
            continue
        min_len = 512 if role == "tool" else 8
        if len(content) < min_len:
            continue

        tool_name = "history_truncated"
        if role == "tool":
            tool_name = _tool_name_for_dropped_msg(all_messages, msg) or "history_truncated"
        elif role == "user":
            tool_name = "history_user"
        elif role == "assistant":
            tool_name = "history_assistant"
            if msg.get("tool_calls"):
                tool_name = "history_assistant_tools"

        payload = content
        if role == "assistant" and msg.get("tool_calls"):
            payload = json.dumps(
                {"content": content, "tool_calls": msg.get("tool_calls")},
                ensure_ascii=False,
                default=str,
            )

        h = await store_entry(
            session_id=session_id,
            agent_id=agent_id,
            content=payload,
            tool_name=tool_name,
            path=ctx_path,
            original_tokens=_est_tokens_str(payload, model_name),
            compressed_tokens=32,
        )
        if not h:
            continue
        offload_count += 1
        markers.append(ccr_marker(h))
        logger.debug("[CTX-OFFLOAD] f2_prefix role={} hash={} path={}", role, h[:12], ctx_path)

    if offload_count:
        logger.info("[CTX-CCR] offload f2_prefix count={} path={}", offload_count, ctx_path)
    if markers:
        kept_messages = _inject_f2_boundary_markers(kept_messages, markers)
    return kept_messages, offload_count


def _role_any(m) -> str:
    if isinstance(m, dict):
        return m.get("role", "") or ""
    return getattr(m, "role", "") or ""


def _content_any(m):
    if isinstance(m, dict):
        return m.get("content")
    return getattr(m, "content", None)


def _tool_calls_any(m):
    if isinstance(m, dict):
        return m.get("tool_calls")
    return getattr(m, "tool_calls", None)


def _tool_call_id_any(m) -> str:
    if isinstance(m, dict):
        return m.get("tool_call_id") or ""
    return getattr(m, "tool_call_id", None) or ""


async def offload_dropped_messages(
    dropped: list,
    *,
    session_id: str,
    agent_id,
    ctx_path: str,
    model_name: str = "",
) -> tuple[list[str], int]:
    """Wave6 边界折叠：调用方**显式给定**要移除的中段消息集合 → 全角色 offload 到 CCR。

    与 `offload_truncated_prefix` 的关键区别（P0-2/7 修正）：
      - 几何为「移中段」，调用方已按 tool 轮对齐算好 dropped，本函数**不做**前缀/尾部推断；
      - **不注入**任何 marker 到保留消息（P0-1）——由调用方在折叠边界 append 独立摘要消息。
    dropped 元素可为 `LLMMessage` 或 dict（双适配）。返回 (ccr_markers, offload_count)。
    """
    if not session_id or not dropped:
        return [], 0

    # 从 dropped 内部建立 tool_call_id → tool_name 映射（整轮折叠时 assistant 也在 dropped 中）
    id2name: dict[str, str] = {}
    for m in dropped:
        if _role_any(m) != "assistant":
            continue
        for tc in _tool_calls_any(m) or []:
            if isinstance(tc, dict):
                id2name[tc.get("id", "") or ""] = (tc.get("function") or {}).get("name", "") or ""

    markers: list[str] = []
    offload_count = 0
    for msg in dropped:
        role = _role_any(msg)
        if role not in ("user", "assistant", "tool"):
            continue
        content = _content_as_str(_content_any(msg))
        if not content.strip() or "<!-- ccr:" in content:
            continue
        min_len = 512 if role == "tool" else 8
        if len(content) < min_len:
            continue

        if role == "tool":
            tool_name = id2name.get(_tool_call_id_any(msg), "") or "history_truncated"
        elif role == "user":
            tool_name = "history_user"
        else:
            tool_name = "history_assistant_tools" if _tool_calls_any(msg) else "history_assistant"

        payload = content
        tcs = _tool_calls_any(msg)
        if role == "assistant" and tcs:
            payload = json.dumps(
                {"content": content, "tool_calls": tcs},
                ensure_ascii=False,
                default=str,
            )

        h = await store_entry(
            session_id=session_id,
            agent_id=agent_id,
            content=payload,
            tool_name=tool_name,
            path=ctx_path,
            original_tokens=_est_tokens_str(payload, model_name),
            compressed_tokens=32,
        )
        if not h:
            continue
        offload_count += 1
        markers.append(ccr_marker(h))
        logger.debug("[CTX-OFFLOAD] fold_mid role={} hash={} path={}", role, h[:12], ctx_path)

    if offload_count:
        logger.info("[CTX-CCR] offload fold_mid count={} path={}", offload_count, ctx_path)
    return markers, offload_count


def _inject_f2_boundary_markers(kept_messages: list[dict], markers: list[str]) -> list[dict]:
    """F2：向保留段边界注入可 discover 的 CCR marker 摘要。"""
    if not markers:
        return kept_messages
    note = "[历史截断，可用 retrieve_context 还原以下归档]\n" + "\n".join(markers)
    kept = list(kept_messages)
    for i, msg in enumerate(kept):
        if msg.get("role") not in ("user", "assistant"):
            continue
        content = msg.get("content")
        if not isinstance(content, str):
            content = _content_as_str(content)
        if note in content:
            return kept
        new_msg = dict(msg)
        new_msg["content"] = f"{note}\n\n{content}" if content else note
        kept[i] = new_msg
        return kept
    return [{"role": "user", "content": note}] + kept
