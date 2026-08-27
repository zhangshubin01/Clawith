"""Token accounting tests, including provider KV-cache hit/miss parsing."""

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
