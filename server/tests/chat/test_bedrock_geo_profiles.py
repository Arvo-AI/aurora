"""Bedrock inference-profile id derivation (geo prefix + region resolution).

Covers the two gaps flagged in review on #666:
 - Fable 5 / 5.1 have no ``eu.`` or ``apac.`` profile, so deriving one 404s.
 - ``BEDROCK_REGION=global`` was special-cased in pricing but unreachable in the
   provider, because ``global`` is not an AWS region boto3 can sign against.
"""
import importlib.util
import os
import sys
import types

import pytest

_SERVER_DIR = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
_PROVIDER_PATH = os.path.join(
    _SERVER_DIR, "chat", "backend", "agent", "providers", "bedrock_provider.py"
)


@pytest.fixture()
def bedrock_module(monkeypatch):
    """Load bedrock_provider with the langchain/relative-import surface stubbed.

    Loaded by path (not imported as a package) so the test does not drag in the whole
    providers package — the module only needs ModelMapper and a ChatOpenAI symbol.
    """
    from chat.backend.agent.model_mapper import ModelMapper

    langchain_openai = types.ModuleType("langchain_openai")
    langchain_openai.ChatOpenAI = type("ChatOpenAI", (), {})
    monkeypatch.setitem(sys.modules, "langchain_openai", langchain_openai)

    pkg_name = "_bedrock_pkg_under_test"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = []
    monkeypatch.setitem(sys.modules, pkg_name, pkg)

    # Relative imports (`.base_provider`, `._sampling_guard`, `..model_mapper`).
    base_provider = types.ModuleType(f"{pkg_name}.base_provider")
    base_provider.BaseLLMProvider = type("BaseLLMProvider", (), {"__init__": lambda self: None})
    monkeypatch.setitem(sys.modules, f"{pkg_name}.base_provider", base_provider)

    sampling_guard = types.ModuleType(f"{pkg_name}._sampling_guard")
    sampling_guard.make_adaptive_sampling_cls = lambda cls, **kw: cls
    monkeypatch.setitem(sys.modules, f"{pkg_name}._sampling_guard", sampling_guard)

    mapper_mod = types.ModuleType("_bedrock_parent_model_mapper")
    mapper_mod.ModelMapper = ModelMapper
    monkeypatch.setitem(sys.modules, "_bedrock_parent_model_mapper", mapper_mod)

    with open(_PROVIDER_PATH, encoding="utf-8") as f:
        source = f.read()
    source = source.replace("from ..model_mapper import", "from _bedrock_parent_model_mapper import")

    spec = importlib.util.spec_from_loader(f"{pkg_name}.bedrock_provider", loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__package__ = pkg_name
    monkeypatch.setitem(sys.modules, f"{pkg_name}.bedrock_provider", module)
    exec(compile(source, _PROVIDER_PATH, "exec"), module.__dict__)
    return module


def _provider(bedrock_module, region):
    provider = bedrock_module.BedrockProvider.__new__(bedrock_module.BedrockProvider)
    provider.base_url = None
    provider.region = region
    return provider


class TestGeoPrefix:
    @pytest.mark.parametrize(
        ("region", "expected"),
        [
            ("us-east-1", "us"),
            ("eu-west-1", "eu"),
            ("ap-southeast-2", "apac"),
            ("", "us"),
            (None, "us"),
            # `global` selects Bedrock's global profiles, which bill at the direct rate.
            ("global", "global"),
            ("GLOBAL", "global"),
        ],
    )
    def test_geo_prefix_for_region(self, bedrock_module, region, expected):
        assert bedrock_module._geo_prefix_for(region) == expected


class TestClientRegionResolution:
    """`global` is a profile geo, not an AWS endpoint — boto3 needs a real region."""

    def test_real_region_passes_through(self, bedrock_module):
        assert bedrock_module._resolve_client_region("eu-west-1") == "eu-west-1"

    def test_global_falls_back_to_aws_region(self, bedrock_module, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "eu-central-1")
        assert bedrock_module._resolve_client_region("global") == "eu-central-1"

    def test_global_falls_back_to_aws_default_region(self, bedrock_module, monkeypatch):
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
        assert bedrock_module._resolve_client_region("global") == "ap-south-1"

    def test_global_falls_back_to_us_east_1(self, bedrock_module, monkeypatch):
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        assert bedrock_module._resolve_client_region("global") == "us-east-1"

    def test_global_never_returned_as_a_client_region(self, bedrock_module, monkeypatch):
        """Even if the AWS vars also say `global`, boto3 must not receive it."""
        monkeypatch.setenv("AWS_REGION", "global")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "global")
        assert bedrock_module._resolve_client_region("global") == "us-east-1"


class TestInferenceProfileIds:
    def test_us_region_derives_us_profile(self, bedrock_module):
        provider = _provider(bedrock_module, "us-east-1")
        assert provider.get_native_model_name("anthropic/claude-opus-5.5") == (
            "us.anthropic.claude-opus-5-5"
        )

    def test_eu_region_derives_eu_profile_for_normal_models(self, bedrock_module):
        provider = _provider(bedrock_module, "eu-west-1")
        assert provider.get_native_model_name("anthropic/claude-opus-5.5") == (
            "eu.anthropic.claude-opus-5-5"
        )

    @pytest.mark.parametrize("model", ["anthropic/claude-fable-5.1", "anthropic/claude-fable-5"])
    @pytest.mark.parametrize("region", ["eu-west-1", "ap-southeast-2"])
    def test_fable_uses_global_profile_outside_us(self, bedrock_module, model, region):
        """AWS publishes Fable only as us./global. — an eu./apac. id would 404."""
        provider = _provider(bedrock_module, region)
        native = provider.get_native_model_name(model)
        assert native.startswith("global."), native

    @pytest.mark.parametrize("model", ["anthropic/claude-fable-5.1", "anthropic/claude-fable-5"])
    def test_fable_still_uses_us_profile_in_us(self, bedrock_module, model):
        provider = _provider(bedrock_module, "us-east-1")
        assert provider.get_native_model_name(model).startswith("us.")

    def test_global_region_derives_global_profile(self, bedrock_module):
        provider = _provider(bedrock_module, "global")
        assert provider.get_native_model_name("anthropic/claude-opus-5.5") == (
            "global.anthropic.claude-opus-5-5"
        )

    def test_dated_models_keep_their_version_suffix(self, bedrock_module):
        provider = _provider(bedrock_module, "us-east-1")
        assert provider.get_native_model_name("anthropic/claude-haiku-4.5") == (
            "us.anthropic.claude-haiku-4-5-20251001-v1:0"
        )

    @pytest.mark.parametrize("model", ["anthropic/claude-haiku-5.5", "anthropic/claude-haiku-5-5"])
    def test_cheap_tier_derives_the_undated_eu_profile(self, bedrock_module, model):
        """Haiku 5.5's Bedrock id has no date suffix (model card: eu.anthropic.claude-haiku-5-5)."""
        provider = _provider(bedrock_module, "eu-central-1")
        assert provider.get_native_model_name(model) == "eu.anthropic.claude-haiku-5-5"

    def test_legacy_haiku_alias_still_resolves(self, bedrock_module):
        """Existing RCA_MODEL=anthropic/claude-haiku-4.5 configs must keep working."""
        provider = _provider(bedrock_module, "eu-west-1")
        assert provider.get_native_model_name("anthropic/claude-haiku-4.5") == (
            "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
        )

    def test_explicit_bedrock_id_passes_through(self, bedrock_module):
        provider = _provider(bedrock_module, "eu-west-1")
        assert provider.get_native_model_name("bedrock/global.anthropic.claude-fable-5-1") == (
            "global.anthropic.claude-fable-5-1"
        )

    def test_non_anthropic_model_passes_through(self, bedrock_module):
        provider = _provider(bedrock_module, "us-east-1")
        assert provider.get_native_model_name("meta/llama-3") == "meta/llama-3"


class TestNativeMode:
    def test_native_without_gateway_url(self, bedrock_module):
        assert _provider(bedrock_module, "eu-west-1").is_native_mode is True

    def test_gateway_when_base_url_set(self, bedrock_module):
        provider = _provider(bedrock_module, "eu-west-1")
        provider.base_url = "https://gateway.internal/v1"
        assert provider.is_native_mode is False


class TestCallOptionTranslation:
    """Aurora's neutral call options -> ChatBedrockConverse / gateway constructor fields.

    Pure function, no client construction: `request_timeout` must never reach
    ChatBedrockConverse (it would be moved into the request body and rejected by
    Bedrock), and `thinking: disabled` may only be sent to models that accept it.
    """

    def test_haiku_5_5_native_gets_thinking_disabled_and_timeout(self, bedrock_module):
        kwargs = {"max_tokens": 400, "request_timeout": 25.5, "disable_thinking": True, "streaming": False}
        fields = bedrock_module._translate_call_options(
            "eu.anthropic.claude-haiku-5-5", kwargs, native_mode=True
        )
        assert fields == {
            "max_tokens": 400,
            # Consumed by get_chat_model into the botocore Config — never the class's
            # own `timeout` field, whose merge would null the retry policy.
            "_read_timeout": 26,
            "additional_model_request_fields": {"thinking": {"type": "disabled"}},
        }
        # Neutral keys are consumed; unrelated kwargs stay for the caller to forward.
        assert kwargs == {"streaming": False}
        assert "request_timeout" not in fields
        assert "timeout" not in fields

    def test_opus_5_5_never_gets_a_thinking_field(self, bedrock_module):
        """Opus 5.5 runs thinking always-on; an explicit `disabled` would 400."""
        fields = bedrock_module._translate_call_options(
            "eu.anthropic.claude-opus-5-5", {"disable_thinking": True}, native_mode=True
        )
        assert fields == {}

    @pytest.mark.parametrize(
        "native_model",
        [
            "eu.anthropic.claude-opus-5-5",
            "eu.anthropic.claude-sonnet-5-5",
            "global.anthropic.claude-fable-5-1",
            "us.anthropic.claude-mythos-5",
        ],
    )
    def test_native_models_that_keep_thinking_lose_the_auxiliary_cap(self, bedrock_module, native_model):
        """Review B3-01: a cap sized for a thinking-free verdict (judge 400, severity
        32) must not reach a model whose thinking cannot be switched off — reasoning
        tokens would exhaust it and the answer would come back empty / without the
        tool call, so the fail-closed judge would block everything. The cap is
        dropped and the model keeps its default output limit, as on main."""
        fields = bedrock_module._translate_call_options(
            native_model, {"disable_thinking": True, "max_tokens": 400}, native_mode=True
        )
        assert fields == {}

    @pytest.mark.parametrize(
        "native_model",
        ["us.anthropic.claude-haiku-4-5-20251001-v1:0", "eu.anthropic.claude-sonnet-4-5-20250929-v1:0", "eu.anthropic.claude-opus-4-6-v1"],
    )
    def test_legacy_generation_drops_the_flag_but_keeps_the_cap(self, bedrock_module, native_model):
        """Claude 4.x thinks only when a `thinking` block is sent (never for these
        calls), so there is nothing to disable and the cap is safe."""
        fields = bedrock_module._translate_call_options(
            native_model, {"disable_thinking": True, "max_tokens": 100}, native_mode=True
        )
        assert fields == {"max_tokens": 100}

    def test_agent_cap_without_disable_thinking_always_passes_through(self, bedrock_module):
        """The agent's per-turn budget is set WITHOUT disable_thinking (thinking
        tokens are meant to count against it) and is never second-guessed."""
        fields = bedrock_module._translate_call_options(
            "eu.anthropic.claude-opus-5-5", {"max_tokens": 16000}, native_mode=True
        )
        assert fields == {"max_tokens": 16000}

    def test_existing_additional_fields_are_merged_not_replaced(self, bedrock_module):
        kwargs = {"disable_thinking": True, "additional_model_request_fields": {"top_k": 5}}
        fields = bedrock_module._translate_call_options(
            "eu.anthropic.claude-haiku-5-5", kwargs, native_mode=True
        )
        assert fields["additional_model_request_fields"] == {"top_k": 5, "thinking": {"type": "disabled"}}
        assert "additional_model_request_fields" not in kwargs

    def test_gateway_mode_keeps_openai_spelling_and_drops_thinking(self, bedrock_module):
        """Through the gateway the thinking field is not portable, so Haiku 5.5 keeps
        thinking by default — the auxiliary cap goes with the flag (B3-01)."""
        fields = bedrock_module._translate_call_options(
            "eu.anthropic.claude-haiku-5-5",
            {"max_tokens": 300, "request_timeout": 25, "disable_thinking": True},
            native_mode=False,
        )
        assert fields == {"request_timeout": 25.0}

    def test_gateway_mode_keeps_the_cap_for_the_legacy_generation(self, bedrock_module):
        fields = bedrock_module._translate_call_options(
            "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
            {"max_tokens": 300, "request_timeout": 25, "disable_thinking": True},
            native_mode=False,
        )
        assert fields == {"max_tokens": 300, "request_timeout": 25.0}

    def test_gateway_mode_agent_cap_passes_through(self, bedrock_module):
        fields = bedrock_module._translate_call_options(
            "eu.anthropic.claude-opus-5-5", {"max_tokens": 16000}, native_mode=False
        )
        assert fields == {"max_tokens": 16000}

    def test_no_options_means_no_fields(self, bedrock_module):
        kwargs = {"streaming": False}
        assert bedrock_module._translate_call_options("eu.anthropic.claude-haiku-5-5", kwargs, native_mode=True) == {}
        assert kwargs == {"streaming": False}


class TestBotocoreRetryConfig:
    """Native-mode clients get `standard` retries + bounded timeouts (brief item 4b).

    Needs botocore (pinned via boto3 in requirements.txt); the CI pre-checks job
    runs with a minimal dependency set, so skip there.
    """

    @pytest.fixture(autouse=True)
    def _botocore(self):
        pytest.importorskip("botocore")

    def test_defaults(self, bedrock_module, monkeypatch):
        monkeypatch.delenv("BEDROCK_READ_TIMEOUT", raising=False)
        monkeypatch.delenv("BEDROCK_MAX_ATTEMPTS", raising=False)
        cfg = bedrock_module._botocore_retry_config()
        assert cfg.retries == {"mode": "standard", "total_max_attempts": 3}
        assert cfg.connect_timeout == 10
        assert cfg.read_timeout == 300

    def test_env_overrides(self, bedrock_module, monkeypatch):
        monkeypatch.setenv("BEDROCK_READ_TIMEOUT", "120")
        monkeypatch.setenv("BEDROCK_MAX_ATTEMPTS", "5")
        cfg = bedrock_module._botocore_retry_config()
        assert cfg.retries == {"mode": "standard", "total_max_attempts": 5}
        assert cfg.read_timeout == 120

    @pytest.mark.parametrize("raw", ["", "  ", "abc", "0", "-7"])
    def test_junk_env_values_fall_back_to_defaults(self, bedrock_module, monkeypatch, raw):
        monkeypatch.setenv("BEDROCK_READ_TIMEOUT", raw)
        monkeypatch.setenv("BEDROCK_MAX_ATTEMPTS", raw)
        cfg = bedrock_module._botocore_retry_config()
        assert cfg.retries["total_max_attempts"] == 3
        assert cfg.read_timeout == 300

    def test_per_call_timeout_keeps_retries_and_bounds_connect(self, bedrock_module, monkeypatch):
        """A 25 s classifier call must not lose the retry policy (the library's own
        `timeout` field would merge `retries=None` over it) and must not wait 10 s
        to connect when the whole call is allowed less than that."""
        monkeypatch.delenv("BEDROCK_MAX_ATTEMPTS", raising=False)
        cfg = bedrock_module._botocore_retry_config(read_timeout=25)
        assert cfg.retries == {"mode": "standard", "total_max_attempts": 3}
        assert cfg.read_timeout == 25
        assert cfg.connect_timeout == 10
        tiny = bedrock_module._botocore_retry_config(read_timeout=5)
        assert tiny.connect_timeout == 5

    def test_merging_the_library_timeout_field_would_null_retries(self, bedrock_module):
        """Documents why `request_timeout` is folded into the Config instead of
        becoming ChatBedrockConverse.timeout (langchain-aws 1.9.0 `_get_effective_config`)."""
        from botocore.config import Config

        base = bedrock_module._botocore_retry_config()
        library_override = Config(connect_timeout=25, read_timeout=25, retries=None)
        assert base.merge(library_override).retries is None
