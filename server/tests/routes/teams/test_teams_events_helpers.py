"""Bot Framework JWT checks, mention detection, tenant lookup and dedupe helpers."""

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from flask import Flask

from routes.teams import teams_events_helpers as h

SERVICE_URL = "https://smba.trafficmanager.net/teams/"


# --- verify_teams_request_claims --------------------------------------------------

@pytest.fixture
def jwt_env(monkeypatch):
    monkeypatch.setenv("TEAMS_APP_ID", "app-id")
    seen = {}
    jwks = MagicMock()
    jwks.get_signing_key_from_jwt.return_value = MagicMock(key="signing-key")
    monkeypatch.setattr(h, "_botframework_jwks_client", lambda: jwks)

    def fake_decode(token, key, **kwargs):
        seen.update(token=token, key=key, **kwargs)
        return {"aud": "app-id", "serviceurl": SERVICE_URL}

    monkeypatch.setattr(h.jwt, "decode", fake_decode)
    return seen


def _call_with_header(value):
    app = Flask(__name__)
    headers = {"Authorization": value} if value is not None else {}
    with app.test_request_context("/teams/messages", method="POST", headers=headers):
        return h.verify_teams_request_claims()


def test_valid_token_returns_its_claims(jwt_env):
    claims = _call_with_header("Bearer tok")
    assert claims["serviceurl"] == SERVICE_URL


def test_issuer_audience_and_algorithm_are_enforced(jwt_env):
    _call_with_header("Bearer tok")
    assert jwt_env["issuer"] == "https://api.botframework.com"
    assert jwt_env["audience"] == "app-id"
    assert jwt_env["algorithms"] == ["RS256"]
    assert jwt_env["key"] == "signing-key"


@pytest.mark.parametrize("header", [None, "", "Basic abc", "tok"])
def test_missing_or_non_bearer_header_is_rejected(jwt_env, header):
    assert _call_with_header(header) is None


def test_missing_app_id_fails_closed(jwt_env, monkeypatch):
    monkeypatch.delenv("TEAMS_APP_ID", raising=False)
    monkeypatch.delenv("TEAMS_CLIENT_ID", raising=False)
    assert _call_with_header("Bearer tok") is None


def test_invalid_signature_is_rejected(jwt_env, monkeypatch):
    def boom(*a, **k):
        raise h.jwt.InvalidSignatureError("bad signature")

    monkeypatch.setattr(h.jwt, "decode", boom)
    assert _call_with_header("Bearer tok") is None


def test_jwks_lookup_failure_is_rejected(jwt_env, monkeypatch):
    def boom():
        raise RuntimeError("jwks down")

    monkeypatch.setattr(h, "_botframework_jwks_client", boom)
    assert _call_with_header("Bearer tok") is None


def test_boolean_wrapper_matches_claims(jwt_env):
    app = Flask(__name__)
    with app.test_request_context("/", headers={"Authorization": "Bearer tok"}):
        assert h.verify_teams_request() is True
    with app.test_request_context("/"):
        assert h.verify_teams_request() is False


def test_jwks_client_is_cached_between_requests(monkeypatch):
    monkeypatch.setattr(h, "_OPENID_CONFIG", None)
    monkeypatch.setattr(h, "_BOTFRAMEWORK_JWKS_CLIENT", None)
    fetches = []

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"jwks_uri": "https://login.botframework.com/v1/.well-known/keys"}

    monkeypatch.setattr(h.requests, "get", lambda *a, **k: fetches.append(1) or FakeResp())
    monkeypatch.setattr(h.jwt, "PyJWKClient", lambda uri: object())
    first = h._botframework_jwks_client()
    assert h._botframework_jwks_client() is first
    assert len(fetches) == 1


# --- serviceUrl trust --------------------------------------------------------------

def test_trusted_service_url_matching_claim():
    assert h.is_trusted_service_url(SERVICE_URL, {"serviceurl": SERVICE_URL}) is True


def test_trailing_slash_and_case_do_not_matter():
    assert h.is_trusted_service_url(SERVICE_URL, {"serviceurl": "HTTPS://smba.trafficmanager.net/teams"}) is True


def test_claim_mismatch_is_untrusted():
    assert h.is_trusted_service_url(SERVICE_URL, {"serviceurl": "https://smba.trafficmanager.net/emea/"}) is False


def test_non_microsoft_host_is_untrusted_even_if_the_claim_agrees():
    assert h.is_trusted_service_url("https://evil.example.com/", {"serviceurl": "https://evil.example.com/"}) is False


def test_missing_claim_still_requires_an_allowed_host():
    assert h.is_trusted_service_url(SERVICE_URL, {}) is True
    assert h.is_trusted_service_url("https://evil.example.com/", {}) is False


# --- is_bot_mentioned --------------------------------------------------------------

def _mention(mentioned_id):
    return {"recipient": {"id": "28:app-id"}, "entities": [{"type": "mention", "mentioned": {"id": mentioned_id}}]}


def test_mentioning_the_bot_counts(monkeypatch):
    monkeypatch.setenv("TEAMS_APP_ID", "app-id")
    assert h.is_bot_mentioned(_mention("28:app-id")) is True


def test_mentioning_someone_else_does_not_count(monkeypatch):
    monkeypatch.setenv("TEAMS_APP_ID", "app-id")
    assert h.is_bot_mentioned(_mention("29:some-user")) is False


def test_non_mention_entities_are_ignored(monkeypatch):
    monkeypatch.setenv("TEAMS_APP_ID", "app-id")
    activity = {"recipient": {"id": "28:app-id"}, "entities": [{"type": "clientInfo", "mentioned": {"id": "28:app-id"}}]}
    assert h.is_bot_mentioned(activity) is False


def test_no_entities_means_not_mentioned(monkeypatch):
    monkeypatch.setenv("TEAMS_APP_ID", "app-id")
    assert h.is_bot_mentioned({"recipient": {"id": "28:app-id"}}) is False


def test_without_an_app_id_nothing_counts_as_a_mention(monkeypatch):
    monkeypatch.delenv("TEAMS_APP_ID", raising=False)
    monkeypatch.delenv("TEAMS_CLIENT_ID", raising=False)
    assert h.is_bot_mentioned(_mention("28:app-id")) is False


# --- tenant -> user ------------------------------------------------------------------

class _Cursor:
    """user_tokens rows are only visible under the org that owns them, like FORCE RLS."""

    def __init__(self, row, owner_org="org-a", orgs=("org-z", "org-a"), rows_by_org=None):
        self.row = row
        self.owner_org = owner_org
        self.orgs = orgs
        self.rows_by_org = rows_by_org
        self.current_org = None
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if sql.startswith("SET myapp.current_org_id"):
            self.current_org = params[0]

    def fetchall(self):
        return [(o,) for o in self.orgs]

    def fetchone(self):
        if self.rows_by_org is not None:
            return self.rows_by_org.get(self.current_org)
        if self.current_org != self.owner_org or self.row is None:
            return None
        # The lookup reads (user_id, timestamp); a bare user id is the newest in its org
        return self.row if len(self.row) > 1 else (self.row[0], 1)


def _patch_pool(monkeypatch, row=None, error=None, rows_by_org=None):
    cursor = _Cursor(row, rows_by_org=rows_by_org)

    @contextmanager
    def conn_ctx():
        if error:
            raise error
        conn = MagicMock()
        conn.cursor.return_value = cursor
        yield conn

    pool = MagicMock()
    pool.get_admin_connection = conn_ctx
    monkeypatch.setattr(h, "db_pool", pool)
    return cursor


def test_tenant_resolves_to_the_connecting_user(monkeypatch):
    cursor = _patch_pool(monkeypatch, row=("uid-admin",))
    assert h.get_user_id_from_teams_tenant("tenant-1") == "uid-admin"
    lookups = [(s, p) for s, p in cursor.executed if "user_tokens" in s]
    sql, params = lookups[-1]
    assert "provider = 'teams'" in sql and "is_active = TRUE" in sql
    assert "timestamp" in sql
    assert params == ("tenant-1",)


def test_newest_token_wins_when_an_older_org_is_listed_first(monkeypatch):
    _patch_pool(
        monkeypatch,
        rows_by_org={"org-z": ("uid-old", 1), "org-a": ("uid-new", 2)},
    )
    assert h.get_user_id_from_teams_tenant("tenant-1") == "uid-new"


def test_tenant_lookup_sets_the_owning_org_before_reading_user_tokens(monkeypatch):
    cursor = _patch_pool(monkeypatch, row=("uid-admin",))
    assert h.get_user_id_from_teams_tenant("tenant-1") == "uid-admin"
    assert cursor.current_org == "org-a"


def test_unknown_tenant_resolves_to_none(monkeypatch):
    _patch_pool(monkeypatch, row=None)
    assert h.get_user_id_from_teams_tenant("tenant-x") is None


def test_empty_tenant_never_queries(monkeypatch):
    cursor = _patch_pool(monkeypatch, row=("uid-admin",))
    assert h.get_user_id_from_teams_tenant("") is None
    assert cursor.executed == []


def test_db_failure_resolves_to_none(monkeypatch):
    _patch_pool(monkeypatch, error=RuntimeError("db down"))
    assert h.get_user_id_from_teams_tenant("tenant-1") is None


# --- ids ---------------------------------------------------------------------------------

def test_strip_thread_suffix():
    assert h.strip_thread_suffix("19:c@thread.tacv2;messageid=5") == "19:c@thread.tacv2"
    assert h.strip_thread_suffix("19:c@thread.tacv2") == "19:c@thread.tacv2"
    assert h.strip_thread_suffix(None) is None
    assert h.strip_thread_suffix("") is None


def test_ids_fall_back_to_the_conversation_id_and_thread_team():
    activity = {
        "conversation": {"id": "19:c@thread.tacv2;messageid=5"},
        "channelData": {"team": {"id": "19:team@thread.tacv2"}},
    }
    assert h.extract_team_and_channel_ids(activity) == ("19:team@thread.tacv2", "19:c@thread.tacv2")


def test_dm_has_no_team():
    activity = {"conversation": {"id": "a:1dm"}, "channelData": {}}
    assert h.extract_team_and_channel_ids(activity) == (None, "a:1dm")


# --- dedupe -------------------------------------------------------------------------------

def test_first_sighting_wins_and_repeats_are_dropped(monkeypatch):
    redis = MagicMock()
    redis.set.side_effect = [True, None]
    monkeypatch.setattr(h, "get_redis_client", lambda: redis)
    assert h.claim_activity_once("act-1") is True
    assert h.claim_activity_once("act-1") is False
    _args, kwargs = redis.set.call_args
    assert kwargs["nx"] is True and kwargs["ex"] == h.ACTIVITY_DEDUPE_TTL_SECONDS


def test_activity_without_an_id_is_processed(monkeypatch):
    monkeypatch.setattr(h, "get_redis_client", lambda: pytest.fail("should not touch redis"))
    assert h.claim_activity_once(None) is True


def test_redis_down_processes_rather_than_drops(monkeypatch):
    monkeypatch.setattr(h, "get_redis_client", lambda: None)
    assert h.claim_activity_once("act-1") is True


def test_redis_error_processes_rather_than_drops(monkeypatch):
    redis = MagicMock()
    redis.set.side_effect = RuntimeError("redis down")
    monkeypatch.setattr(h, "get_redis_client", lambda: redis)
    assert h.claim_activity_once("act-1") is True


# --- send_message_to_aurora -----------------------------------------------------------------

@pytest.fixture
def aurora_calls(monkeypatch):
    import sys
    import types

    calls = {"session": [], "run": []}
    stub = types.ModuleType("chat.background.task")
    stub.create_background_chat_session = lambda **kw: calls["session"].append(kw) or "new-session"
    run = MagicMock()
    run.delay = lambda **kw: calls["run"].append(kw)
    stub.run_background_chat = run
    monkeypatch.setitem(sys.modules, "chat.background.task", stub)
    monkeypatch.setattr(h, "_post_teams_thinking_placeholder", lambda **kw: "thinking-1")
    return calls


def test_new_conversation_creates_a_session_and_runs_in_ask_mode(aurora_calls):
    h.send_message_to_aurora(
        "uid-admin", "hello", team_id="team-1", channel_id="19:c@thread.tacv2",
        reply_to_id="act-1", service_url=SERVICE_URL, conversation_id="19:c@thread.tacv2;messageid=5",
    )
    assert len(aurora_calls["session"]) == 1
    run = aurora_calls["run"][0]
    assert run["mode"] == "ask" and run["session_id"] == "new-session"
    # Session row and task get the same metadata, placeholder id included
    assert aurora_calls["session"][0]["trigger_metadata"] == run["trigger_metadata"]
    assert run["trigger_metadata"]["thinking_activity_id"] == "thinking-1"


def test_supplied_session_keeps_the_thinking_placeholder_id(aurora_calls):
    h.send_message_to_aurora(
        "uid-admin", "hello", team_id="team-1", channel_id="19:c@thread.tacv2",
        service_url=SERVICE_URL, conversation_id="19:c@thread.tacv2", session_id="existing",
    )
    assert aurora_calls["session"] == []
    run = aurora_calls["run"][0]
    assert run["session_id"] == "existing"
    assert run["trigger_metadata"]["thinking_activity_id"] == "thinking-1"


def test_dm_dispatch_carries_no_team(aurora_calls):
    h.send_message_to_aurora(
        "uid-admin", "hello", team_id=None, channel_id="a:1dm",
        service_url=SERVICE_URL, conversation_id="a:1dm",
    )
    assert aurora_calls["run"][0]["trigger_metadata"]["team_id"] is None


def test_placeholder_uses_a_short_timeout(monkeypatch):
    import connectors.teams_connector.bot_client as bc

    seen = {}

    def fake_reply(**kwargs):
        seen.update(kwargs)
        return {"id": "thinking-9"}

    monkeypatch.setattr(bc, "send_reply_in_conversation", fake_reply)
    result = h._post_teams_thinking_placeholder(
        service_url=SERVICE_URL, conversation_id="a:1dm", reply_to_id="act-1",
    )
    assert result == "thinking-9"
    assert seen["timeout"] <= 10
