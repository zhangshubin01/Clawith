"""Token accounting tests, including provider KV-cache hit/miss parsing."""

import uuid

import pytest
from loguru import logger

import app.services.token_tracker as token_tracker
from app.services.token_tracker import TokenUsage, extract_token_usage


class TestExtractDeepSeekUsage:
    def test_full_cache_hit(self) -> None:
        usage = extract_token_usage(
            {
                "prompt_tokens": 11648,
                "completion_tokens": 100,
                "total_tokens": 11748,
                "prompt_cache_hit_tokens": 11648,
                "prompt_cache_miss_tokens": 0,
            }
        )
        assert usage is not None
        assert usage.cache_read_tokens == 11648
        assert usage.cache_miss_tokens == 0

    def test_full_cache_miss(self) -> None:
        usage = extract_token_usage(
            {
                "prompt_tokens": 11648,
                "completion_tokens": 100,
                "total_tokens": 11748,
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 11648,
            }
        )
        assert usage is not None
        assert usage.cache_read_tokens == 0
        assert usage.cache_miss_tokens == 11648

    def test_partial_hit(self) -> None:
        usage = extract_token_usage(
            {
                "prompt_tokens": 12000,
                "completion_tokens": 100,
                "total_tokens": 12100,
                "prompt_cache_hit_tokens": 11000,
                "prompt_cache_miss_tokens": 1000,
            }
        )
        assert usage is not None
        assert usage.cache_read_tokens == 11000
        assert usage.cache_miss_tokens == 1000

    def test_real_payload_top_level_and_details_same_value_not_double_counted(self) -> None:
        """DeepSeek's actual cached-hit response carries the SAME value twice:
        top-level prompt_cache_hit_tokens AND prompt_tokens_details.cached_tokens.
        Shape captured live from deepseek-v4-pro on 2026-08-27 (second identical
        request, cache hit): hit=6656, miss=41, prompt=6697 = 6656 + 41.
        cache_read must not sum both occurrences, or a 99.4% hit rate (6656/6697)
        is recorded as 13312/6697 ≈ 199% of the prompt."""
        usage = extract_token_usage(
            {
                "prompt_tokens": 6697,
                "completion_tokens": 16,
                "total_tokens": 6713,
                "prompt_cache_hit_tokens": 6656,
                "prompt_cache_miss_tokens": 41,
                "prompt_tokens_details": {"cached_tokens": 6656},
            }
        )
        assert usage is not None
        assert usage.cache_read_tokens == 6656
        assert usage.cache_miss_tokens == 41

    def test_reasoning_tokens_from_completion_details(self) -> None:
        """DeepSeek reasoning models report completion_tokens_details.reasoning_tokens;
        it is captured separately while output_tokens keeps its inclusive meaning."""
        usage = extract_token_usage(
            {
                "prompt_tokens": 100,
                "completion_tokens": 200,
                "total_tokens": 300,
                "completion_tokens_details": {"reasoning_tokens": 150},
            }
        )
        assert usage is not None
        assert usage.reasoning_tokens == 150
        # output_tokens unchanged — reasoning still counted for quota/billing.
        assert usage.output_tokens == 200


class TestExtractOpenAICompatibleUsage:
    def test_miss_derived_from_uncached_remainder(self) -> None:
        usage = extract_token_usage(
            {
                "prompt_tokens": 1000,
                "completion_tokens": 50,
                "total_tokens": 1050,
                "prompt_tokens_details": {"cached_tokens": 800},
            }
        )
        assert usage is not None
        assert usage.cache_read_tokens == 800
        assert usage.cache_miss_tokens == 200


class TestExtractAnthropicUsage:
    def test_miss_is_uncached_input(self) -> None:
        usage = extract_token_usage(
            {
                "input_tokens": 1000,
                "output_tokens": 50,
                "cache_read_input_tokens": 700,
                "cache_creation_input_tokens": 100,
            }
        )
        assert usage is not None
        assert usage.cache_read_tokens == 700
        assert usage.cache_creation_tokens == 100
        assert usage.cache_miss_tokens == 200


class TestExtractGeminiUsage:
    def test_miss_is_uncached_prompt(self) -> None:
        usage = extract_token_usage(
            {
                "promptTokenCount": 1000,
                "candidatesTokenCount": 50,
                "totalTokenCount": 1050,
                "cachedContentTokenCount": 600,
            }
        )
        assert usage is not None
        assert usage.cache_read_tokens == 600
        assert usage.cache_miss_tokens == 400


class TestTokenUsageAdd:
    def test_add_accumulates_cache_miss(self) -> None:
        left = TokenUsage(cache_miss_tokens=100, cache_read_tokens=900)
        right = TokenUsage(cache_miss_tokens=50, cache_read_tokens=950)
        left.add(right)
        assert left.cache_miss_tokens == 150
        assert left.cache_read_tokens == 1850


# ── Cache low-hit watchdog cooldown ──


@pytest.fixture
def _captured_logs():
    captured: list[str] = []
    sink_id = logger.add(lambda message: captured.append(str(message)), level="WARNING")
    yield captured
    logger.remove(sink_id)


@pytest.fixture(autouse=True)
def _clear_cooldown():
    token_tracker.clear_low_hit_warning_cooldown()
    yield
    token_tracker.clear_low_hit_warning_cooldown()


def _high_miss(miss: int = 1024, input_tokens: int = 2048) -> TokenUsage:
    return TokenUsage(input_tokens=input_tokens, cache_miss_tokens=miss)


class TestLowHitWarningCooldown:
    def test_first_high_miss_warns(self, _captured_logs) -> None:
        token_tracker._maybe_warn_low_hit(uuid.uuid4(), "agent-a", _high_miss())
        assert any("Low hit rate" in line for line in _captured_logs)

    def test_suppressed_within_cooldown(self, _captured_logs) -> None:
        agent_id = uuid.uuid4()
        token_tracker._maybe_warn_low_hit(agent_id, "agent-a", _high_miss())
        token_tracker._maybe_warn_low_hit(agent_id, "agent-a", _high_miss(miss=2000, input_tokens=3000))
        assert sum("Low hit rate" in line for line in _captured_logs) == 1

    def test_warns_again_after_cooldown(self, _captured_logs, monkeypatch) -> None:
        agent_id = uuid.uuid4()
        monkeypatch.setattr(token_tracker, "LOW_HIT_WARNING_COOLDOWN_SECONDS", 0.0)
        token_tracker._maybe_warn_low_hit(agent_id, "agent-a", _high_miss())
        token_tracker._maybe_warn_low_hit(agent_id, "agent-a", _high_miss())
        assert sum("Low hit rate" in line for line in _captured_logs) == 2

    def test_independent_agents_warn_separately(self, _captured_logs) -> None:
        token_tracker._maybe_warn_low_hit(uuid.uuid4(), "agent-a", _high_miss())
        token_tracker._maybe_warn_low_hit(uuid.uuid4(), "agent-b", _high_miss())
        assert sum("Low hit rate" in line for line in _captured_logs) == 2

    def test_low_miss_does_not_warn(self, _captured_logs) -> None:
        token_tracker._maybe_warn_low_hit(uuid.uuid4(), "agent-a", _high_miss(miss=1023))
        assert not any("Low hit rate" in line for line in _captured_logs)

    def test_low_ratio_does_not_warn(self, _captured_logs) -> None:
        token_tracker._maybe_warn_low_hit(uuid.uuid4(), "agent-a", _high_miss(miss=5000, input_tokens=20000))
        assert not any("Low hit rate" in line for line in _captured_logs)

    def test_clear_hook_resets_cooldown(self, _captured_logs) -> None:
        agent_id = uuid.uuid4()
        token_tracker._maybe_warn_low_hit(agent_id, "agent-a", _high_miss())
        token_tracker.clear_low_hit_warning_cooldown()
        token_tracker._maybe_warn_low_hit(agent_id, "agent-a", _high_miss())
        assert sum("Low hit rate" in line for line in _captured_logs) == 2

    def test_cooldown_dict_is_bounded(self) -> None:
        for _ in range(token_tracker._MAX_LOW_HIT_WARNING_ENTRIES + 5):
            token_tracker._maybe_warn_low_hit(uuid.uuid4(), "agent-x", _high_miss())
        assert len(token_tracker._last_low_hit_warning) <= token_tracker._MAX_LOW_HIT_WARNING_ENTRIES
