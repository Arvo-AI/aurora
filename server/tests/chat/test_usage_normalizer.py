"""Cache-aware usage normalization and cost math.

Providers disagree on whether ``input_tokens`` already contains cached tokens; the
normalizer makes it always inclusive so cost and the DB columns stay correct.
"""
import pytest

from chat.backend.agent.utils.llm_usage_tracker import LLMUsageTracker
from chat.backend.agent.utils.usage_normalizer import NormalizedUsage, normalize_usage


class TestNormalizeUsage:
    def test_none_and_garbage_are_zero(self):
        assert normalize_usage(None) == NormalizedUsage()
        assert normalize_usage("nope") == NormalizedUsage()

    def test_no_cache_passthrough(self):
        um = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
        assert normalize_usage(um) == NormalizedUsage(100, 20, 0, 0)

    def test_inclusive_provider_untouched(self):
        # Anthropic/OpenAI: input already contains the cached tokens, total = in + out
        um = {
            "input_tokens": 1000,
            "output_tokens": 50,
            "total_tokens": 1050,
            "input_token_details": {"cache_read": 800, "cache_creation": 100},
        }
        assert normalize_usage(um) == NormalizedUsage(1000, 50, 800, 100)

    def test_anthropic_per_ttl_cache_creation_keys(self):
        # langchain-anthropic zeroes "cache_creation" when it reports per-TTL keys
        um = {
            "input_tokens": 1000,
            "output_tokens": 50,
            "total_tokens": 1050,
            "input_token_details": {
                "cache_read": 800,
                "cache_creation": 0,
                "ephemeral_5m_input_tokens": 100,
                "ephemeral_1h_input_tokens": 0,
            },
        }
        assert normalize_usage(um) == NormalizedUsage(1000, 50, 800, 100)

    def test_converse_exclusive_input_is_made_inclusive(self):
        # Bedrock Converse: inputTokens (100) excludes cache; totalTokens sums all four
        um = {
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 100 + 50 + 800 + 100,
            "input_token_details": {"cache_read": 800, "cache_creation": 100},
        }
        assert normalize_usage(um) == NormalizedUsage(1000, 50, 800, 100)

    def test_already_fixed_upstream_is_not_double_counted(self):
        # If langchain-aws later reports an inclusive input, total == in + out
        um = {
            "input_tokens": 1000,
            "output_tokens": 50,
            "total_tokens": 1050,
            "input_token_details": {"cache_read": 800, "cache_creation": 100},
        }
        assert normalize_usage(um).input_tokens == 1000

    def test_openai_style_token_usage(self):
        usage = {
            "prompt_tokens": 500,
            "completion_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 400},
        }
        assert normalize_usage(usage) == NormalizedUsage(500, 10, 400, 0)


class TestCalculateCostCacheWrites:
    # "default" static pricing keeps these independent of the per-model table
    MODEL = "anthropic/claude-sonnet-4-5"

    @pytest.fixture()
    def price(self, monkeypatch):
        monkeypatch.setitem(
            LLMUsageTracker.MODEL_PRICING,
            self.MODEL,
            {"input": 0.003, "output": 0.015, "cached_input": 0.0003},
        )
        monkeypatch.setenv("LLM_PROVIDER_MODE", "direct")

    def test_claude_cache_write_costs_125_percent(self, price):
        cost = LLMUsageTracker.calculate_cost(
            1000, 0, self.MODEL, cache_write_tokens=1000, provider_mode="direct"
        )
        assert cost == pytest.approx(1 * 0.003 * 1.25)

    def test_read_write_and_fresh_tokens_split(self, price):
        # 10k input = 7k read + 2k write + 1k fresh
        cost = LLMUsageTracker.calculate_cost(
            10_000,
            0,
            self.MODEL,
            cached_input_tokens=7000,
            cache_write_tokens=2000,
            provider_mode="direct",
        )
        expected = 7 * 0.0003 + 2 * 0.003 * 1.25 + 1 * 0.003
        assert cost == pytest.approx(expected)

    def test_non_claude_cache_write_is_plain_input(self, monkeypatch):
        monkeypatch.setitem(
            LLMUsageTracker.MODEL_PRICING,
            "openai/some-model",
            {"input": 0.002, "output": 0.008, "cached_input": 0.0005},
        )
        cost = LLMUsageTracker.calculate_cost(
            1000, 0, "openai/some-model", cache_write_tokens=1000, provider_mode="direct"
        )
        assert cost == pytest.approx(0.002)

    def test_existing_callers_unchanged(self, price):
        # No cache_write_tokens passed: identical to the old read-only math
        cost = LLMUsageTracker.calculate_cost(
            1000, 100, self.MODEL, cached_input_tokens=400, provider_mode="direct"
        )
        assert cost == pytest.approx(0.6 * 0.003 + 0.4 * 0.0003 + 0.1 * 0.015)

    def test_write_tokens_capped_at_remaining_input(self, price):
        # Bad provider data (writes > input) must not produce negative/inflated cost
        cost = LLMUsageTracker.calculate_cost(
            100, 0, self.MODEL, cache_write_tokens=10_000, provider_mode="direct"
        )
        assert cost == pytest.approx(0.1 * 0.003 * 1.25)
