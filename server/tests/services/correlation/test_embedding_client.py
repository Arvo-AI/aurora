"""EmbeddingClient: Bedrock credential chain / region resolution and the embed cache.

No network and no real boto3: the Bedrock path is exercised against a fake ``boto3``
module injected into ``sys.modules`` (the client imports boto3 lazily), so these run
in CI's minimal environment as well.
"""
import json
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from chat.backend.agent import bedrock_env
from services.correlation.embedding_client import EmbeddingClient

_TITAN = "bedrock/amazon.titan-embed-text-v2:0"
_REGION_VARS = ("BEDROCK_REGION", "AWS_REGION", "AWS_DEFAULT_REGION")
_CRED_VARS = (
    "BEDROCK_ACCESS_KEY_ID", "BEDROCK_SECRET_ACCESS_KEY", "BEDROCK_SESSION_TOKEN",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "BEDROCK_PROFILE",
)


@pytest.fixture
def clean_env(monkeypatch):
    for var in _REGION_VARS + _CRED_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


class _FakeConfig:
    """Stand-in for botocore.config.Config that just records its kwargs."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _titan_body(vector):
    body = MagicMock()
    body.read.return_value = json.dumps({"embedding": vector, "inputTextTokenCount": 3}).encode()
    return {"body": body}


@pytest.fixture
def fake_boto3(monkeypatch):
    """Inject fake boto3 / botocore modules; returns the fake boto3 module."""
    runtime = MagicMock(name="bedrock-runtime")
    runtime.invoke_model.return_value = _titan_body([0.1, 0.2, 0.3])

    boto3 = types.ModuleType("boto3")
    boto3.client = MagicMock(name="boto3.client", return_value=runtime)
    session = MagicMock(name="boto3.Session")
    session.return_value.client.return_value = runtime
    boto3.Session = session

    botocore = types.ModuleType("botocore")
    botocore_config = types.ModuleType("botocore.config")
    botocore_config.Config = _FakeConfig
    botocore.config = botocore_config

    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.config", botocore_config)
    boto3._runtime = runtime
    return boto3


def _client(monkeypatch, model=_TITAN) -> EmbeddingClient:
    monkeypatch.setenv("EMBEDDING_MODEL", model)
    return EmbeddingClient()


# ---------------------------------------------------------------------------
# bedrock_env: shared region + credential resolution
# ---------------------------------------------------------------------------


class TestBedrockEnv:
    def test_no_keys_means_default_credential_chain(self, clean_env):
        assert bedrock_env.bedrock_credential_kwargs() == {}

    def test_half_a_key_pair_is_ignored(self, clean_env):
        clean_env.setenv("BEDROCK_ACCESS_KEY_ID", "AKIAEXAMPLE")
        assert bedrock_env.bedrock_credential_kwargs() == {}

    def test_bedrock_keys_take_precedence_over_aws_keys(self, clean_env):
        clean_env.setenv("AWS_ACCESS_KEY_ID", "AKIAAWS")
        clean_env.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
        clean_env.setenv("BEDROCK_ACCESS_KEY_ID", "AKIABEDROCK")
        clean_env.setenv("BEDROCK_SECRET_ACCESS_KEY", "bedrock-secret")
        clean_env.setenv("BEDROCK_SESSION_TOKEN", "bedrock-token")
        assert bedrock_env.bedrock_credential_kwargs() == {
            "aws_access_key_id": "AKIABEDROCK",
            "aws_secret_access_key": "bedrock-secret",
            "aws_session_token": "bedrock-token",
        }

    def test_aws_keys_are_the_fallback_without_a_token(self, clean_env):
        clean_env.setenv("AWS_ACCESS_KEY_ID", "AKIAAWS")
        clean_env.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
        assert bedrock_env.bedrock_credential_kwargs() == {
            "aws_access_key_id": "AKIAAWS",
            "aws_secret_access_key": "aws-secret",
        }

    def test_region_precedence(self, clean_env):
        assert bedrock_env.resolve_bedrock_region() is None
        clean_env.setenv("AWS_DEFAULT_REGION", "us-east-1")
        assert bedrock_env.resolve_bedrock_region() == "us-east-1"
        clean_env.setenv("AWS_REGION", "eu-west-1")
        assert bedrock_env.resolve_bedrock_region() == "eu-west-1"
        clean_env.setenv("BEDROCK_REGION", "eu-central-1")
        assert bedrock_env.resolve_bedrock_region() == "eu-central-1"

    def test_blank_region_counts_as_unset(self, clean_env):
        clean_env.setenv("BEDROCK_REGION", "   ")
        clean_env.setenv("AWS_REGION", "eu-central-1")
        assert bedrock_env.resolve_bedrock_region() == "eu-central-1"

    def test_global_geo_maps_to_a_real_signing_region(self, clean_env):
        assert bedrock_env.resolve_client_region("eu-central-1") == "eu-central-1"
        assert bedrock_env.resolve_client_region(None) is None
        assert bedrock_env.resolve_client_region("global") == "us-east-1"
        clean_env.setenv("AWS_REGION", "eu-west-3")
        assert bedrock_env.resolve_client_region("global") == "eu-west-3"

    def test_provider_alias_shares_the_implementation(self, clean_env):
        from chat.backend.agent.providers import bedrock_provider

        clean_env.setenv("AWS_DEFAULT_REGION", "eu-north-1")
        assert bedrock_provider._resolve_client_region("global") == "eu-north-1"
        assert bedrock_provider._GLOBAL_HOME_REGION_FALLBACK == bedrock_env.GLOBAL_HOME_REGION_FALLBACK


# ---------------------------------------------------------------------------
# Bedrock embedding path
# ---------------------------------------------------------------------------


class TestBedrockEmbedding:
    def test_uses_default_chain_and_configured_region_without_static_keys(self, clean_env, fake_boto3):
        clean_env.setenv("BEDROCK_REGION", "eu-central-1")
        client = _client(clean_env)

        vector = client.embed("checkout-api p99 latency > 2s")

        assert vector == [0.1, 0.2, 0.3]
        fake_boto3.Session.assert_not_called()
        fake_boto3.client.assert_called_once()
        args, kwargs = fake_boto3.client.call_args
        assert args == ("bedrock-runtime",)
        assert kwargs["region_name"] == "eu-central-1"
        assert "aws_access_key_id" not in kwargs
        assert "aws_secret_access_key" not in kwargs
        config = kwargs["config"]
        assert config.kwargs["retries"] == {"mode": "standard", "total_max_attempts": 3}
        assert config.kwargs["connect_timeout"] == 5
        assert config.kwargs["read_timeout"] == 20

    def test_invoke_model_uses_the_base_in_region_titan_id(self, clean_env, fake_boto3):
        clean_env.setenv("BEDROCK_REGION", "eu-central-1")
        client = _client(clean_env)

        client.embed("hello")

        call = fake_boto3._runtime.invoke_model.call_args
        assert call.kwargs["modelId"] == "amazon.titan-embed-text-v2:0"  # no eu. prefix
        assert json.loads(call.kwargs["body"]) == {"inputText": "hello"}
        assert call.kwargs["contentType"] == "application/json"
        assert call.kwargs["accept"] == "application/json"

    def test_static_keys_are_passed_explicitly_when_both_are_set(self, clean_env, fake_boto3):
        clean_env.setenv("BEDROCK_REGION", "eu-central-1")
        clean_env.setenv("BEDROCK_ACCESS_KEY_ID", "AKIABEDROCK")
        clean_env.setenv("BEDROCK_SECRET_ACCESS_KEY", "bedrock-secret")
        client = _client(clean_env)

        client.embed("hello")

        kwargs = fake_boto3.client.call_args.kwargs
        assert kwargs["aws_access_key_id"] == "AKIABEDROCK"
        assert kwargs["aws_secret_access_key"] == "bedrock-secret"
        assert "aws_session_token" not in kwargs

    def test_global_geo_signs_against_a_real_region(self, clean_env, fake_boto3):
        clean_env.setenv("BEDROCK_REGION", "global")
        clean_env.setenv("AWS_REGION", "eu-west-1")
        client = _client(clean_env)

        client.embed("hello")

        assert fake_boto3.client.call_args.kwargs["region_name"] == "eu-west-1"

    def test_no_region_anywhere_keeps_the_historical_fallback(self, clean_env, fake_boto3):
        client = _client(clean_env)

        assert client.embed("hello") == [0.1, 0.2, 0.3]
        assert fake_boto3.client.call_args.kwargs["region_name"] == "us-east-1"

    def test_named_profile_builds_a_session(self, clean_env, fake_boto3):
        clean_env.setenv("BEDROCK_REGION", "eu-central-1")
        clean_env.setenv("BEDROCK_PROFILE", "aurora-bedrock")
        client = _client(clean_env)

        client.embed("hello")

        fake_boto3.Session.assert_called_once_with(profile_name="aurora-bedrock")
        fake_boto3.client.assert_not_called()
        session_client_kwargs = fake_boto3.Session.return_value.client.call_args.kwargs
        assert session_client_kwargs["region_name"] == "eu-central-1"

    def test_client_is_built_once(self, clean_env, fake_boto3):
        clean_env.setenv("BEDROCK_REGION", "eu-central-1")
        client = _client(clean_env)

        client.embed("one")
        client.embed("two")

        assert fake_boto3.client.call_count == 1
        assert fake_boto3._runtime.invoke_model.call_count == 2

    @pytest.mark.parametrize(
        "payload",
        [
            {"outputs": [{"text": "stub"}]},   # Floci / non-Titan stub shape
            {"embedding": []},
            {"embedding": "not-a-list"},
            {"embedding": [0.1, "x"]},
            [],
        ],
    )
    def test_unexpected_response_shape_returns_none_and_is_not_cached(self, clean_env, fake_boto3, payload):
        clean_env.setenv("BEDROCK_REGION", "eu-central-1")
        body = MagicMock()
        body.read.return_value = json.dumps(payload).encode()
        fake_boto3._runtime.invoke_model.return_value = {"body": body}
        client = _client(clean_env)

        assert client.embed("hello") is None
        assert client.embed("hello") is None
        assert fake_boto3._runtime.invoke_model.call_count == 2  # failures are retried, not cached

    def test_provider_error_returns_none(self, clean_env, fake_boto3):
        clean_env.setenv("BEDROCK_REGION", "eu-central-1")
        fake_boto3._runtime.invoke_model.side_effect = RuntimeError("AccessDeniedException")
        client = _client(clean_env)

        assert client.embed("hello") is None


# ---------------------------------------------------------------------------
# Embed cache (provider-agnostic)
# ---------------------------------------------------------------------------


class TestEmbedCache:
    def test_identical_text_hits_the_cache(self, clean_env):
        client = _client(clean_env)
        calls = []

        def fake_embed(text):
            calls.append(text)
            return [1.0, 0.0]

        with patch.object(client, "_embed_bedrock", side_effect=fake_embed):
            first = client.embed("same title")
            second = client.embed("same title")
            third = client.embed("another title")

        assert first == second == [1.0, 0.0]
        assert third == [1.0, 0.0]
        assert calls == ["same title", "another title"]
        assert (client.cache_hits, client.cache_misses) == (1, 2)

    def test_cached_vectors_are_copies(self, clean_env):
        client = _client(clean_env)
        with patch.object(client, "_embed_bedrock", return_value=[1.0, 0.0]):
            first = client.embed("t")
            first.append(99.0)
            second = client.embed("t")
        assert second == [1.0, 0.0]

    def test_none_results_are_not_cached(self, clean_env):
        client = _client(clean_env)
        with patch.object(client, "_embed_bedrock", return_value=None) as embed:
            assert client.embed("t") is None
            assert client.embed("t") is None
        assert embed.call_count == 2

    def test_key_is_scoped_to_provider_and_model(self, clean_env):
        a = _client(clean_env, "bedrock/amazon.titan-embed-text-v2:0")
        b = _client(clean_env, "openai/text-embedding-3-small")
        assert a._cache_key("x") != b._cache_key("x")
        # Deterministic across instances of the same model (the Redis key must be shared).
        same_model = _client(clean_env, "bedrock/amazon.titan-embed-text-v2:0")
        assert same_model._cache_key("x") == a._cache_key("x")

    def test_cache_is_bounded(self, clean_env, monkeypatch):
        import services.correlation.embedding_client as mod

        monkeypatch.setattr(mod, "EMBED_CACHE_MAXSIZE", 2)
        client = _client(clean_env)
        with patch.object(client, "_embed_bedrock", return_value=[1.0]) as embed:
            client.embed("a")
            client.embed("b")
            client.embed("c")   # evicts "a"
            client.embed("a")   # miss again
        assert embed.call_count == 4
        assert len(client._cache) == 2

    def test_unconfigured_or_blank_text_never_touches_the_cache(self, clean_env):
        clean_env.delenv("EMBEDDING_MODEL", raising=False)
        client = EmbeddingClient()
        assert client.embed("x") is None
        configured = _client(clean_env)
        assert configured.embed("   ") is None
        assert (configured.cache_hits, configured.cache_misses) == (0, 0)


# ---------------------------------------------------------------------------
# Correlation run: the incoming alert is embedded once, not once per candidate
# ---------------------------------------------------------------------------


def test_similarity_strategy_embeds_each_distinct_title_once(clean_env):
    from services.correlation.strategies.similarity import SimilarityStrategy

    client = _client(clean_env)
    calls = []

    def fake_embed(text):
        calls.append(text)
        return [1.0, 0.5, 0.25]

    strategy = SimilarityStrategy()
    with patch.object(client, "_embed_bedrock", side_effect=fake_embed), \
         patch("services.correlation.strategies.similarity.get_embedding_client", return_value=client):
        for incident_title in ("db latency", "cache evictions", "db latency"):
            score = strategy.score("checkout 5xx", "checkout", incident_title, ["checkout"])
            assert 0.0 <= score <= 1.0

    # 3 comparisons used to cost 6 InvokeModel calls; now the alert title is embedded
    # once and each distinct incident title once.
    assert calls == ["checkout 5xx", "db latency", "cache evictions"]
