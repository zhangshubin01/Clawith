"""Wave6 消息三区 + 边界折叠 + 前缀缓存追踪（§15 Cache-Safe Compaction）。

背景（round97 cache 断崖根因）：旧 `offload_old_tool_messages` 每轮对历史中段 tool
消息**原地改写**（scatter），使 provider prefix cache 从最早改动点起全部失效
（cache_read 98816→25472，-74%）。headroom 定性：**Passthrough is sacred** —
压缩只动 live zone，frozen 前缀字节必须逐轮不变。

本模块（新增文件 ≤1，聚合三件事，原则 1 DRY）：
  - `compute_zones` / `MessageZones`：把消息切成 frozen / compressible / live 三区。
  - `PrefixCacheTracker`：混合推导 frozen_count（稳定性哈希 + cache_read 校验 + 断崖 shrink）
    + overlay（重放上轮已发送前缀对象，杜绝任何字节漂移）。
  - `reactive_fold_messages`：高水位触发的**一次性**边界折叠 —— 仅把 frozen 与 live 之间的
    compressible 中段按 tool 轮整段移除并 offload 到 CCR，在边界 append 一条**独立**摘要消息
    （绝不改写 frozen 前缀，P0-1），带 high/low 迟滞防 ping-pong。

Tier1 / soul·memory 守护（原则 4）：折叠按轮判定，整轮含 Tier1（read/write/edit/retrieve）
结果的轮不移除；soul/memory 通常经 read_file/read_document（Tier1）读入，已被 Tier1 守卫覆盖。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from loguru import logger

from .compression_config import is_tier1_strict


# ────────────────────────── 消息适配（LLMMessage / dict 双支持）──────────────────────────

def _role(m: Any) -> str:
    if isinstance(m, dict):
        return m.get("role", "") or ""
    return getattr(m, "role", "") or ""


def _content(m: Any) -> Any:
    if isinstance(m, dict):
        return m.get("content")
    return getattr(m, "content", None)


def _tool_calls(m: Any) -> list | None:
    if isinstance(m, dict):
        return m.get("tool_calls")
    return getattr(m, "tool_calls", None)


def _tool_call_id(m: Any) -> str:
    if isinstance(m, dict):
        return m.get("tool_call_id") or ""
    return getattr(m, "tool_call_id", None) or ""


def _dynamic_content(m: Any) -> str:
    if isinstance(m, dict):
        return m.get("dynamic_content") or ""
    return getattr(m, "dynamic_content", None) or ""


def _content_to_str(content: Any) -> str:
    """把 str / vision-list / None 统一成用于签名与 token 估算的文本。"""
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


def _msg_signature(m: Any) -> str:
    """消息稳定签名 —— 用于前缀稳定性哈希比对。

    覆盖真正影响发送字节的字段：role、content、tool_calls、tool_call_id、system 的
    dynamic_content（`LLMMessage.to_openai_format` 会把它并入 system content）。
    """
    role = _role(m)
    content = _content_to_str(_content(m))
    tcs = _tool_calls(m)
    tc_repr = json.dumps(tcs, ensure_ascii=False, sort_keys=True, default=str) if tcs else ""
    tcid = _tool_call_id(m)
    dc = _dynamic_content(m) if role == "system" else ""
    return f"{role}\x1f{tcid}\x1f{content}\x1f{tc_repr}\x1f{dc}"


# ────────────────────────── 三区划分 ──────────────────────────

@dataclass
class MessageZones:
    """三区边界（半开区间索引）：
    - frozen  = [0, frozen_end)          字节不可变，命中 prefix cache
    - compress= [frozen_end, live_start) 唯一允许整段移除 + CCR offload
    - live    = [live_start, n)          最近 live_rounds 轮，仅 Layer0 + read_lifecycle
    """
    frozen_end: int
    live_start: int


def _round_start_indices(messages: list) -> list[int]:
    """tool 轮起点 = 带 tool_calls 的 assistant 消息索引（轮对齐切分依据）。"""
    return [
        i for i, m in enumerate(messages)
        if _role(m) == "assistant" and _tool_calls(m)
    ]


def compute_zones(messages: list, frozen_count: int, live_rounds: int) -> MessageZones:
    """按 frozen_count（前缀条数）与 live_rounds（尾部保留轮数）切分三区。

    live_start 对齐到倒数第 live_rounds 个 tool 轮的起点；轮数不足时 compressible 区为空。
    """
    n = len(messages)
    frozen_end = max(0, min(frozen_count, n))
    starts = _round_start_indices(messages)
    if len(starts) <= max(0, live_rounds):
        # 轮数不足以形成 compressible 区 → live 紧贴 frozen（无可折叠中段）
        return MessageZones(frozen_end=frozen_end, live_start=frozen_end)
    live_start = starts[len(starts) - live_rounds]
    if live_start < frozen_end:
        live_start = frozen_end
    return MessageZones(frozen_end=frozen_end, live_start=live_start)


# ────────────────────────── 前缀缓存追踪 ──────────────────────────

@dataclass
class PrefixCacheTracker:
    """混合推导 frozen_count + overlay 重放（headroom overlay_cached_prefix 等价）。

    单靠 cache_read 推导不可靠（DeepSeek 无 `prompt_cache_hit_tokens` 逐条映射），故用混合法：
      1) 稳定性哈希（主）：与上轮已发送签名逐条比对，取最长相同前缀长度。
      2) cache_read 校验（辅）：cache_read>0 且未断崖 → 认可；断崖（跌 >50%）→ 收缩到下限。
      3) 下限 clamp 到 min_frozen（= CTX_FROZEN_PREFIX_MSGS），异常时安全回退。
    """
    min_frozen: int = 2
    frozen_count: int = 0
    _prev_sigs: list[str] = field(default_factory=list)
    _prev_cache_read: int = 0
    _stable_rounds: int = 0
    last_forwarded: list | None = None

    def observe(self, messages: list, cache_read: int, ctx_window: int) -> int:
        """发送前调用：推导本轮 frozen_count。返回值同时写入 self.frozen_count。"""
        sigs = [_msg_signature(m) for m in messages]

        # 1) 最长公共前缀（与上轮已发送签名比对）
        stable = 0
        for a, b in zip(sigs, self._prev_sigs):
            if a == b:
                stable += 1
            else:
                break

        # 2) cache_read 断崖检测：上轮有 cache 命中、本轮骤降 >50% → 前缀已失效
        cliff = (
            self._prev_cache_read > 0
            and cache_read >= 0
            and cache_read < self._prev_cache_read * 0.5
        )
        if cliff:
            frozen = self.min_frozen
            self._stable_rounds = 0
            logger.warning(
                "[CTX-CACHE] prefix cliff cache_read {}→{} shrink frozen→{}",
                self._prev_cache_read, cache_read, frozen,
            )
        else:
            frozen = max(self.min_frozen, min(stable, len(messages)))
            if cache_read > 0:
                self._stable_rounds += 1

        self.frozen_count = frozen
        self._prev_sigs = sigs
        self._prev_cache_read = cache_read
        return frozen

    def overlay(self, messages: list) -> list:
        """用上轮已发送的前缀对象替换本轮前 frozen_count 条（签名一致时），
        保证 frozen 前缀发送字节与上轮**完全相同**，杜绝对象重建导致的隐性漂移。"""
        if not self.last_forwarded or self.frozen_count <= 0:
            return messages
        n = min(self.frozen_count, len(messages), len(self.last_forwarded))
        out = list(messages)
        replaced = 0
        for i in range(n):
            if _msg_signature(out[i]) == _msg_signature(self.last_forwarded[i]):
                out[i] = self.last_forwarded[i]
                replaced += 1
            else:
                break
        if replaced:
            logger.debug("[CTX-CACHE] overlay reused {} frozen-prefix msgs", replaced)
        return out

    def note_forwarded(self, messages: list) -> None:
        """发送后（或产出最终待发送列表后）记录，供下轮 overlay / 稳定性哈希。"""
        self.last_forwarded = list(messages)


# ────────────────────────── 边界折叠 ──────────────────────────

def _segment_has_protected(seg: list) -> bool:
    """整轮守卫：段内任一 tool 结果对应 Tier1（read/write/edit/retrieve）→ 保留（不折叠）。

    段自包含（以带 tool_calls 的 assistant 起头），故可直接在段内建立 id→name 映射。
    soul/memory 一般经 read_file/read_document（Tier1）读入 → 被此守卫覆盖（原则 4）。
    """
    id2name: dict[str, str] = {}
    for m in seg:
        if _role(m) == "assistant":
            for tc in _tool_calls(m) or []:
                if isinstance(tc, dict):
                    id2name[tc.get("id", "") or ""] = (tc.get("function") or {}).get("name", "") or ""
    for m in seg:
        if _role(m) == "tool":
            if is_tier1_strict(id2name.get(_tool_call_id(m), "")):
                return True
    return False


def _count_offload_required(dropped: list) -> int:
    """统计折叠段中必须成功写入 CCR 才可移除的消息条数（与 offload_dropped_messages 过滤一致）。"""
    id2name: dict[str, str] = {}
    for m in dropped:
        if _role(m) != "assistant":
            continue
        for tc in _tool_calls(m) or []:
            if isinstance(tc, dict):
                id2name[tc.get("id", "") or ""] = (tc.get("function") or {}).get("name", "") or ""
    required = 0
    for msg in dropped:
        role = _role(msg)
        if role not in ("user", "assistant", "tool"):
            continue
        content = _content_to_str(_content(msg))
        if not content.strip() or "<!-- ccr:" in content:
            continue
        min_len = 512 if role == "tool" else 8
        if len(content) >= min_len:
            required += 1
    return required


def _build_fold_summary(markers: list[str], folded_msgs: int) -> str:
    """折叠边界摘要（独立消息内容）：告知 agent 中段历史已归档、可 retrieve_context 还原。"""
    head = f"[历史中段已折叠归档：{folded_msgs} 条消息移出上下文，可用 retrieve_context 还原]"
    if markers:
        return head + "\n" + "\n".join(markers)
    return head


async def reactive_fold_messages(
    messages: list,
    *,
    frozen_head: int,
    ctx_window: int,
    model_name: str,
    session_id: str,
    agent_id,
    ctx_path: str,
    low_water: float,
    live_rounds: int,
    est_tokens_fn: Callable[[list, str], int],
) -> tuple[list, int]:
    """高水位触发的一次性边界折叠（cache-safe）。返回 (new_messages, folded_msg_count)。

    几何：仅折叠 compressible 区 [frozen_end, live_start)，按 tool 轮从旧到新整段移除，
    直到 est_tokens 降到 low_water 以下（迟滞）。被移除消息 offload 到 CCR，在**首个移除
    位置**插入一条独立 user 摘要消息。frozen 前缀 [0, frozen_end) 全程不动（P0-1）。

    frozen_head：effective_frozen = max(CTX_FROZEN_PREFIX_MSGS, tracker.frozen_count)，
    统一三区下界，避免 fold 侵入 provider 已缓存前缀。
    """
    from .client import LLMMessage
    from .ccr_offload import offload_dropped_messages

    zones = compute_zones(messages, frozen_head, live_rounds)
    fe, ls = zones.frozen_end, zones.live_start
    if ls <= fe:
        return messages, 0

    # compressible 区内的轮切分：段 = [start_k, start_{k+1} or ls)
    comp_starts = [
        i for i in range(fe, ls)
        if _role(messages[i]) == "assistant" and _tool_calls(messages[i])
    ]
    if not comp_starts:
        return messages, 0
    segments: list[tuple[int, int]] = []
    for k, s in enumerate(comp_starts):
        e = comp_starts[k + 1] if k + 1 < len(comp_starts) else ls
        segments.append((s, e))

    # 从旧到新累计移除，直到降到 low_water 以下（Tier1 轮跳过保 verbatim）
    target = ctx_window * low_water
    cur = est_tokens_fn(messages, model_name)
    dropped_idx: set[int] = set()
    to_offload: list = []
    for (s, e) in segments:
        if cur <= target:
            break
        seg = messages[s:e]
        if _segment_has_protected(seg):
            continue
        seg_tok = est_tokens_fn(seg, model_name)
        for j in range(s, e):
            dropped_idx.add(j)
        to_offload.extend(seg)
        cur -= seg_tok

    if not dropped_idx:
        logger.info("[CTX-FOLD] path={} no droppable segment (all Tier1/protected)", ctx_path)
        return messages, 0

    required_offload = _count_offload_required(to_offload)
    markers, offloaded = await offload_dropped_messages(
        to_offload,
        session_id=session_id,
        agent_id=agent_id,
        ctx_path=ctx_path,
        model_name=model_name,
    )

    if required_offload > 0 and offloaded < required_offload:
        try:
            from .ccr_store import incr_ccr_metric
            incr_ccr_metric("fold_aborted_offload_incomplete")
        except Exception:
            pass
        logger.error(
            "[CTX-FOLD] path={} abort fold: offloaded={}/{} required — 消息列表保持不变",
            ctx_path, offloaded, required_offload,
        )
        return messages, 0

    # 重建：在首个移除位置插入独立摘要；frozen 前缀原样保留（索引 < fe 不在 dropped_idx）
    fold_msg = LLMMessage(role="user", content=_build_fold_summary(markers, len(dropped_idx)))
    kept: list = []
    inserted = False
    for i, m in enumerate(messages):
        if i in dropped_idx:
            if not inserted:
                kept.append(fold_msg)
                inserted = True
            continue
        kept.append(m)

    after = est_tokens_fn(kept, model_name)
    logger.info(
        "[CTX-FOLD] path={} folded_msgs={} offloaded={} tokens≈{}→{} frozen={} live_start={}",
        ctx_path, len(dropped_idx), offloaded, est_tokens_fn(messages, model_name), after, fe, ls,
    )
    return kept, len(dropped_idx)
