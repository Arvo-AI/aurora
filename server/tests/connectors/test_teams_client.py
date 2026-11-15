"""Graph client: path safety, retry parsing and the locked token refresh."""

import time
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from connectors.teams_connector import client as tc


# --- path / segment safety -----------------------------------------------------

@pytest.mark.parametrize("path", [
    "me",                              # no leading slash
    "https://evil.example.com/x",      # absolute URL
    "//evil.example.com/x",            # protocol-relative
    "/teams/../me",                    # traversal
    "/teams/%2e%2e/me",                # encoded traversal
    "/me?$select=id",                  # query smuggled into the path
])
def test_graph_paths_reject_traversal_and_absolute_urls(path):
    with pytest.raises(tc.TeamsAPIError):
        tc.TeamsClient._graph_url(path)


def test_graph_url_stays_on_graph_host():
    assert tc.TeamsClient._graph_url("/me/joinedTeams") == "https://graph.microsoft.com/v1.0/me/joinedTeams"


@pytest.mark.parametrize("value", ["", "a/b", "a;b", "a=b", "a b", "x" * 600])
def test_safe_segment_rejects_unsafe_ids(value):
    with pytest.raises(tc.TeamsAPIError):
        tc.TeamsClient._safe_segment(value, label="channel_id")


def test_safe_segment_accepts_channel_ids():
    assert tc.TeamsClient._safe_segment("19:abc@thread.tacv2", label="channel_id") == "19:abc@thread.tacv2"


# --- 429 handling ---------------------------------------------------------------

def _response(status, headers=None, body=None):
    resp = MagicMock()
    resp.status_code = status
    resp.ok = 200 <= status < 300
    resp.headers = headers or {}
    resp.content = b"x"
    resp.json.return_value = body if body is not None else {}
    return resp


def test_retry_after_http_date_falls_back_to_backoff(monkeypatch):
    sleeps = []
    monkeypatch.setattr(tc.time, "sleep", lambda s: sleeps.append(s))
    responses = iter([
        _response(429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
        _response(200, body={"ok": True}),
    ])
    monkeypatch.setattr(tc.requests, "request", lambda *a, **k: next(responses))
    assert tc.TeamsClient("tok")._request("GET", "/me") == {"ok": True}
    assert sleeps == [2]


def test_retry_after_seconds_is_honoured_and_capped(monkeypatch):
    sleeps = []
    monkeypatch.setattr(tc.time, "sleep", lambda s: sleeps.append(s))
    responses = iter([_response(429, {"Retry-After": "120"}), _response(200, body={})])
    monkeypatch.setattr(tc.requests, "request", lambda *a, **k: next(responses))
    tc.TeamsClient("tok")._request("GET", "/me")
    assert sleeps == [30]


def test_error_status_raises_with_code(monkeypatch):
    monkeypatch.setattr(tc.requests, "request", lambda *a, **k: _response(403))
    with pytest.raises(tc.TeamsAPIError) as exc:
        tc.TeamsClient("tok")._request("GET", "/me")
    assert exc.value.status_code == 403


# --- expiry ---------------------------------------------------------------------

def test_expiry_prefers_the_refresh_timestamp():
    creds = {"expires_in": 3600, "connected_at": int(time.time()) - 86400, "token_issued_at": int(time.time())}
    assert tc._access_token_expired(creds) is False


def test_expiry_uses_connected_at_for_a_never_refreshed_token():
    creds = {"expires_in": 3600, "connected_at": int(time.time()) - 7200}
    assert tc._access_token_expired(creds) is True


def test_expiry_unknown_means_not_expired():
    assert tc._access_token_expired({"access_token": "t"}) is False


# --- get_teams_client_for_user -----------------------------------------------------

@pytest.fixture
def refresh_env(monkeypatch):
    """Patches creds storage, the refresh call and the lock; returns the call log."""
    import connectors.teams_connector.oauth as oauth
    import utils.auth.stateless_auth as sa
    import utils.auth.token_management as tm

    log = {"refresh": [], "stored": [], "creds": [], "locked": True}

    def get_creds(user_id, provider):
        current = log["creds"][0] if len(log["creds"]) == 1 else log["creds"].pop(0)
        return dict(current) if current else current

    def refresh(refresh_token):
        log["refresh"].append(refresh_token)
        return {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}

    @contextmanager
    def lock(user_id):
        yield log["locked"]

    monkeypatch.setattr(sa, "get_credentials_from_db", get_creds)
    monkeypatch.setattr(tm, "store_tokens_in_db", lambda uid, data, prov: log["stored"].append((uid, data, prov)))
    monkeypatch.setattr(oauth, "refresh_access_token", refresh)
    monkeypatch.setattr(tc, "_refresh_lock", lock)
    return log


def _expired(**over):
    creds = {"access_token": "old", "refresh_token": "r1", "expires_in": 3600,
             "connected_at": int(time.time()) - 7200, "tenant_id": "tenant-1"}
    creds.update(over)
    return creds


def test_valid_token_is_returned_without_refresh_or_write(refresh_env):
    refresh_env["creds"] = [_expired(connected_at=int(time.time()))]
    client = tc.get_teams_client_for_user("u1")
    assert client.access_token == "old"
    assert refresh_env["refresh"] == [] and refresh_env["stored"] == []


def test_expired_token_is_refreshed_once_and_persisted(refresh_env):
    refresh_env["creds"] = [_expired()]
    client = tc.get_teams_client_for_user("u1")
    assert client.access_token == "new-access"
    assert refresh_env["refresh"] == ["r1"]
    _uid, stored, provider = refresh_env["stored"][0]
    assert provider == "teams"
    assert stored["refresh_token"] == "new-refresh"
    # The connect time survives; only the token issue time moves
    assert stored["connected_at"] < stored["token_issued_at"]
    assert stored["tenant_id"] == "tenant-1"


def test_waiter_reuses_the_token_another_worker_refreshed(refresh_env):
    fresh = _expired(access_token="rotated", connected_at=int(time.time()))
    # First read sees an expired token, the re-read under the lock sees the fresh one
    refresh_env["creds"] = [_expired(), fresh]
    client = tc.get_teams_client_for_user("u1")
    assert client.access_token == "rotated"
    assert refresh_env["refresh"] == []


def test_busy_lock_does_not_rotate_the_token_twice(refresh_env):
    refresh_env["creds"] = [_expired()]
    refresh_env["locked"] = False
    assert tc.get_teams_client_for_user("u1") is None
    assert refresh_env["refresh"] == []


def test_expired_token_without_refresh_token_is_unusable(refresh_env):
    refresh_env["creds"] = [_expired(refresh_token=None)]
    assert tc.get_teams_client_for_user("u1") is None


def test_missing_credentials_return_none(refresh_env):
    refresh_env["creds"] = [None]
    assert tc.get_teams_client_for_user("u1") is None


# --- has_teams_credentials ---------------------------------------------------------

def test_has_credentials_never_refreshes_or_writes(refresh_env):
    refresh_env["creds"] = [_expired()]
    assert tc.has_teams_credentials("u1") is True
    assert refresh_env["refresh"] == [] and refresh_env["stored"] == []


def test_has_credentials_false_without_tokens(refresh_env):
    refresh_env["creds"] = [{"tenant_id": "t"}]
    assert tc.has_teams_credentials("u1") is False
