"""跨 prompt 历史 tool 结果 hydrate — 对齐运行时 Layer0/CCR。"""

from __future__ import annotations

from loguru import logger

from .compression_config import LAYER1_PROTECT_ROUNDS, is_tier1_strict
from .context_compressor import _est_tokens_str
from .emit_guarded import emit_guarded
from .truncate_caps import apply_cross_session_read_hints
from .tool_trim import _tool_token_budget
from .utils import truncate_messages_with_pair_integrity


def _tool_round_index(messages: list[dict]) -> dict[str, int]:
    """tool_call_id → 1-based tool 轮次。"""
    tc_round: dict[str, int] = {}
    round_no = 0
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            round_no += 1
            tc_id = tc.get("id", "")
            if tc_id:
                tc_round[tc_id] = round_no
    return tc_round


def _tool_name_for_id(messages: list[dict], tc_id: str) -> str:
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            if tc.get("id") == tc_id:
                return (tc.get("function") or {}).get("name", "") or ""
    return ""




def _est_tokens_messages(messages: list[dict], model_name: str) -> int:
    """dict 格式 history 的 token 估算（_est_tokens 仅 LLMMessage）。"""
    import json as _json

    total = 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, str) and c:
            total += _est_tokens_str(c, model_name)
        tcs = m.get("tool_calls")
        if tcs:
            total += _est_tokens_str(_json.dumps(tcs, default=str), model_name)
    return max(total, 1)


async def _offload_old_tools_in_dict(
    messages: list[dict],
    *,
    session_id: str,
    agent_id,
    ctx_path: str,
    model_name: str,
    protect_rounds: int = LAYER1_PROTECT_ROUNDS,
) -> tuple[list[dict], int]:
    """F2 丢弃前：原地压老轮 tool 结果（dict 格式），对齐 Layer1 可逆压缩。"""
    if not session_id or not messages:
        return messages, 0

    from .ccr_store import ccr_marker, store_entry

    tc_round = _tool_round_index(messages)
    current_round = max(tc_round.values()) if tc_round else 0
    out: list[dict] = []
    offload_count = 0

    for msg in messages:
        if msg.get("role") != "tool":
            out.append(msg)
            continue

        content = msg.get("content")
        if not isinstance(content, str) or not content:
            out.append(msg)
            continue
        if "<!-- ccr:" in content:
            out.append(msg)
            continue

        tc_id = msg.get("tool_call_id", "") or ""
        round_no = tc_round.get(tc_id, current_round)
        tool_name = _tool_name_for_id(messages, tc_id)
        if current_round - round_no < protect_rounds:
            out.append(msg)
            continue
        if is_tier1_strict(tool_name):
            out.append(msg)
            continue
        if len(content) < 512:
            out.append(msg)
            continue

        summary = f"[历史 tool 结果已归档 tool={tool_name or 'unknown'} round={round_no}]"
        before_tok = _est_tokens_str(content, model_name)
        h = await store_entry(
            session_id=session_id,
            agent_id=agent_id,
            content=content,
            tool_name=tool_name,
            path=ctx_path,
            original_tokens=before_tok,
            compressed_tokens=max(1, len(summary) // 4),
        )
        if not h:
            out.append(msg)
            continue
        body = emit_guarded(summary, ccr_marker(h), content, model_name, ctx_path=ctx_path)
        new_msg = dict(msg)
        new_msg["content"] = body
        out.append(new_msg)
        offload_count += 1

    if offload_count:
        logger.info("[CTX-HISTORY] F2 compress_before_drop count={} path={}", offload_count, ctx_path)
    return out, offload_count


async def _pretruncate_by_token_budget(
    messages: list[dict],
    *,
    ctx_window: int,
    model_name: str,
    ctx_path: str,
    session_id: str = "",
    agent_id=None,
) -> list[dict]:
    """F2：超 ctx_window 90% 时先压老轮 tool，仍超则无损丢弃前缀（全角色 offload + 边界桩）。"""
    if not messages or ctx_window <= 0:
        return messages
    orig_tokens = _est_tokens_messages(messages, model_name)
    max_tokens = int(ctx_window * 0.9)
    if orig_tokens <= max_tokens:
        return messages

    working = list(messages)
    if session_id:
        working, compressed_n = await _offload_old_tools_in_dict(
            working,
            session_id=session_id,
            agent_id=agent_id,
            ctx_path=ctx_path,
            model_name=model_name,
        )
        if compressed_n and _est_tokens_messages(working, model_name) <= max_tokens:
            logger.info(
                "[CTX-HISTORY] F2 pretruncate path={} tokens={}→{} msgs={} (compress_only)",
                ctx_path,
                orig_tokens,
                _est_tokens_messages(working, model_name),
                len(working),
            )
            return working

    best = working
    for limit in range(len(working), 0, -1):
        candidate = truncate_messages_with_pair_integrity(working, limit)
        if _est_tokens_messages(candidate, model_name) <= max_tokens:
            best = candidate
            break
    else:
        best = truncate_messages_with_pair_integrity(working, 1)

    if session_id and len(best) < len(working):
        from .ccr_offload import offload_truncated_prefix

        best, _ = await offload_truncated_prefix(
            working,
            best,
            session_id=session_id,
            agent_id=agent_id,
            ctx_path=ctx_path,
            model_name=model_name,
        )

    logger.info(
        "[CTX-HISTORY] F2 pretruncate path={} tokens={}→{} msgs={}→{}",
        ctx_path,
        orig_tokens,
        _est_tokens_messages(best, model_name),
        len(messages),
        len(best),
    )
    return best


async def _apply_history_read_lifecycle(
    messages: list[dict],
    *,
    session_id: str,
    agent_id,
    ctx_path: str,
    model_name: str,
) -> list[dict]:
    """F4：hydrate 后 stale/superseded read dedup（不重复 offload 老轮）。"""
    from app.config import get_settings
    from .read_lifecycle import ReadLifecycleManager

    if not getattr(get_settings(), "CTX_READ_LIFECYCLE_ENABLED", True):
        return messages
    mgr = ReadLifecycleManager()
    lr = await mgr.apply_async(
        messages,
        session_id=session_id,
        agent_id=agent_id,
        ctx_path=ctx_path,
        frozen_message_count=0,
        tools_available=True,
        model_name=model_name,
    )
    if lr.reads_stale or lr.reads_superseded:
        logger.info(
            "[CTX-HISTORY] F4 read_lc path={} stale={} superseded={} saved_bytes={}",
            ctx_path,
            lr.reads_stale,
            lr.reads_superseded,
            lr.bytes_before - lr.bytes_after,
        )
    return lr.messages


async def hydrate_history_tool_results(
    messages: list[dict],
    *,
    session_id: str,
    agent_id,
    ctx_path: str,
    ctx_window: int = 100_000,
    protect_rounds: int = LAYER1_PROTECT_ROUNDS,
    model_name: str = "",
) -> list[dict]:
    """convert 之后、truncate 之前：F2 预截 → tool 压缩/CCR → F4 read_lc → C2 hint。"""
    if not messages:
        return messages
    if not session_id:
        return apply_cross_session_read_hints(messages, ctx_path=ctx_path)

    messages = await _pretruncate_by_token_budget(
        messages,
        ctx_window=ctx_window,
        model_name=model_name,
        ctx_path=ctx_path,
        session_id=session_id,
        agent_id=agent_id,
    )

    from .caller import _guarded_compress_with_ccr

    tc_round = _tool_round_index(messages)
    current_round = max(tc_round.values()) if tc_round else 0
    out: list[dict] = []

    for msg in messages:
        if msg.get("role") != "tool":
            out.append(msg)
            continue

        content = msg.get("content")
        if not isinstance(content, str) or not content:
            out.append(msg)
            continue
        if "<!-- ccr:" in content:
            out.append(msg)
            continue

        tc_id = msg.get("tool_call_id", "") or ""
        round_no = tc_round.get(tc_id, current_round)
        tool_name = _tool_name_for_id(messages, tc_id)
        age = current_round - round_no

        if age >= protect_rounds and len(content) >= 512:
            from .ccr_store import ccr_marker, store_entry

            summary = f"[历史 tool 结果已归档 tool={tool_name or 'unknown'} round={round_no}]"
            before_tok = _est_tokens_str(content, model_name)
            h = await store_entry(
                session_id=session_id,
                agent_id=agent_id,
                content=content,
                tool_name=tool_name,
                path=ctx_path,
                original_tokens=before_tok,
                compressed_tokens=max(1, len(summary) // 4),
            )
            if h:
                body = emit_guarded(summary, ccr_marker(h), content, model_name, ctx_path=ctx_path)
                new_msg = dict(msg)
                new_msg["content"] = body
                out.append(new_msg)
                logger.info(
                    "[CTX-HISTORY] path={} tool={} strategy=offload_old round={} chars={}→{}",
                    ctx_path, tool_name, round_no, len(content), len(body),
                )
                continue

        budget = _tool_token_budget(tool_name, ctx_window)
        est = _est_tokens_str(content, model_name)
        if est <= budget:
            out.append(msg)
            continue

        compressed = await _guarded_compress_with_ccr(
            content,
            tool_name,
            budget,
            model_name,
            ctx_path,
            session_id,
            agent_id,
            tools_available=True,
            ctx_window=ctx_window,
        )
        new_msg = dict(msg)
        new_msg["content"] = compressed
        out.append(new_msg)
        logger.info(
            "[CTX-HISTORY] path={} tool={} strategy=compress round={} tokens={}→{}",
            ctx_path,
            tool_name,
            round_no,
            est,
            _est_tokens_str(compressed, model_name),
        )

    out = await _apply_history_read_lifecycle(
        out,
        session_id=session_id,
        agent_id=agent_id,
        ctx_path=ctx_path,
        model_name=model_name,
    )
    return apply_cross_session_read_hints(out, ctx_path=ctx_path)
