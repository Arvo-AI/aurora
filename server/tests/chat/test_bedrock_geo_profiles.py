"""Bedrock inference-profile id derivation (geo prefix + region resolution).

Covers the two gaps Noah flagged on #666:
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

    source = open(_PROVIDER_PATH, encoding="utf-8").read()
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

    def test_explicit_bedrock_id_passes_through(self, bedrock_module):
        provider = _provider(bedrock_module, "eu-west-1")
        assert provider.get_native_model_name("bedrock/global.anthropic.claude-fable-5-1") == (
            "global.anthropic.claude-fable-5-1"
        )

    def test_non_anthropic_model_passes_through(self, bedrock_module):
        provider = _provider(bedrock_module, "us-east-1")
        assert provider.get_native_model_name("meta/llama-3") == "meta/llama-3"
