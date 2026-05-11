"""Tests the OAuth2 CSRF state cache shared by every OAuth2 connector
(Atlassian, Bitbucket, Confluence, Google Chat, Notion, OVH, SharePoint).
Pins atomic single-use retrieval (replay protection) and the no-silent-
in-memory-fallback rule -- both regressions would compromise the
authorize/callback handshake for all OAuth2 integrations at once.
Also pins adversarial replay, cross-user CSRF substitution, and concurrent
race conditions on the single-use guarantee (TestStateReplayAndCSRF).
"""

import importlib
import json
import os
import sys
import threading
import time
from unittest.mock import MagicMock

import pytest

# REDIS_URL must be set before import: the module pings Redis at import time
# and raises RuntimeError if the env var is missing.
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from utils.auth import oauth2_state_cache as cache_module  # noqa: E402
from utils.auth.oauth2_state_cache import (  # noqa: E402
    clear_oauth2_states,
    retrieve_oauth2_state,
    store_oauth2_state,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_redis(monkeypatch):
    """Replace the module's Redis client with a controllable MagicMock."""
    fake = MagicMock(name="redis_client")
    monkeypatch.setattr(cache_module, "redis_client", fake)
    return fake


@pytest.fixture
def redis_exceptions(monkeypatch):
    """Swap ``redis.exceptions`` MagicMocks for real classes the cache can raise/catch."""

    class _RedisError(Exception):
        pass

    class _ResponseError(_RedisError):
        pass

    class _ConnectionError(_RedisError):
        pass

    class _TimeoutError(_RedisError):
        pass

    monkeypatch.setattr(cache_module.redis.exceptions, "RedisError", _RedisError)
    monkeypatch.setattr(cache_module.redis.exceptions, "ResponseError", _ResponseError)
    monkeypatch.setattr(
        cache_module.redis.exceptions, "ConnectionError", _ConnectionError,
    )
    monkeypatch.setattr(cache_module.redis.exceptions, "TimeoutError", _TimeoutError)

    return MagicMock(
        RedisError=_RedisError,
        ResponseError=_ResponseError,
        ConnectionError=_ConnectionError,
        TimeoutError=_TimeoutError,
    )


def _setex_payload(fake_redis):
    fake_redis.setex.assert_called()
    _, _, raw = fake_redis.setex.call_args.args
    return json.loads(raw)


def _in_memory_redis(fake_redis):
    """Wire ``fake_redis`` to behave like a minimal in-process Redis store.

    Returns the backing dict so tests can inspect or mutate it directly.
    """
    backing = {}
    lock = threading.Lock()

    def _setex(key, _ttl, value):
        with lock:
            backing[key] = value

    def _getdel(key):
        with lock:
            return backing.pop(key, None)

    fake_redis.setex.side_effect = _setex
    fake_redis.getdel.side_effect = _getdel
    return backing


# ---------------------------------------------------------------------------
# store_oauth2_state
# ---------------------------------------------------------------------------


class TestStoreOauth2State:
    """Writes must be namespaced, time-bounded, and faithful to inputs."""

    def test_uses_namespaced_redis_key(self, fake_redis):
        """The key MUST be ``oauth2:state:<state>`` so retrieve can find it."""
        store_oauth2_state(state="abc123", user_id="u-1", endpoint="ovh-eu")

        key, _, _ = fake_redis.setex.call_args.args
        assert key == "oauth2:state:abc123"

    def test_writes_with_30_minute_ttl(self, fake_redis):
        """30-minute TTL is the documented security boundary.

        Also pins the type: a float or timedelta that Redis silently rounds to
        0 would make every token immediately stale without surfacing an error.
        """
        store_oauth2_state(state="abc123", user_id="u-1", endpoint="ovh-eu")

        _, ttl_seconds, _ = fake_redis.setex.call_args.args
        assert ttl_seconds == 1800
        assert isinstance(ttl_seconds, int)

    def test_payload_includes_user_id_endpoint_and_timestamp(self, fake_redis):
        store_oauth2_state(state="abc123", user_id="u-1", endpoint="ovh-eu")

        payload = _setex_payload(fake_redis)
        assert payload["user_id"] == "u-1"
        assert payload["endpoint"] == "ovh-eu"
        assert "timestamp" in payload

    def test_optional_fields_included_when_provided(self, fake_redis):
        store_oauth2_state(
            state="abc123",
            user_id="u-1",
            endpoint="ovh-eu",
            project_id="proj-9",
            code_verifier="pkce-verifier",
        )

        payload = _setex_payload(fake_redis)
        assert payload["project_id"] == "proj-9"
        assert payload["code_verifier"] == "pkce-verifier"

    def test_optional_fields_omitted_when_not_provided(self, fake_redis):
        """Don't write empty/None values: keep payloads tight and predictable."""
        store_oauth2_state(state="abc123", user_id="u-1", endpoint="ovh-eu")

        payload = _setex_payload(fake_redis)
        assert "project_id" not in payload
        assert "code_verifier" not in payload

    def test_payload_is_json_serializable(self, fake_redis):
        """``setex`` is called with a JSON string, never a raw dict."""
        store_oauth2_state(state="abc123", user_id="u-1", endpoint="ovh-eu")

        _, _, raw = fake_redis.setex.call_args.args
        assert isinstance(raw, str)
        json.loads(raw)


# ---------------------------------------------------------------------------
# retrieve_oauth2_state -- atomic single-use
# ---------------------------------------------------------------------------


class TestRetrieveOauth2State:
    """Retrieval must consume the token in one shot. Replay is a CVE class."""

    def test_returns_decoded_state(self, fake_redis):
        fake_redis.getdel.return_value = json.dumps(
            {"user_id": "u-1", "endpoint": "ovh-eu", "timestamp": "1700000000"},
        )

        result = retrieve_oauth2_state("abc123")

        assert result == {
            "user_id": "u-1",
            "endpoint": "ovh-eu",
            "timestamp": "1700000000",
        }

    def test_uses_getdel_for_atomic_consumption(self, fake_redis):
        """``GETDEL`` is the whole point: read and delete in one round trip."""
        fake_redis.getdel.return_value = json.dumps({"user_id": "u-1"})

        retrieve_oauth2_state("abc123")

        fake_redis.getdel.assert_called_once_with("oauth2:state:abc123")

    def test_returns_none_when_state_not_found(self, fake_redis):
        """Missing key (expired or invalid) -> None, never a stale dict."""
        fake_redis.getdel.return_value = None

        assert retrieve_oauth2_state("abc123") is None

    def test_returns_none_on_malformed_json(self, fake_redis):
        """Corrupt payload -> None. Don't crash the OAuth callback."""
        fake_redis.getdel.return_value = "{not json"

        assert retrieve_oauth2_state("abc123") is None

    def test_falls_back_to_pipeline_only_on_response_error(
        self, fake_redis, redis_exceptions,
    ):
        """Redis < 6.2 lacks GETDEL and raises ResponseError; fall back to GET+DEL."""
        fake_redis.getdel.side_effect = redis_exceptions.ResponseError("unknown command")

        pipe = MagicMock(name="pipeline")
        pipe.execute.return_value = [json.dumps({"user_id": "u-1"}), 1]
        fake_redis.pipeline.return_value = pipe

        result = retrieve_oauth2_state("abc123")

        pipe.get.assert_called_once_with("oauth2:state:abc123")
        pipe.delete.assert_called_once_with("oauth2:state:abc123")
        pipe.execute.assert_called_once()
        assert result == {"user_id": "u-1"}


# ---------------------------------------------------------------------------
# Roundtrip: store -> retrieve -> single-use deletion
# ---------------------------------------------------------------------------


class TestStoreRetrieveRoundtrip:
    """End-to-end shape contract between store and retrieve."""

    def test_data_written_by_store_is_visible_to_retrieve(self, fake_redis):
        """Whatever ``store`` serializes, ``retrieve`` must deserialize cleanly."""
        backing = {}

        def _setex(key, _ttl, value):
            backing[key] = value

        def _getdel(key):
            return backing.pop(key, None)

        fake_redis.setex.side_effect = _setex
        fake_redis.getdel.side_effect = _getdel

        store_oauth2_state(
            state="abc123",
            user_id="u-1",
            endpoint="ovh-eu",
            project_id="proj-9",
            code_verifier="pkce-verifier",
        )
        result = retrieve_oauth2_state("abc123")

        assert result["user_id"] == "u-1"
        assert result["endpoint"] == "ovh-eu"
        assert result["project_id"] == "proj-9"
        assert result["code_verifier"] == "pkce-verifier"

    def test_state_can_only_be_retrieved_once(self, fake_redis):
        """Replay protection: a second retrieve of the same state -> None."""
        backing = {}

        def _setex(key, _ttl, value):
            backing[key] = value

        def _getdel(key):
            return backing.pop(key, None)

        fake_redis.setex.side_effect = _setex
        fake_redis.getdel.side_effect = _getdel

        store_oauth2_state(state="abc123", user_id="u-1", endpoint="ovh-eu")

        first = retrieve_oauth2_state("abc123")
        second = retrieve_oauth2_state("abc123")

        assert first is not None
        assert second is None


# ---------------------------------------------------------------------------
# No insecure fallback when Redis fails
# ---------------------------------------------------------------------------


class TestNoInsecureFallback:
    """Redis errors must surface; the module must never silently switch to memory."""

    def test_store_propagates_redis_errors(self, fake_redis, redis_exceptions):
        """``store`` must raise on Redis failure -- no in-memory shadow store."""
        fake_redis.setex.side_effect = redis_exceptions.RedisError("boom")

        with pytest.raises(redis_exceptions.RedisError):
            store_oauth2_state(state="abc123", user_id="u-1", endpoint="ovh-eu")

    def test_retrieve_propagates_connection_errors(self, fake_redis, redis_exceptions):
        """ConnectionError must NOT be caught: a quiet ``None`` would look like 'no matching state'."""
        fake_redis.getdel.side_effect = redis_exceptions.ConnectionError("redis down")

        with pytest.raises(redis_exceptions.ConnectionError):
            retrieve_oauth2_state("abc123")

    def test_clear_propagates_redis_errors(self, fake_redis, redis_exceptions):
        """Even the destructive ``clear`` path must surface failures."""
        fake_redis.keys.side_effect = redis_exceptions.RedisError("boom")

        with pytest.raises(redis_exceptions.RedisError):
            clear_oauth2_states()

    def test_module_refuses_to_load_without_redis_url(self, monkeypatch):
        """No ``REDIS_URL`` -> RuntimeError; no default localhost, no in-memory fallback."""
        monkeypatch.delenv("REDIS_URL", raising=False)
        sys.modules.pop("utils.auth.oauth2_state_cache", None)

        try:
            with pytest.raises(RuntimeError, match="REDIS_URL"):
                importlib.import_module("utils.auth.oauth2_state_cache")
        finally:
            sys.modules["utils.auth.oauth2_state_cache"] = cache_module


# ---------------------------------------------------------------------------
# clear_oauth2_states -- targeting and emptiness
# ---------------------------------------------------------------------------


class TestClearOauth2States:
    """``clear`` must touch only OAuth2 keys and tolerate empty state."""

    def test_clear_only_targets_oauth_namespace(self, fake_redis):
        """Pattern MUST be scoped: ``oauth2:state:*``, not ``*``."""
        fake_redis.keys.return_value = ["oauth2:state:abc", "oauth2:state:def"]

        clear_oauth2_states()

        fake_redis.keys.assert_called_once_with("oauth2:state:*")
        fake_redis.delete.assert_called_once_with(
            "oauth2:state:abc", "oauth2:state:def",
        )

    def test_clear_with_no_keys_does_not_call_delete(self, fake_redis):
        """``DEL`` with zero args is a Redis error; skip the call."""
        fake_redis.keys.return_value = []

        clear_oauth2_states()

        fake_redis.delete.assert_not_called()


# ---------------------------------------------------------------------------
# Replay, cross-user CSRF, and TTL adversarial cases
# ---------------------------------------------------------------------------


class TestStateReplayAndCSRF:
    """State tokens must be single-use, owner-bound, and indistinguishable once consumed."""

    def test_concurrent_retrieve_exactly_one_wins(self, fake_redis):
        """Under concurrent load only one caller must receive the token payload."""
        _in_memory_redis(fake_redis)

        store_oauth2_state(state="tok-race", user_id="u-1", endpoint="ovh-eu")

        results = []
        barrier = threading.Barrier(10)

        def _retrieve():
            barrier.wait()
            results.append(retrieve_oauth2_state("tok-race"))

        threads = [threading.Thread(target=_retrieve) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        non_none = [r for r in results if r is not None]
        assert len(non_none) == 1, (
            f"Expected exactly 1 winner in concurrent GETDEL race, got {len(non_none)}"
        )

    def test_cross_user_payload_reflects_stored_owner(self, fake_redis):
        """A state token issued for user A cannot be silently re-attributed to user B.

        The cache module stores user_id faithfully and returns it verbatim;
        it is the route layer's responsibility to compare result["user_id"]
        against the authenticated caller.  This test pins that the returned
        payload can never claim a different owner than the one who created it.
        """
        _in_memory_redis(fake_redis)

        store_oauth2_state(state="tok-csrf", user_id="user-A", endpoint="ovh-eu")

        result = retrieve_oauth2_state("tok-csrf")

        assert result is not None
        assert result["user_id"] == "user-A"
        assert result["user_id"] != "user-B"

    def test_state_for_user_a_is_not_retrievable_after_consumed_by_attacker(
        self, fake_redis
    ):
        """Once the token is consumed -- by anyone -- it is gone for everyone."""
        _in_memory_redis(fake_redis)

        store_oauth2_state(state="tok-gone", user_id="user-A", endpoint="ovh-eu")

        attacker_result = retrieve_oauth2_state("tok-gone")
        legitimate_result = retrieve_oauth2_state("tok-gone")

        assert attacker_result is not None
        assert legitimate_result is None

    def test_unknown_state_is_indistinguishable_from_consumed(self, fake_redis):
        """A state token that was never stored returns None — same as an expired
        or already-consumed one. The caller can never tell the difference, which
        is the intended security property: there is no oracle for token existence.
        """
        fake_redis.getdel.return_value = None

        result = retrieve_oauth2_state("tok-never-existed")

        assert result is None

    def test_expired_state_returns_none(self, fake_redis):
        """After TTL expiry Redis evicts the key; retrieval must return None."""
        backing = {}

        def _setex(key, ttl, value):
            backing[key] = (value, time.monotonic() + ttl)

        def _getdel(key):
            entry = backing.pop(key, None)
            if entry is None:
                return None
            value, expires_at = entry
            if time.monotonic() > expires_at:
                return None
            return value

        fake_redis.setex.side_effect = _setex
        fake_redis.getdel.side_effect = _getdel

        store_oauth2_state(state="tok-expired", user_id="u-1", endpoint="ovh-eu")

        expired_key = "oauth2:state:tok-expired"
        if expired_key in backing:
            value, _ = backing[expired_key]
            backing[expired_key] = (value, time.monotonic() - 1)

        result = retrieve_oauth2_state("tok-expired")
        assert result is None

    def test_multiple_distinct_tokens_are_independent(self, fake_redis):
        """Consuming token A must not invalidate token B stored for the same user."""
        _in_memory_redis(fake_redis)

        store_oauth2_state(state="tok-alpha", user_id="u-1", endpoint="ovh-eu")
        store_oauth2_state(state="tok-beta", user_id="u-1", endpoint="ovh-eu")

        result_alpha = retrieve_oauth2_state("tok-alpha")
        result_beta = retrieve_oauth2_state("tok-beta")

        assert result_alpha is not None
        assert result_beta is not None
