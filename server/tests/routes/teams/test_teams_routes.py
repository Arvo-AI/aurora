"""Teams OAuth callback: state handling, tenant resolution and log hygiene."""

import importlib
import json
import logging
import sys
import types
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from flask import Flask

TENANT = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def routes(monkeypatch):
    """Import teams_routes with the Redis-backed state cache stubbed out."""
    cache = types.ModuleType("utils.auth.oauth2_state_cache")
    cache.stored = {}
    cache.store_oauth2_state = lambda state, user_id, provider: cache.stored.__setitem__(state, {"user_id": user_id})
    cache.retrieve_oauth2_state = lambda state: cache.stored.pop(state, None)
    monkeypatch.setitem(sys.modules, "utils.auth.oauth2_state_cache", cache)
    # Re-import each time so the module binds this test's stub, not a cached one
    monkeypatch.delitem(sys.modules, "routes.teams.teams_routes", raising=False)
    teams_routes = importlib.import_module("routes.teams.teams_routes")
    teams_routes.state_cache = cache
    yield teams_routes
    # Don't leave a module bound to this test's stub for later tests
    sys.modules.pop("routes.teams.teams_routes", None)


@pytest.fixture
def app(routes):
    flask_app = Flask(__name__)
    flask_app.register_blueprint(routes.teams_bp, url_prefix="/teams")
    return flask_app


@pytest.fixture
def oauth(routes, monkeypatch):
    """Fakes Microsoft + storage; returns a dict of what the callback did."""
    state = {"stored": [], "token": {}, "graph_tenant": None, "memory": 0, "registered": 0}
    monkeypatch.setenv("TEAMS_CLIENT_ID", "client-id")
    monkeypatch.setenv("TEAMS_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("TEAMS_TENANT_ID", TENANT)
    monkeypatch.setenv("NEXT_PUBLIC_BACKEND_URL", "https://aurora.example.com")
    monkeypatch.setattr(routes, "exchange_code_for_token", lambda code: state["token"])
    monkeypatch.setattr(routes, "_tenant_from_graph", lambda token: state["graph_tenant"])
    monkeypatch.setattr(routes, "_decode_id_token", _fake_verified_decode)
    monkeypatch.setattr(routes, "store_tokens_in_db", lambda uid, data, prov: state["stored"].append((uid, data, prov)))

    # Post-connect side effects (memory seed + channel sync) are covered elsewhere
    import routes.teams.teams_channel_metadata as metadata
    import services.memory.teams_memory as teams_memory

    monkeypatch.setattr(teams_memory, "seed_teams_memory", lambda user_id: state.__setitem__("memory", state["memory"] + 1))
    monkeypatch.setattr(metadata.auto_register_channels_task, "delay",
                        lambda user_id: state.__setitem__("registered", state["registered"] + 1))
    return state


def _id_token(**claims):
    """Claims as JSON; `oauth` swaps the signature check for a plain parse."""
    claims.setdefault("iss", f"https://login.microsoftonline.com/{claims.get('tid')}/v2.0")
    return json.dumps(claims)


def _fake_verified_decode(token):
    return json.loads(token)


def _location(resp):
    return resp.headers["Location"]


def _error_code(resp):
    return parse_qs(urlparse(_location(resp)).query).get("error", [None])[0]


def _begin(routes, user_id="uid-admin"):
    routes.state_cache.store_oauth2_state("good-state", user_id, "teams")


# --- state handling --------------------------------------------------------------------

def test_missing_code_or_state_fails(app, oauth):
    resp = app.test_client().get("/teams/callback?code=abc")
    assert _error_code(resp) == "no_code_or_state"
    assert oauth["stored"] == []


def test_unknown_state_is_rejected_and_nothing_is_stored(app, oauth):
    resp = app.test_client().get("/teams/callback?code=abc&state=forged-state")
    assert _error_code(resp) == "invalid_state"
    assert oauth["stored"] == []


def test_a_user_id_is_not_accepted_as_state(app, oauth):
    resp = app.test_client().get("/teams/callback?code=abc&state=uid-admin")
    assert _error_code(resp) == "invalid_state"


def test_state_is_single_use(app, routes, oauth):
    oauth["token"] = {"access_token": "at", "id_token": _id_token(tid=TENANT)}
    _begin(routes)
    client = app.test_client()
    assert "teams_auth=success" in _location(client.get("/teams/callback?code=abc&state=good-state"))
    assert _error_code(client.get("/teams/callback?code=abc&state=good-state")) == "invalid_state"


# --- tenant resolution -------------------------------------------------------------------

def test_tenant_comes_from_the_id_token(app, routes, oauth):
    oauth["token"] = {"access_token": "at", "refresh_token": "rt", "expires_in": 3600,
                      "id_token": _id_token(tid=TENANT.upper())}
    _begin(routes)
    resp = app.test_client().get("/teams/callback?code=abc&state=good-state")
    assert "teams_auth=success" in _location(resp)
    # Connecting seeds the policy memory and kicks off channel sync
    assert oauth["memory"] == 1 and oauth["registered"] == 1
    uid, data, provider = oauth["stored"][0]
    assert (uid, provider) == ("uid-admin", "teams")
    assert data["tenant_id"] == TENANT
    assert data["refresh_token"] == "rt"


def test_graph_is_the_fallback_when_there_is_no_id_token(app, routes, oauth):
    oauth["token"] = {"access_token": "at"}
    oauth["graph_tenant"] = "from-graph"
    _begin(routes)
    app.test_client().get("/teams/callback?code=abc&state=good-state")
    assert oauth["stored"][0][1]["tenant_id"] == "from-graph"


def test_unresolvable_tenant_is_a_failure_not_a_silent_success(app, routes, oauth):
    oauth["token"] = {"access_token": "at"}
    oauth["graph_tenant"] = None
    _begin(routes)
    resp = app.test_client().get("/teams/callback?code=abc&state=good-state")
    assert _error_code(resp) == "tenant_unresolved"
    assert oauth["stored"] == []


def test_missing_access_token_fails(app, routes, oauth):
    oauth["token"] = {"id_token": _id_token(tid=TENANT)}
    _begin(routes)
    assert _error_code(app.test_client().get("/teams/callback?code=abc&state=good-state")) == "no_token"


@pytest.mark.parametrize("token", [None, "", "not-a-jwt", _id_token(sub="x"), _id_token(tid="not-a-guid")])
def test_id_token_without_a_guid_tid_is_ignored(routes, oauth, token):
    assert routes._tenant_from_id_token(token) is None


def test_id_token_tid_is_normalised(routes, oauth):
    assert routes._tenant_from_id_token(_id_token(tid=TENANT.upper())) == TENANT


def test_id_token_whose_issuer_names_another_tenant_is_ignored(routes, oauth):
    other = "99999999-9999-9999-9999-999999999999"
    forged = _id_token(tid=TENANT, iss=f"https://login.microsoftonline.com/{other}/v2.0")
    assert routes._tenant_from_id_token(forged) is None


def test_id_token_that_fails_verification_falls_back_to_graph(app, routes, oauth, monkeypatch):
    def reject(token):
        raise jwt.InvalidSignatureError("bad signature")

    monkeypatch.setattr(routes, "_decode_id_token", reject)
    oauth["token"] = {"access_token": "at", "id_token": _id_token(tid=TENANT)}
    oauth["graph_tenant"] = "from-graph"
    _begin(routes)
    app.test_client().get("/teams/callback?code=abc&state=good-state")
    assert oauth["stored"][0][1]["tenant_id"] == "from-graph"


def test_id_token_is_verified_with_rs256_and_our_client_id(routes, monkeypatch):
    seen = {}

    class FakeJwks:
        def get_signing_key_from_jwt(self, token):
            return type("Key", (), {"key": "pubkey"})()

    def fake_decode(token, key, **kwargs):
        seen.update(kwargs, key=key)
        return {}

    monkeypatch.setenv("TEAMS_CLIENT_ID", "client-id")
    monkeypatch.setattr(routes, "_MS_JWKS_CLIENT", FakeJwks())
    monkeypatch.setattr(routes.jwt, "decode", fake_decode)
    routes._decode_id_token("tok")
    assert seen["algorithms"] == ["RS256"]
    assert seen["audience"] == "client-id"
    assert seen["key"] == "pubkey"


# --- Microsoft error param ------------------------------------------------------------------

def test_microsoft_error_param_cannot_forge_log_lines(app, oauth, caplog):
    forged = "access_denied%0AINFO%20forged%20line%0D%0Aadmin%20logged%20in"
    with caplog.at_level(logging.WARNING, logger="routes.teams.teams_routes"):
        resp = app.test_client().get(f"/teams/callback?error={forged}&error_description=bad%0Athing")
    assert resp.status_code == 302
    records = [r.getMessage() for r in caplog.records if "Microsoft" in r.getMessage()]
    assert records
    assert all("\n" not in m and "\r" not in m for m in records)


def test_microsoft_error_param_is_length_capped_in_logs(app, oauth, caplog):
    with caplog.at_level(logging.WARNING, logger="routes.teams.teams_routes"):
        app.test_client().get("/teams/callback?error=" + "a" * 5000)
    message = next(r.getMessage() for r in caplog.records if "Microsoft" in r.getMessage())
    assert len(message) < 400


def test_microsoft_error_redirects_to_setup_with_the_code(app, oauth):
    resp = app.test_client().get("/teams/callback?error=access_denied")
    assert _error_code(resp) == "access_denied"
    assert "/teams/setup" in _location(resp)
    # The raw Microsoft text never rides along in the redirect
    assert "error_description" not in _location(resp)


def test_unknown_microsoft_error_becomes_a_fixed_code(app, oauth):
    resp = app.test_client().get("/teams/callback?error=https://evil.example/phish")
    assert _error_code(resp) == "oauth_error"


def test_single_tenant_description_maps_to_its_own_code(app, oauth):
    resp = app.test_client().get(
        "/teams/callback?error=invalid_request&error_description=not%20configured%20as%20a%20multi-tenant"
    )
    assert _error_code(resp) == "single_tenant_common"
    assert "error_description" not in _location(resp)
