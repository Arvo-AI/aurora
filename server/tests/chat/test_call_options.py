"""Provider-neutral call options and Claude family facts (chat.backend.agent.call_options).

The translation of ``max_tokens`` / ``request_timeout`` / ``disable_thinking`` to each
client's own fields lives in the providers; this module pins the shared pieces every
provider relies on: id normalization, the thinking-disable allowlist (with the 5.5
exclusions that are prefix-collisions of allowed families) and the cache minimums.
"""
import pytest

from chat.backend.agent.call_options import (
    CHEAP_MODEL,
    CHEAP_MODEL_BEDROCK_GEOS,
    CallOptions,
    bedrock_geo_for_region,
    bedrock_pinned_cheap_model,
    cheap_tier_routable,
    claude_catalog_ids,
    claude_family,
    default_auxiliary_model,
    effective_max_tokens,
    gemini_thinks_by_default,
    max_tokens_for,
    min_cacheable_tokens,
    pop_call_options,
    reasons_by_default,
    supports_thinking_disable,
)


def test_cheap_model_is_haiku_5_5():
    assert CHEAP_MODEL == "anthropic/claude-haiku-5.5"


def test_haiku_5_5_geos_match_the_model_card():
    """Bedrock model card (read 2026-10-22): us./eu./au./jp. geo profiles + global. — no apac."""
    assert set(CHEAP_MODEL_BEDROCK_GEOS) == {"us", "eu", "au", "jp", "global"}
    assert "apac" not in CHEAP_MODEL_BEDROCK_GEOS


@pytest.mark.parametrize(
    ("region", "expected"),
    [
        ("us-east-1", "us"),
        ("eu-central-1", "eu"),
        ("ap-northeast-1", "apac"),
        ("ap-southeast-2", "apac"),
        ("global", "global"),
        ("GLOBAL", "global"),
        ("", "us"),
        (None, "us"),
    ],
)
def test_bedrock_geo_for_region(region, expected):
    assert bedrock_geo_for_region(region) == expected


class TestCheapTierRoutable:
    """Mirror of ProviderRegistry.get_provider_for_model on env vars only. Dummy values
    throughout — nothing here is a real key."""

    @pytest.mark.parametrize(
        "env",
        [
            {"LLM_PROVIDER_MODE": "openrouter"},
            {"LLM_PROVIDER_MODE": "anthropic"},
            {"LLM_PROVIDER_MODE": "Anthropic "},  # case / whitespace tolerant
            {"LLM_PROVIDER_MODE": "bedrock", "BEDROCK_REGION": "eu-central-1"},
            {"LLM_PROVIDER_MODE": "bedrock", "BEDROCK_REGION": "us-west-2"},
            {"LLM_PROVIDER_MODE": "bedrock", "BEDROCK_REGION": "global"},
            # Gateway without a region: the provider derives the `us.` profile id.
            {"LLM_PROVIDER_MODE": "bedrock", "BEDROCK_BASE_URL": "https://gw.internal/v1"},
            # Gateway: the derived profile names go to the gateway exactly like MAIN's do,
            # so a geo that publishes Haiku 5.5 is routable there too.
            {"LLM_PROVIDER_MODE": "bedrock", "BEDROCK_BASE_URL": "https://gw.internal/v1",
             "BEDROCK_REGION": "eu-central-1"},
            {"LLM_PROVIDER_MODE": "bedrock", "BEDROCK_BASE_URL": "https://gw.internal/v1",
             "BEDROCK_REGION": "global"},
            {"LLM_PROVIDER_MODE": "bedrock", "AWS_REGION": "eu-west-1"},
            {"LLM_PROVIDER_MODE": "bedrock", "AWS_DEFAULT_REGION": "eu-west-1"},
            {"LLM_PROVIDER_MODE": "direct", "ANTHROPIC_API_KEY": "dummy"},
            {"LLM_PROVIDER_MODE": "auto", "ANTHROPIC_API_KEY": "dummy"},
            {"ANTHROPIC_API_KEY": "dummy"},  # mode unset == direct
            {"LLM_PROVIDER_MODE": "", "ANTHROPIC_API_KEY": "dummy"},  # Compose empty == direct
            # A forced non-Claude provider falls back to prefix routing for anthropic/ ids,
            # which works as long as the Anthropic key is there.
            {"LLM_PROVIDER_MODE": "ollama", "ANTHROPIC_API_KEY": "dummy"},
            {"LLM_PROVIDER_MODE": "vertex", "ANTHROPIC_API_KEY": "dummy"},
        ],
    )
    def test_routable(self, env):
        assert cheap_tier_routable(env) is True
        assert default_auxiliary_model("ollama/llama3.1", env) == CHEAP_MODEL

    @pytest.mark.parametrize(
        "env",
        [
            {},
            {"LLM_PROVIDER_MODE": "direct"},
            {"LLM_PROVIDER_MODE": "direct", "ANTHROPIC_API_KEY": ""},
            {"LLM_PROVIDER_MODE": "auto"},
            # Bedrock gateway pinned to explicit bedrock/ ids under direct routing: the
            # anthropic/ cheap tier would hit the Anthropic provider, not the gateway.
            {"LLM_PROVIDER_MODE": "direct", "BEDROCK_BASE_URL": "https://gw.internal/v1"},
            {"LLM_PROVIDER_MODE": "direct", "BEDROCK_REGION": "eu-central-1"},
            # Forced bedrock with nothing configured: no Bedrock route exists either.
            {"LLM_PROVIDER_MODE": "bedrock"},
            {"LLM_PROVIDER_MODE": "bedrock", "BEDROCK_REGION": ""},
            # Forced bedrock, native mode, ap-* region: the derived `apac.` Haiku 5.5
            # profile does not exist (model card: us/eu/au/jp/global only) -> 404 -> the
            # judge and input rail would fail closed. Even with a stray Anthropic key the
            # forced provider still serves the anthropic/ id, so the key changes nothing.
            {"LLM_PROVIDER_MODE": "bedrock", "BEDROCK_REGION": "ap-northeast-1"},
            {"LLM_PROVIDER_MODE": "bedrock", "BEDROCK_REGION": "ap-southeast-2", "ANTHROPIC_API_KEY": "dummy"},
            {"LLM_PROVIDER_MODE": "bedrock", "AWS_REGION": "ap-south-1"},
            # Gateway mode in an ap-* region: get_native_model_name derives the same
            # `apac.` id for the gateway, so a Bedrock-backed gateway 404s the same way.
            {"LLM_PROVIDER_MODE": "bedrock", "BEDROCK_BASE_URL": "https://gw.internal/v1",
             "BEDROCK_REGION": "ap-northeast-1"},
            {"LLM_PROVIDER_MODE": "bedrock", "BEDROCK_BASE_URL": "https://gw.internal/v1",
             "AWS_REGION": "ap-southeast-2", "ANTHROPIC_API_KEY": "dummy"},
            # Forced providers that cannot serve Claude, no Anthropic key.
            {"LLM_PROVIDER_MODE": "ollama", "OLLAMA_BASE_URL": "http://host:11434"},
            {"LLM_PROVIDER_MODE": "vertex", "VERTEX_AI_PROJECT": "demo"},
            {"LLM_PROVIDER_MODE": "google", "GOOGLE_AI_API_KEY": "dummy"},
            {"LLM_PROVIDER_MODE": "openai", "OPENAI_API_KEY": "dummy"},
        ],
    )
    def test_not_routable_falls_back_to_main_model(self, env):
        assert cheap_tier_routable(env) is False
        assert default_auxiliary_model("ollama/llama3.1", env) == "ollama/llama3.1"

    def test_defaults_to_the_process_environment(self, monkeypatch):
        for var in ("LLM_PROVIDER_MODE", "ANTHROPIC_API_KEY", "BEDROCK_BASE_URL",
                    "BEDROCK_REGION", "AWS_REGION", "AWS_DEFAULT_REGION"):
            monkeypatch.delenv(var, raising=False)
        assert cheap_tier_routable() is False
        monkeypatch.setenv("LLM_PROVIDER_MODE", "openrouter")
        assert cheap_tier_routable() is True


class TestBedrockPinnedMainModel:
    """MAIN_MODEL pinned to an explicit bedrock/<id>: the cheap tier must stay on that
    Bedrock route in every LLM_PROVIDER_MODE — never move to another provider (an EU
    deployment with a stray Anthropic key must not leak background traffic to
    api.anthropic.com) and never to an id the pinned geo does not publish (an `apac.`
    Haiku 5.5 would 404 and the fail-closed judge would block every request)."""

    JP_PIN = "bedrock/jp.anthropic.claude-sonnet-4-6"
    EU_PIN = "bedrock/eu.anthropic.claude-opus-5-5"

    @pytest.mark.parametrize(
        ("main_model", "expected"),
        [
            ("bedrock/eu.anthropic.claude-opus-5-5", "bedrock/eu.anthropic.claude-haiku-5-5"),
            ("bedrock/jp.anthropic.claude-sonnet-4-6", "bedrock/jp.anthropic.claude-haiku-5-5"),
            ("bedrock/au.anthropic.claude-sonnet-4-6", "bedrock/au.anthropic.claude-haiku-5-5"),
            ("bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0", "bedrock/us.anthropic.claude-haiku-5-5"),
            ("bedrock/global.anthropic.claude-opus-5-5", "bedrock/global.anthropic.claude-haiku-5-5"),
            # Case-insensitive on the geo; a pin that already IS the cheap tier is a no-op.
            ("bedrock/EU.anthropic.claude-opus-5-5", "bedrock/eu.anthropic.claude-haiku-5-5"),
            ("bedrock/eu.anthropic.claude-haiku-5-5", "bedrock/eu.anthropic.claude-haiku-5-5"),
            # Legacy Haiku 4.5 pin moves to the newer cheap tier on the same geo.
            ("bedrock/eu.anthropic.claude-haiku-4-5-20251001-v1:0", "bedrock/eu.anthropic.claude-haiku-5-5"),
        ],
    )
    def test_native_claude_pin_derives_haiku_5_5_on_the_same_geo(self, main_model, expected):
        env = {"LLM_PROVIDER_MODE": "bedrock", "BEDROCK_REGION": "eu-central-1"}
        assert bedrock_pinned_cheap_model(main_model, env) == expected
        assert default_auxiliary_model(main_model, env) == expected

    @pytest.mark.parametrize(
        "main_model",
        [
            # apac. is not a geo Haiku 5.5 publishes.
            "bedrock/apac.anthropic.claude-sonnet-4-5-20250929-v1:0",
            # Bare foundation-model id (provisioned throughput), ARN, unknown prefix.
            "bedrock/anthropic.claude-sonnet-4-5-20250929-v1:0",
            "bedrock/arn:aws:bedrock:eu-central-1:123456789012:application-inference-profile/abc",
            "bedrock/xx.anthropic.claude-opus-5-5",
            # Non-Anthropic Bedrock models: the account may lack Anthropic model access.
            "bedrock/eu.amazon.nova-pro-v1:0",
            "bedrock/eu.meta.llama3-2-90b-instruct-v1:0",
        ],
    )
    def test_other_native_pins_follow_main_model(self, main_model):
        env = {"LLM_PROVIDER_MODE": "bedrock", "BEDROCK_REGION": "eu-central-1"}
        assert bedrock_pinned_cheap_model(main_model, env) == main_model
        assert default_auxiliary_model(main_model, env) == main_model

    @pytest.mark.parametrize(
        "mode", ["bedrock", "direct", "auto", "anthropic", "openrouter", "vertex", ""]
    )
    def test_gateway_pins_follow_main_model_in_every_mode(self, mode):
        """Gateway names are the gateway's own; the cheap tier's name there is unknown."""
        env = {"LLM_PROVIDER_MODE": mode, "BEDROCK_BASE_URL": "https://gw.internal/v1",
               "ANTHROPIC_API_KEY": "dummy"}
        for pin in ("bedrock/claude-sonnet", self.EU_PIN):
            assert bedrock_pinned_cheap_model(pin, env) == pin
            assert default_auxiliary_model(pin, env) == pin

    def test_review_case_apac_install_pinned_to_jp_profile(self):
        """LLM_PROVIDER_MODE=bedrock + ap-northeast-1 + MAIN=bedrock/jp.anthropic.claude-sonnet-4-6:
        the region alone would derive apac. (unpublished); the pin says jp., which exists."""
        env = {"LLM_PROVIDER_MODE": "bedrock", "BEDROCK_REGION": "ap-northeast-1"}
        result = default_auxiliary_model(self.JP_PIN, env)
        assert result == "bedrock/jp.anthropic.claude-haiku-5-5"
        assert not result.startswith("anthropic/")

    def test_review_case_eu_pin_with_stray_anthropic_key_stays_on_bedrock(self):
        """direct + ANTHROPIC_API_KEY + BEDROCK_REGION=eu-central-1 + MAIN=bedrock/eu.…opus-5-5:
        prefix routing would otherwise send the judge and every summary to the Anthropic API."""
        env = {"LLM_PROVIDER_MODE": "direct", "ANTHROPIC_API_KEY": "dummy",
               "BEDROCK_REGION": "eu-central-1"}
        result = default_auxiliary_model(self.EU_PIN, env)
        assert result == "bedrock/eu.anthropic.claude-haiku-5-5"
        assert result.startswith("bedrock/eu.")

    @pytest.mark.parametrize("mode", ["direct", "auto", "anthropic", "openrouter", "ollama", ""])
    def test_pin_is_honoured_in_every_mode(self, mode):
        env = {"LLM_PROVIDER_MODE": mode, "ANTHROPIC_API_KEY": "dummy", "BEDROCK_REGION": "eu-central-1"}
        assert default_auxiliary_model(self.EU_PIN, env) == "bedrock/eu.anthropic.claude-haiku-5-5"

    @pytest.mark.parametrize(
        "main_model",
        ["anthropic/claude-opus-5.5", "ollama/llama3.1", "openai/gpt-5.6-sol", "claude-sonnet-4-6", "", "bedrock/"],
    )
    def test_non_bedrock_main_models_are_not_pins(self, main_model):
        env = {"LLM_PROVIDER_MODE": "bedrock", "BEDROCK_REGION": "eu-central-1"}
        assert bedrock_pinned_cheap_model(main_model, env) is None

    def test_clean_main_model_behaviour_is_unchanged(self):
        """Test (c) from iteration 2: a clean Claude MAIN_MODEL under forced bedrock in the EU
        still yields the clean cheap tier (translated to eu.anthropic.claude-haiku-5-5 later)."""
        env = {"LLM_PROVIDER_MODE": "bedrock", "BEDROCK_REGION": "eu-central-1"}
        assert default_auxiliary_model("anthropic/claude-opus-5.5", env) == CHEAP_MODEL


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("anthropic/claude-haiku-5.5", "claude-haiku-5-5"),
        ("anthropic/claude-haiku-5-5", "claude-haiku-5-5"),
        ("claude-haiku-5-5", "claude-haiku-5-5"),
        ("bedrock/eu.anthropic.claude-haiku-5-5", "claude-haiku-5-5"),
        ("bedrock/global.anthropic.claude-fable-5-1", "claude-fable-5-1"),
        ("eu.anthropic.claude-opus-5-5", "claude-opus-5-5"),
        ("anthropic/claude-opus-5.5", "claude-opus-5-5"),
        # Dated / versioned Bedrock ids lose their suffix.
        ("us.anthropic.claude-haiku-4-5-20251001-v1:0", "claude-haiku-4-5"),
        ("bedrock/us.anthropic.claude-opus-4-6-v1", "claude-opus-4-6"),
        ("anthropic.claude-sonnet-4-5-20250929-v1:0", "claude-sonnet-4-5"),
        # Non-Claude ids are normalized but not mangled into a Claude name.
        ("openai/gpt-5.5", "openai/gpt-5-5"),
        ("", ""),
    ],
)
def test_claude_family_normalizes_every_spelling(model_id, expected):
    assert claude_family(model_id) == expected


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("bedrock/eu.anthropic.claude-haiku-5-5", ("anthropic/claude-haiku-5-5", "anthropic/claude-haiku-5.5")),
        ("anthropic/claude-opus-5.5", ("anthropic/claude-opus-5-5", "anthropic/claude-opus-5.5")),
        ("anthropic.claude-haiku-4-5-20251001-v1:0", ("anthropic/claude-haiku-4-5", "anthropic/claude-haiku-4.5")),
        # A single-digit version has no dotted twin.
        ("bedrock/eu.anthropic.claude-sonnet-5", ("anthropic/claude-sonnet-5",)),
        # The generation number of the legacy ids is not a version pair.
        ("claude-3-haiku", ("anthropic/claude-3-haiku",)),
        ("anthropic/claude-3-5-sonnet", ("anthropic/claude-3-5-sonnet", "anthropic/claude-3.5-sonnet")),
        ("openai/gpt-5.5", ()),
        ("bedrock/amazon.nova-pro-v1:0", ()),
        ("", ()),
    ],
)
def test_claude_catalog_ids_cover_both_version_spellings(model_id, expected):
    assert claude_catalog_ids(model_id) == expected


class TestThinkingDisableAllowlist:
    @pytest.mark.parametrize(
        "model_id",
        [
            "anthropic/claude-haiku-5.5",
            "bedrock/eu.anthropic.claude-haiku-5-5",
            "anthropic/claude-sonnet-5",
            "eu.anthropic.claude-sonnet-5",
            "anthropic/claude-opus-5",
            "anthropic/claude-opus-4.7",
            "anthropic/claude-opus-4.8",
        ],
    )
    def test_documented_models_allow_disabling(self, model_id):
        assert supports_thinking_disable(model_id) is True

    @pytest.mark.parametrize(
        "model_id",
        [
            # Always-on / rejects `disabled` — and both are prefix collisions of allowed
            # families (claude-opus-5 / claude-sonnet-5), so the exclusion must win.
            "anthropic/claude-opus-5.5",
            "bedrock/eu.anthropic.claude-opus-5-5",
            "anthropic/claude-sonnet-5.5",
            "anthropic/claude-sonnet-5-5",
            # Fable / Mythos 400 on any explicit thinking field.
            "anthropic/claude-fable-5.1",
            "bedrock/global.anthropic.claude-fable-5-1",
            "anthropic/claude-mythos-5",
            # Older generation: thinking is off unless enabled, nothing to disable.
            "anthropic/claude-haiku-4.5",
            "anthropic/claude-sonnet-4.6",
            # Not Claude at all.
            "openai/gpt-5.5",
            "google/gemini-2.5-flash",
            "",
        ],
    )
    def test_everything_else_fails_safe(self, model_id):
        assert supports_thinking_disable(model_id) is False


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("anthropic/claude-haiku-5.5", 512),
        ("bedrock/eu.anthropic.claude-opus-5-5", 512),
        ("anthropic/claude-sonnet-5.5", 512),
        ("anthropic/claude-fable-5.1", 512),
        ("bedrock/global.anthropic.claude-fable-5", 512),
        ("anthropic/claude-mythos-5.1", 512),
        ("anthropic/claude-opus-5", 512),
        ("anthropic/claude-sonnet-5", 1024),
        ("anthropic/claude-opus-4.8", 1024),
        ("anthropic/claude-sonnet-4.6", 1024),
        ("us.anthropic.claude-sonnet-4-5-20250929-v1:0", 1024),
        ("us.anthropic.claude-haiku-4-5-20251001-v1:0", 4096),
        ("anthropic/claude-opus-4.5", 4096),
        ("anthropic/claude-opus-4.7", 4096),
        ("some/unknown-model", 4096),
    ],
)
def test_min_cacheable_tokens_per_family(model_id, expected):
    assert min_cacheable_tokens(model_id) == expected


class TestReasonsByDefault:
    """Which models spend output tokens on thinking when no thinking field is sent.

    The only consumer (``effective_max_tokens``) drops an auxiliary output cap on
    True, so a wrong True costs nothing but the cap, while a wrong False would let
    reasoning tokens truncate a judge verdict or a summary — unknown ids therefore
    default to True.
    """

    @pytest.mark.parametrize(
        "model_id",
        [
            # Claude 5.x: adaptive thinking on by default (langchain-aws
            # `thinking_on_by_default` + Haiku 5.5 model card).
            "anthropic/claude-haiku-5.5",
            "bedrock/eu.anthropic.claude-haiku-5-5",
            "eu.anthropic.claude-opus-5-5",
            "anthropic/claude-sonnet-5.5",
            "anthropic/claude-sonnet-5",
            "anthropic/claude-opus-5",
            "bedrock/global.anthropic.claude-fable-5-1",
            "anthropic/claude-mythos-5",
            # OpenAI reasoning models (OpenAIProvider forces effort=high on them).
            "openai/gpt-5.5",
            "gpt-5.1",
            "openai/o3",
            "o4-mini",
            # Gemini thinking models.
            "google/gemini-2.5-pro",
            "gemini-2.5-flash",
            "vertex/gemini-3.1-pro-preview",
            "gemini-4-argon",
            # Unknown vendors / open-ended catalogues: assumed to reason (fail-safe).
            "bedrock/eu.amazon.nova-pro-v1:0",
            "bedrock/openai.gpt-oss-120b-1:0",
            "deepseek/deepseek-r1",
            "ollama/qwen3:8b",
            "meta-llama/llama-3.3-70b-instruct",
            "",
        ],
    )
    def test_models_that_think_or_are_unknown(self, model_id):
        assert reasons_by_default(model_id) is True

    @pytest.mark.parametrize(
        "model_id",
        [
            # Claude 4.x thinks only when a `thinking` block is sent.
            "anthropic/claude-haiku-4.5",
            "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "anthropic/claude-sonnet-4.5",
            "anthropic/claude-sonnet-4.6",
            "anthropic/claude-opus-4.6",
            "anthropic/claude-opus-4.7",
            "anthropic/claude-opus-4.8",
            "bedrock/eu.anthropic.claude-opus-4-6-v1",
            # gpt-3 / gpt-4 families have no reasoning.
            "openai/gpt-4.1",
            "gpt-4o-mini",
            "openai/gpt-3.5-turbo",
            # Gemini flash-lite and pre-2.5 ids ship with thinking off.
            "google/gemini-2.5-flash-lite",
            "vertex/gemini-3.5-flash-lite",
            "gemini-2.0-flash",
        ],
    )
    def test_models_known_not_to_think(self, model_id):
        assert reasons_by_default(model_id) is False

    def test_gemini_helper_matches_the_provider_indicator_list(self):
        assert gemini_thinks_by_default("gemini-2.5-pro") is True
        assert gemini_thinks_by_default("gemini-3.6-flash") is True
        assert gemini_thinks_by_default("gemini-3.5-flash-lite") is False
        assert gemini_thinks_by_default("gemini-1.5-pro") is False


class TestEffectiveMaxTokens:
    """Review B3-01: an auxiliary cap only reaches a model whose thinking is off."""

    def test_no_cap_requested(self):
        assert effective_max_tokens(CallOptions(disable_thinking=True), "anthropic/claude-opus-5.5") is None

    def test_cap_without_disable_thinking_is_the_callers_decision(self):
        """The agent's 16K turn budget deliberately includes thinking tokens."""
        opts = CallOptions(max_tokens=16000)
        assert effective_max_tokens(opts, "anthropic/claude-opus-5.5") == 16000
        assert effective_max_tokens(opts, "openai/gpt-5.5") == 16000

    def test_kept_when_the_provider_switches_thinking_off(self):
        opts = CallOptions(max_tokens=400, disable_thinking=True)
        assert effective_max_tokens(opts, "eu.anthropic.claude-haiku-5-5", thinking_disabled=True) == 400

    @pytest.mark.parametrize(
        "model_id",
        ["anthropic/claude-haiku-4.5", "openai/gpt-4.1", "google/gemini-2.5-flash-lite"],
    )
    def test_kept_for_models_that_do_not_think_anyway(self, model_id):
        opts = CallOptions(max_tokens=32, disable_thinking=True)
        assert effective_max_tokens(opts, model_id) == 32

    @pytest.mark.parametrize(
        "model_id",
        [
            "eu.anthropic.claude-opus-5-5",  # native Bedrock, always-on thinking
            "anthropic/claude-haiku-5.5",  # gateway / OpenRouter: field not portable
            "openai/gpt-5.5",  # Responses API: max_output_tokens includes reasoning
            "google/gemini-2.5-pro",  # thinking cannot be disabled
            "ollama/qwen3:8b",  # unknown local model
        ],
    )
    def test_dropped_when_thinking_stays_on(self, model_id):
        opts = CallOptions(max_tokens=400, disable_thinking=True)
        assert effective_max_tokens(opts, model_id, thinking_disabled=False) is None


class TestPopCallOptions:
    def test_pops_only_the_neutral_keys(self):
        kwargs = {
            "max_tokens": "400",
            "request_timeout": 25,
            "disable_thinking": True,
            "streaming": False,
            "callbacks": None,
        }
        opts = pop_call_options(kwargs)
        assert opts == CallOptions(max_tokens=400, request_timeout=25.0, disable_thinking=True)
        assert kwargs == {"streaming": False, "callbacks": None}

    def test_defaults_when_absent(self):
        kwargs = {"temperature": 0.0}
        assert pop_call_options(kwargs) == CallOptions()
        assert kwargs == {"temperature": 0.0}

    def test_disable_thinking_is_coerced_to_bool(self):
        assert pop_call_options({"disable_thinking": None}).disable_thinking is False
        assert pop_call_options({"disable_thinking": 1}).disable_thinking is True


# ---------------------------------------------------------------------------
# max_tokens_for — central output caps with LLM_MAX_TOKENS_<TYPE> overrides
# ---------------------------------------------------------------------------

_MAX_TOKENS_VARS = (
    "LLM_MAX_TOKENS_JUDGE",
    "LLM_MAX_TOKENS_SEVERITY",
    "LLM_MAX_TOKENS_SUMMARY",
    "LLM_MAX_TOKENS_REPORT",
    "LLM_MAX_TOKENS_AGENT",
    "LLM_MAX_TOKENS_MEMORY_AGENT",
)


class TestMaxTokensFor:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        for var in _MAX_TOKENS_VARS:
            monkeypatch.delenv(var, raising=False)

    @pytest.mark.parametrize(
        ("call_type", "expected"),
        [
            ("judge", 400),
            ("severity", 32),
            ("summary", 1500),
            ("report", 4000),
            ("agent", 16000),
            ("memory_agent", 2000),
        ],
    )
    def test_defaults_match_the_calibrated_budgets(self, call_type, expected):
        assert max_tokens_for(call_type) == expected

    def test_defaults_sit_inside_the_brief_ranges(self):
        assert 300 <= max_tokens_for("judge") <= 500
        assert 16 <= max_tokens_for("severity") <= 32
        assert 800 <= max_tokens_for("summary") <= 1500
        assert 8000 <= max_tokens_for("agent") <= 16000
        assert 1000 <= max_tokens_for("memory_agent") <= 2000

    @pytest.mark.parametrize(
        ("call_type", "var"),
        [
            ("judge", "LLM_MAX_TOKENS_JUDGE"),
            ("severity", "LLM_MAX_TOKENS_SEVERITY"),
            ("summary", "LLM_MAX_TOKENS_SUMMARY"),
            ("report", "LLM_MAX_TOKENS_REPORT"),
            ("agent", "LLM_MAX_TOKENS_AGENT"),
            ("memory_agent", "LLM_MAX_TOKENS_MEMORY_AGENT"),
        ],
    )
    def test_env_override_wins(self, monkeypatch, call_type, var):
        monkeypatch.setenv(var, " 777 ")
        assert max_tokens_for(call_type) == 777

    @pytest.mark.parametrize("raw", ["", "   ", "abc", "0", "-50", "12.5"])
    def test_empty_or_junk_values_mean_default(self, monkeypatch, raw):
        """Docker Compose forwards ${VAR} as "" when unset; a typo must never yield a
        zero or negative cap."""
        monkeypatch.setenv("LLM_MAX_TOKENS_JUDGE", raw)
        assert max_tokens_for("judge") == 400

    def test_override_of_one_type_does_not_leak_to_others(self, monkeypatch):
        monkeypatch.setenv("LLM_MAX_TOKENS_JUDGE", "999")
        assert max_tokens_for("judge") == 999
        assert max_tokens_for("severity") == 32
        assert max_tokens_for("summary") == 1500

    def test_unknown_call_type_is_rejected(self):
        with pytest.raises(ValueError):
            max_tokens_for("vision")  # type: ignore[arg-type]
