"""Reusable token usage tracking for all LLM call paths.

Provides a single function to record token consumption against an Agent,
used by web chat, heartbeat, triggers, and A2A communication.
"""

import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass

from loguru import logger
from app.dao import query_dao


@dataclass
class TokenUsage:
    """Normalized token accounting returned by model providers."""

    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_miss_tokens: int = 0
    estimated_tokens: int = 0

    def add(self, other: "TokenUsage") -> None:
        self.total_tokens += other.total_tokens
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.reasoning_tokens += other.reasoning_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_creation_tokens += other.cache_creation_tokens
        self.cache_miss_tokens += other.cache_miss_tokens
        self.estimated_tokens += other.estimated_tokens


# — Cache low-hit watchdog cooldown —
# In long conversations the cache-miss ratio oscillates in a sawtooth:
# compaction spikes to ~100% (history → summary rewrites the prefix) and
# DeepSeek's prefix-cache eviction spikes to 50–66%, falling back to 30–48%
# in between. The ≥50% threshold lands on every spike, so a single spike is
# expected behaviour — only a *sustained* low hit rate signals a real break
# (schema reorder / prompt edit). The warning is therefore rate-limited to
# once per agent per window: the right shape for a one-shot "go check schema
# stability" alert, and the fix for alert fatigue, not a workaround.
# Langfuse usage_details (input / input_cache_read) and DailyTokenUsage already
# carry the full hit/miss signal; this warning is a rate-limited operator alert
# layered on top of that observation, not the source of truth for cache health.
# In-memory like agent_tools_cache.py / list_dedup.py: production runs uvicorn
# with a single worker; with several workers each holds its own cooldown and
# the window only bounds per-worker noise.
LOW_HIT_WARNING_COOLDOWN_SECONDS = 30 * 60.0  # decision value, not derived from any reference project
_MAX_LOW_HIT_WARNING_ENTRIES = 1024
_last_low_hit_warning: "OrderedDict[uuid.UUID, float]" = OrderedDict()


def _evict_low_hit_lru_if_needed() -> None:
    while len(_last_low_hit_warning) > _MAX_LOW_HIT_WARNING_ENTRIES:
        _last_low_hit_warning.popitem(last=False)


def _low_hit_warning_due(agent_id: uuid.UUID) -> bool:
    """True when a low-hit warning should fire for this agent (cooldown elapsed)."""
    now = time.monotonic()
    last = _last_low_hit_warning.get(agent_id)
    if last is not None and now - last < LOW_HIT_WARNING_COOLDOWN_SECONDS:
        return False
    _last_low_hit_warning[agent_id] = now
    _last_low_hit_warning.move_to_end(agent_id)
    _evict_low_hit_lru_if_needed()
    return True


def clear_low_hit_warning_cooldown() -> None:
    """Test hook — drop every cooldown timestamp."""
    _last_low_hit_warning.clear()


def _maybe_warn_low_hit(agent_id: uuid.UUID, agent_name: str, usage: TokenUsage) -> None:
    """Emit the cache-health warning, at most once per agent per cooldown window.

    A sawtooth spike (compaction / cache eviction) is expected behaviour, not a
    broken prefix; the per-agent cooldown collapses repeated spikes into a
    single alert. Thresholds are unchanged so a real sustained break still
    fires (once per window, indefinitely).
    """
    if usage.cache_miss_tokens < 1024:
        return
    miss_ratio = usage.cache_miss_tokens / max(usage.input_tokens, 1)
    if miss_ratio < 0.5:
        return
    if not _low_hit_warning_due(agent_id):
        return
    logger.warning(
        "[Token Cache] Low hit rate agent={} miss={} input={} "
        "ratio={:.0%} — check prompt/tool-schema stability",
        agent_name,
        usage.cache_miss_tokens,
        usage.input_tokens,
        miss_ratio,
    )


def estimate_tokens_from_chars(total_chars: int) -> int:
    """Rough token estimate when real usage is unavailable. ~3 chars per token."""
    return max(total_chars // 3, 1)


def estimate_token_usage_from_chars(total_chars: int) -> TokenUsage:
    tokens = estimate_tokens_from_chars(total_chars)
    return TokenUsage(total_tokens=tokens, estimated_tokens=tokens)


def _int_token(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _token_counter(source: dict, *keys: str) -> int:
    return sum(_int_token(source.get(key)) for key in keys)


def extract_token_usage(usage: dict | None) -> TokenUsage | None:
    """Extract normalized token usage, including prompt-cache counters when available."""
    if not usage:
        return None

    # OpenAI compatible:
    # {"prompt_tokens": N, "completion_tokens": N, "total_tokens": N,
    #  "prompt_tokens_details": {"cached_tokens": N}}
    if "total_tokens" in usage:
        detail_sources = [
            details
            for details in (
                usage.get("prompt_tokens_details"),
                usage.get("input_tokens_details"),
            )
            if isinstance(details, dict)
        ]
        cached = _token_counter(
            usage,
            "cached_tokens",
            "cache_read_tokens",
            "cache_read_input_tokens",
            "prompt_cache_hit_tokens",  # DeepSeek KV cache hit
        )
        cache_creation = _token_counter(
            usage,
            "cache_creation_tokens",
            "cache_creation_input_tokens",
        )
        # DeepSeek reports misses explicitly; for other OpenAI-compatible
        # providers the miss is the uncached remainder of the prompt.
        cache_miss = _token_counter(usage, "prompt_cache_miss_tokens")
        # Some providers report the same cache counters in both places:
        # DeepSeek sends prompt_cache_hit_tokens at the top level AND the
        # identical value as prompt_tokens_details.cached_tokens. Summing both
        # levels double-counts every hit, so the details are only consulted as
        # a fallback for providers that report there exclusively (e.g. OpenAI).
        if not cached:
            for details in detail_sources:
                cached += _token_counter(
                    details,
                    "cached_tokens",
                    "cache_read_tokens",
                    "cache_read_input_tokens",
                    "prompt_cache_hit_tokens",
                )
        if not cache_creation:
            for details in detail_sources:
                cache_creation += _token_counter(
                    details,
                    "cache_creation_tokens",
                    "cache_creation_input_tokens",
                )
        if not cache_miss:
            cache_miss = max(_int_token(usage.get("prompt_tokens")) - cached, 0)
        if cached or cache_creation:
            logger.debug(f"[Token Cache] API Provider -> Created: {cache_creation} tokens, Read: {cached} tokens")
        input_tokens = _int_token(usage.get("prompt_tokens", usage.get("input_tokens", 0)))
        output_tokens = _int_token(usage.get("completion_tokens", usage.get("output_tokens", 0)))
        total_tokens = _int_token(usage.get("total_tokens", input_tokens + output_tokens))
        # Reasoning/thinking tokens (DeepSeek completion_tokens_details.reasoning_tokens,
        # Qwen top-level reasoning_tokens). Recorded separately so Langfuse can
        # attribute reasoning cost; output_tokens keeps its inclusive meaning for
        # quota/billing (unchanged).
        reasoning_tokens = _token_counter(usage, "reasoning_tokens")
        completion_details = usage.get("completion_tokens_details")
        if isinstance(completion_details, dict):
            reasoning_tokens += _token_counter(completion_details, "reasoning_tokens")
        return TokenUsage(
            total_tokens=total_tokens,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
            cache_read_tokens=cached,
            cache_creation_tokens=cache_creation,
            cache_miss_tokens=cache_miss,
        )

    # Anthropic:
    # {"input_tokens": N, "output_tokens": N,
    #  "cache_creation_input_tokens": N, "cache_read_input_tokens": N}
    if "input_tokens" in usage or "output_tokens" in usage:
        cache_creation = _token_counter(usage, "cache_creation_input_tokens", "cache_creation_tokens")
        cache_read = _token_counter(usage, "cache_read_input_tokens", "cache_read_tokens", "cached_tokens")
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            cache_creation += _token_counter(details, "cache_creation_input_tokens", "cache_creation_tokens")
            cache_read += _token_counter(details, "cached_tokens", "cache_read_input_tokens", "cache_read_tokens")
        if cache_creation or cache_read:
            logger.info(f"[Token Cache] Anthropic Native Hit -> Created: {cache_creation}, Read: {cache_read} tokens")
        input_tokens = _int_token(usage.get("input_tokens", 0))
        output_tokens = _int_token(usage.get("output_tokens", 0))
        return TokenUsage(
            total_tokens=input_tokens + output_tokens,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
            cache_miss_tokens=max(input_tokens - cache_read - cache_creation, 0),
        )

    # Gemini usage metadata can be normalized by the client, but keep a direct
    # fallback for providers that pass it through.
    if "promptTokenCount" in usage or "candidatesTokenCount" in usage:
        input_tokens = _int_token(usage.get("promptTokenCount", 0))
        output_tokens = _int_token(usage.get("candidatesTokenCount", 0))
        total_tokens = _int_token(usage.get("totalTokenCount", input_tokens + output_tokens))
        cached = _int_token(usage.get("cachedContentTokenCount", 0))
        return TokenUsage(
            total_tokens=total_tokens,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cached,
            cache_miss_tokens=max(input_tokens - cached, 0),
        )

    return None


def extract_usage_tokens(usage: dict | None) -> int | None:
    """Extract total token count from an LLM response usage dict.

    Supports both OpenAI format (prompt_tokens + completion_tokens)
    and Anthropic format (input_tokens + output_tokens).
    Returns None if usage data is not available.
    """
    parsed = extract_token_usage(usage)
    return parsed.total_tokens if parsed else None


async def record_token_usage(
    agent_id: uuid.UUID,
    tokens: int | TokenUsage,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    estimated_tokens: int = 0,
) -> None:
    """Record token consumption for an agent.

    Safely updates tokens_used_today, tokens_used_month, and tokens_used_total.
    Uses an independent DB session to avoid interfering with the caller's transaction.
    """
    usage = (
        tokens
        if isinstance(tokens, TokenUsage)
        else TokenUsage(
            total_tokens=tokens,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
            estimated_tokens=estimated_tokens,
        )
    )
    if usage.total_tokens <= 0:
        return

    try:
        from app.models.agent import Agent
        from sqlalchemy import select

        async with query_dao.session() as db:
            result = await query_dao.execute(db, select(Agent).where(Agent.id == agent_id))
            agent = result.scalar_one_or_none()
            if agent:
                agent.tokens_used_today = (agent.tokens_used_today or 0) + usage.total_tokens
                agent.tokens_used_month = (agent.tokens_used_month or 0) + usage.total_tokens
                agent.tokens_used_total = (agent.tokens_used_total or 0) + usage.total_tokens
                agent.cache_read_tokens_today = (agent.cache_read_tokens_today or 0) + usage.cache_read_tokens
                agent.cache_read_tokens_month = (agent.cache_read_tokens_month or 0) + usage.cache_read_tokens
                agent.cache_read_tokens_total = (agent.cache_read_tokens_total or 0) + usage.cache_read_tokens
                agent.cache_creation_tokens_today = (
                    agent.cache_creation_tokens_today or 0
                ) + usage.cache_creation_tokens
                agent.cache_creation_tokens_month = (
                    agent.cache_creation_tokens_month or 0
                ) + usage.cache_creation_tokens
                agent.cache_creation_tokens_total = (
                    agent.cache_creation_tokens_total or 0
                ) + usage.cache_creation_tokens
                agent.cache_miss_tokens_today = (agent.cache_miss_tokens_today or 0) + usage.cache_miss_tokens
                agent.cache_miss_tokens_month = (agent.cache_miss_tokens_month or 0) + usage.cache_miss_tokens
                agent.cache_miss_tokens_total = (agent.cache_miss_tokens_total or 0) + usage.cache_miss_tokens

                # Cache health watchdog: a single high cache-miss step is not
                # evidence of a broken prefix — compaction and cache eviction
                # both spike then recover. Warn at most once per agent per
                # window; a sustained break still fires once per window.
                _maybe_warn_low_hit(agent_id, agent.name, usage)

                from datetime import datetime, timezone
                from sqlalchemy.dialects.postgresql import insert
                from app.models.activity_log import DailyTokenUsage

                today_date = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                stmt = (
                    insert(DailyTokenUsage)
                    .values(
                        tenant_id=agent.tenant_id,
                        agent_id=agent.id,
                        date=today_date,
                        tokens_used=usage.total_tokens,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        cache_read_tokens=usage.cache_read_tokens,
                        cache_creation_tokens=usage.cache_creation_tokens,
                        cache_miss_tokens=usage.cache_miss_tokens,
                        estimated_tokens=usage.estimated_tokens,
                    )
                    .on_conflict_do_update(
                        index_elements=["agent_id", "date"],
                        set_=dict(
                            tokens_used=DailyTokenUsage.tokens_used + usage.total_tokens,
                            input_tokens=DailyTokenUsage.input_tokens + usage.input_tokens,
                            output_tokens=DailyTokenUsage.output_tokens + usage.output_tokens,
                            cache_read_tokens=DailyTokenUsage.cache_read_tokens + usage.cache_read_tokens,
                            cache_creation_tokens=DailyTokenUsage.cache_creation_tokens + usage.cache_creation_tokens,
                            cache_miss_tokens=DailyTokenUsage.cache_miss_tokens + usage.cache_miss_tokens,
                            estimated_tokens=DailyTokenUsage.estimated_tokens + usage.estimated_tokens,
                        ),
                    )
                )
                await query_dao.execute(db, stmt)

                await query_dao.commit(db)
                logger.debug(
                    f"Recorded {usage.total_tokens:,} tokens for agent {agent.name} "
                    f"(cache_read={usage.cache_read_tokens:,})"
                )
    except Exception as e:
        logger.warning(f"Failed to record token usage for agent {agent_id}: {e}")
