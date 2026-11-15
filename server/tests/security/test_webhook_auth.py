"""Alert webhooks must be authenticated by the connection's webhook secret.

``/<vendor>/webhook/<user_id>`` routes skip session auth (the vendor has no
Aurora login). The ``user_id`` is not a credential: any org member reads every
member's id from ``GET /api/orgs/current``. Before this fix, nine of these
routes accepted any POST carrying a valid ``user_id`` and started an RCA
investigation as that user, using that user's connected credentials.

These tests drive the real blueprints through Flask's test client with the
credential store, the advisory lock and Celery dispatch replaced by in-memory
fakes.
"""

import ast
import base64
import hmac
import importlib
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

flask = pytest.importorskip("flask")

from utils.web import webhook_auth  # noqa: E402

USER = "11111111-2222-3333-4444-555555555555"
SECRET = "s3cr3t-" + "x" * 36
HEADER = webhook_auth.WEBHOOK_HEADER_NAME

# Enough for every vendor's "is it connected" check (Splunk needs a URL + token).
CONNECTED = {"api_token": "vendor-api-token", "base_url": "https://vendor.example.com:8089", "webhook_secret": SECRET}
LEGACY = {"api_token": "vendor-api-token", "base_url": "https://vendor.example.com:8089"}


# --------------------------------------------------------------------------- #
# Per-vendor wiring: blueprint, URL, payload, and where the work is enqueued.
# --------------------------------------------------------------------------- #

def _capture_delay(monkeypatch, task, sink):
    # The Celery stub installed by whichever module loaded first may or may not
    # give the task a .delay attribute; set it either way.
    monkeypatch.setattr(task, "delay", lambda *a, **k: sink.append((a, k)), raising=False)


def _patch_creds(monkeypatch, module, attr, creds):
    monkeypatch.setattr(module, attr, lambda user_id, *a, **k: dict(creds) if user_id == USER and creds else {})


def _setup_grafana(monkeypatch, mod, creds, sink):
    monkeypatch.setattr(mod, "validate_user_exists", lambda user_id: user_id == USER)
    monkeypatch.setattr(mod, "_has_grafana_row", lambda user_id: (creds is not None, creds is not None))
    _patch_creds(monkeypatch, mod, "get_token_data", creds)
    _capture_delay(monkeypatch, mod.process_grafana_alert, sink)


def _setup_datadog(monkeypatch, mod, creds, sink):
    _patch_creds(monkeypatch, mod, "get_token_data", creds)
    _capture_delay(monkeypatch, mod.process_datadog_event, sink)


def _setup_pagerduty(monkeypatch, mod, creds, sink):
    _patch_creds(monkeypatch, mod, "get_token_data", creds)
    from routes.pagerduty import tasks
    _capture_delay(monkeypatch, tasks.process_pagerduty_event, sink)


def _setup_newrelic(monkeypatch, mod, creds, sink):
    _patch_creds(monkeypatch, mod, "_get_stored_newrelic_credentials", creds)
    from routes.newrelic import tasks
    _capture_delay(monkeypatch, tasks.process_newrelic_event, sink)


def _setup_opsgenie(monkeypatch, mod, creds, sink):
    _patch_creds(monkeypatch, mod, "get_token_data", creds)
    _capture_delay(monkeypatch, mod.process_opsgenie_event, sink)


def _setup_jira(monkeypatch, mod, creds, sink):
    _patch_creds(monkeypatch, mod, "get_token_data", creds)
    monkeypatch.setattr(mod, "_read_rca_settings", lambda user_id: ("auto", []))
    monkeypatch.setattr(mod, "_enqueue_webhook", lambda decision, payload, user_id: sink.append(((payload, user_id), {})))


def _setup_dynatrace(monkeypatch, mod, creds, sink):
    _patch_creds(monkeypatch, mod, "get_token_data", creds)
    _capture_delay(monkeypatch, mod.process_dynatrace_problem, sink)


def _setup_netdata(monkeypatch, mod, creds, sink):
    _patch_creds(monkeypatch, mod, "get_token_data", creds)
    _capture_delay(monkeypatch, mod.process_netdata_alert, sink)


def _setup_splunk(monkeypatch, mod, creds, sink):
    _patch_creds(monkeypatch, mod, "get_token_data", creds)
    _capture_delay(monkeypatch, mod.process_splunk_alert, sink)


VENDORS = {
    "grafana": SimpleNamespace(
        module="routes.grafana.grafana_routes", bp="grafana_bp", path="/grafana/alerts/webhook/{}",
        payload={"title": "FORGED", "status": "firing", "alerts": [{"status": "firing", "labels": {"severity": "critical"}}]},
        setup=_setup_grafana,
    ),
    "datadog": SimpleNamespace(
        module="routes.datadog.datadog_routes", bp="datadog_bp", path="/datadog/webhook/{}",
        payload={"event_type": "monitor_alert", "title": "FORGED"}, setup=_setup_datadog,
    ),
    "pagerduty": SimpleNamespace(
        module="routes.pagerduty.pagerduty_routes", bp="pagerduty_bp", path="/pagerduty/webhook/{}",
        payload={"event": {"id": "e1", "event_type": "incident.triggered", "resource_type": "incident", "data": {}}},
        setup=_setup_pagerduty,
    ),
    "newrelic": SimpleNamespace(
        module="routes.newrelic.newrelic_routes", bp="newrelic_bp", path="/newrelic/webhook/{}",
        payload={"issueId": "i1", "title": "FORGED"}, setup=_setup_newrelic,
    ),
    "opsgenie": SimpleNamespace(
        module="routes.opsgenie.opsgenie_routes", bp="opsgenie_bp", path="/opsgenie/webhook/{}",
        payload={"action": "Create", "alert": {"message": "FORGED"}}, setup=_setup_opsgenie,
    ),
    "jira": SimpleNamespace(
        module="routes.jira.jira_routes", bp="jira_bp", path="/jira/webhook/{}",
        payload={"webhookEvent": "jira:issue_created",
                 "issue": {"key": "OPS-1", "fields": {"project": {"key": "OPS"}, "issuetype": {"name": "Bug"}}}},
        setup=_setup_jira,
    ),
    "dynatrace": SimpleNamespace(
        module="routes.dynatrace.dynatrace_routes", bp="dynatrace_bp", path="/dynatrace/webhook/{}",
        payload={"ProblemTitle": "FORGED", "State": "OPEN"}, setup=_setup_dynatrace,
    ),
    "netdata": SimpleNamespace(
        module="routes.netdata.netdata_routes", bp="netdata_bp", path="/netdata/alerts/webhook/{}",
        payload={"alarm": "FORGED", "status": "critical"}, setup=_setup_netdata,
    ),
    "splunk": SimpleNamespace(
        module="routes.splunk.splunk_routes", bp="splunk_bp", path="/splunk/alerts/webhook/{}",
        payload={"search_name": "FORGED"}, setup=_setup_splunk,
    ),
}


def _load(module_name):
    # Earlier modules in the full suite reload Flask's globals, which leaves
    # already-imported modules holding ``request``/``jsonify`` bound to stale
    # context variables. Re-import the helpers, then the route module that
    # imports from them, so all use the Flask the test client drives.
    importlib.reload(sys.modules["utils.web.webhook_auth"])
    if "routes.webhook_secret_routes" in sys.modules:
        importlib.reload(sys.modules["routes.webhook_secret_routes"])
    if module_name in sys.modules:
        return importlib.reload(sys.modules[module_name])
    return importlib.import_module(module_name)


@pytest.fixture(params=sorted(VENDORS))
def vendor(request):
    return request.param


@pytest.fixture
def fake_store(monkeypatch):
    """In-memory credential store behind the helper's strict read, lock and write.

    ``_load`` reloads the helper module, which drops these patches: call
    ``store.install()`` again after loading a route module.
    """
    store = SimpleNamespace(creds={}, writes=[], fail_read=False)

    def read(user_id, provider):
        if store.fail_read:
            raise RuntimeError("vault unavailable")
        return dict(store.creds)

    def write(user_id, provider, payload):
        store.writes.append(payload)
        store.creds = dict(payload)

    def install():
        monkeypatch.setattr(webhook_auth, "_read_creds_strict", read)
        monkeypatch.setattr(webhook_auth, "_store", write)
        monkeypatch.setattr(webhook_auth, "webhook_secret_lock", lambda *a, **k: nullcontext())

    install()
    store.install = install
    return store


def _client(monkeypatch, vendor_name, creds):
    spec = VENDORS[vendor_name]
    mod = _load(spec.module)
    app = flask.Flask(__name__)
    app.register_blueprint(getattr(mod, spec.bp), url_prefix="/" + vendor_name)
    sink = []
    spec.setup(monkeypatch, mod, creds, sink)
    return app.test_client(), spec, sink, mod


def _post(client, spec, headers=None, query="", user=USER):
    return client.post(spec.path.format(user) + query, json=spec.payload, headers=headers or {})


def _basic(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


# --------------------------------------------------------------------------- #
# Every previously unprotected webhook
# --------------------------------------------------------------------------- #

def test_forged_request_without_secret_is_rejected(monkeypatch, vendor):
    client, spec, sink, _ = _client(monkeypatch, vendor, CONNECTED)
    resp = _post(client, spec)
    assert resp.status_code == 401
    assert sink == []


def test_wrong_secret_is_rejected(monkeypatch, vendor):
    client, spec, sink, _ = _client(monkeypatch, vendor, CONNECTED)
    assert _post(client, spec, headers={HEADER: SECRET[:-1] + "y"}).status_code == 401
    assert _post(client, spec, query="?secret=wrong").status_code == 401
    assert _post(client, spec, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert _post(client, spec, headers={"Authorization": _basic("aurora", "wrong")}).status_code == 401
    assert sink == []


def test_legacy_connection_without_stored_secret_is_rejected(monkeypatch, vendor):
    client, spec, sink, _ = _client(monkeypatch, vendor, LEGACY)
    resp = _post(client, spec, headers={HEADER: SECRET})
    assert resp.status_code == 401
    assert "copy the new webhook URL" in resp.get_json()["error"]
    assert sink == []


def test_another_users_secret_does_not_authenticate(monkeypatch, vendor):
    """The secret authenticates one connection; it is not a global key."""
    client, spec, sink, _ = _client(monkeypatch, vendor, CONNECTED)
    resp = _post(client, spec, headers={HEADER: SECRET}, user="99999999-0000-0000-0000-000000000000")
    assert resp.status_code in (401, 404)
    assert sink == []


@pytest.mark.parametrize("location", ["header", "query", "bearer", "basic"])
def test_correct_secret_enqueues_for_the_url_user(monkeypatch, vendor, location):
    client, spec, sink, _ = _client(monkeypatch, vendor, CONNECTED)
    headers, query = {}, ""
    if location == "header":
        headers = {HEADER: SECRET}
    elif location == "query":
        query = "?secret=" + SECRET
    elif location == "bearer":
        headers = {"Authorization": "Bearer " + SECRET}
    else:
        headers = {"Authorization": _basic("aurora", SECRET)}

    resp = _post(client, spec, headers=headers, query=query)

    assert resp.status_code in (200, 202), resp.get_data(as_text=True)
    assert len(sink) == 1
    args, kwargs = sink[0]
    assert USER in list(args) + list(kwargs.values())


@pytest.mark.parametrize("vendor_auth", [_basic("svc", "vendor-password"), "Bearer vendor-token"])
def test_vendor_authorization_header_does_not_hide_the_url_secret(monkeypatch, vendor, vendor_auth):
    """A tool that sends its own Authorization header must still be accepted via ?secret=."""
    client, spec, sink, _ = _client(monkeypatch, vendor, CONNECTED)
    resp = _post(client, spec, headers={"Authorization": vendor_auth}, query="?secret=" + SECRET)
    assert resp.status_code in (200, 202), resp.get_data(as_text=True)
    assert len(sink) == 1


def test_secret_is_not_stored_with_the_alert(monkeypatch, vendor):
    """Handlers persist request headers as alert metadata; the secret must be redacted."""
    client, spec, sink, _ = _client(monkeypatch, vendor, CONNECTED)
    _post(client, spec, headers={HEADER: SECRET})
    assert len(sink) == 1
    assert SECRET not in repr(sink)


# --------------------------------------------------------------------------- #
# Rotation: POST /<vendor>/webhook-secret/rotate on all nine blueprints
# --------------------------------------------------------------------------- #

def _rotate(vendor_name, store):
    spec = VENDORS[vendor_name]
    mod = _load(spec.module)
    store.install()
    app = flask.Flask(__name__)
    app.register_blueprint(getattr(mod, spec.bp), url_prefix="/" + vendor_name)
    view = app.view_functions[f"{getattr(mod, spec.bp).name}.rotate_webhook_secret_route"]
    with app.test_request_context(method="POST"):
        result = view.__wrapped__(USER)  # skip the RBAC decorator; its permission is checked below
    resp, status = result if isinstance(result, tuple) else (result, 200)
    return status, resp.get_json()


def test_rotate_replaces_the_secret(vendor, fake_store):
    fake_store.creds = dict(CONNECTED)
    status, body = _rotate(vendor, fake_store)
    assert status == 200
    assert body["webhookSecret"] != SECRET
    assert fake_store.creds["webhook_secret"] == body["webhookSecret"]
    assert fake_store.creds["api_token"] == "vendor-api-token"  # the rest of the credentials is kept


def test_rotate_when_not_connected_is_404(vendor, fake_store):
    status, _ = _rotate(vendor, fake_store)
    assert status == 404
    assert fake_store.writes == []


def test_rotate_when_the_store_is_unreadable_is_500(vendor, fake_store):
    fake_store.creds = dict(CONNECTED)
    fake_store.fail_read = True
    status, _ = _rotate(vendor, fake_store)
    assert status == 500
    assert fake_store.writes == []


# --------------------------------------------------------------------------- #
# Reconnecting must keep the secret, and a store read error must not replace it
# --------------------------------------------------------------------------- #

def _netdata_connect(monkeypatch, read):
    mod = _load("routes.netdata.netdata_routes")
    import utils.auth.token_management as tm
    monkeypatch.setattr(tm, "get_token_data", read)
    writes = []
    monkeypatch.setattr(mod, "store_tokens_in_db", lambda user_id, payload, provider, **k: writes.append(payload))
    app = flask.Flask(__name__)
    with app.test_request_context(method="POST", json={"apiToken": "new-token"}):
        result = mod.connect.__wrapped__(USER)
    resp, status = result if isinstance(result, tuple) else (result, 200)
    return status, writes


def test_reconnect_keeps_the_existing_secret(monkeypatch):
    status, writes = _netdata_connect(monkeypatch, lambda *a, **k: {"api_token": "old", "webhook_secret": SECRET})
    assert status == 200
    assert writes[0]["webhook_secret"] == SECRET
    assert writes[0]["api_token"] == "new-token"


def test_first_connect_creates_a_secret(monkeypatch):
    status, writes = _netdata_connect(monkeypatch, lambda *a, **k: {})
    assert status == 200
    assert len(writes[0]["webhook_secret"]) >= 43


def test_connect_fails_instead_of_replacing_the_secret_when_the_store_is_unreadable(monkeypatch):
    def unreadable(*a, raise_on_error=False, **k):
        if raise_on_error:
            raise RuntimeError("vault unavailable")
        return {}

    status, writes = _netdata_connect(monkeypatch, unreadable)
    assert status == 500
    assert writes == []


def test_splunk_account_save_keeps_the_secret(monkeypatch):
    import utils.splunk_accounts as sa
    import utils.auth.token_management as tm
    writes = []
    monkeypatch.setattr(tm, "get_token_data", lambda *a, **k: {"webhook_secret": SECRET})
    monkeypatch.setattr(tm, "store_tokens_in_db", lambda user_id, payload, provider, **k: writes.append(payload))
    monkeypatch.setattr(sa, "sync_splunk_connections", lambda *a, **k: None)
    sa.store_splunk_accounts(USER, [{"base_url": "https://splunk.example.com:8089", "api_token": "t"}])
    assert writes[0]["webhook_secret"] == SECRET


# --------------------------------------------------------------------------- #
# Upgrading a legacy connection from the webhook-url endpoint
# --------------------------------------------------------------------------- #

def test_ensure_returns_the_existing_secret_without_writing(fake_store):
    assert webhook_auth.ensure_webhook_secret(USER, "datadog", {"webhook_secret": SECRET}) == SECRET
    assert fake_store.writes == []


def test_ensure_adds_a_secret_to_a_legacy_connection(fake_store):
    fake_store.creds = {"api_key": "k"}
    secret = webhook_auth.ensure_webhook_secret(USER, "datadog", {"api_key": "k"})
    assert secret
    assert fake_store.writes == [{"api_key": "k", "webhook_secret": secret}]


def test_ensure_returns_the_secret_a_concurrent_request_already_created(fake_store):
    """The caller's copy predates another request's write; the re-read under the lock wins."""
    fake_store.creds = {"api_key": "k", "webhook_secret": SECRET}
    assert webhook_auth.ensure_webhook_secret(USER, "datadog", {"api_key": "k"}) == SECRET
    assert fake_store.writes == []


def test_ensure_does_not_write_when_the_store_is_unreadable(fake_store):
    fake_store.creds = {"api_key": "k"}
    fake_store.fail_read = True
    assert webhook_auth.ensure_webhook_secret(USER, "datadog", {"api_key": "k"}) is None
    assert fake_store.writes == []


def test_ensure_does_not_create_a_connection_unless_allowed(fake_store):
    assert webhook_auth.ensure_webhook_secret(USER, "datadog", {}) is None
    assert fake_store.writes == []


# --------------------------------------------------------------------------- #
# Grafana: no anonymous auto-connect; connected after the first authenticated webhook
# --------------------------------------------------------------------------- #

def _grafana_with_row(monkeypatch, row_exists, is_active):
    client, spec, sink, mod = _client(monkeypatch, "grafana", None)
    monkeypatch.setattr(mod, "_has_grafana_row", lambda user_id: (row_exists, is_active))
    writes = []
    monkeypatch.setattr(mod, "store_tokens_in_db", lambda *a, **k: writes.append(a))
    monkeypatch.setattr(mod, "_set_grafana_active", lambda *a, **k: writes.append(a) or True)
    return client, spec, sink, writes


def test_grafana_anonymous_request_does_not_auto_connect(monkeypatch):
    client, spec, sink, writes = _grafana_with_row(monkeypatch, False, False)
    resp = _post(client, spec)
    assert resp.status_code == 404
    assert writes == []
    assert sink == []


def test_grafana_anonymous_request_does_not_reactivate_a_disconnected_connection(monkeypatch):
    client, spec, sink, writes = _grafana_with_row(monkeypatch, True, False)
    resp = _post(client, spec, headers={HEADER: SECRET})
    assert resp.status_code == 404
    assert writes == []
    assert sink == []


def test_grafana_webhook_url_creates_a_pending_connection(monkeypatch, fake_store):
    mod = _load("routes.grafana.grafana_routes")
    fake_store.install()
    monkeypatch.setattr(mod, "get_token_data", lambda *a, **k: {})
    monkeypatch.setattr(mod, "external_backend_url", lambda *a, **k: "https://aurora.example.com")
    import utils.secrets.secret_ref_utils as sru
    monkeypatch.setattr(sru, "get_token_owner_id", lambda user_id, provider: user_id)

    app = flask.Flask(__name__)
    with app.test_request_context():
        body = mod.get_webhook_url.__wrapped__(USER).get_json()

    secret = body["webhookSecret"]
    assert fake_store.writes == [{"webhook_secret": secret, "awaiting_first_webhook": True}]
    assert body["headerName"] == HEADER
    assert body["webhookUrl"] == f"https://aurora.example.com/grafana/alerts/webhook/{USER}?secret={secret}"
    # Not connected until Grafana actually delivers a webhook with the secret.
    assert mod.grafana_connected(fake_store.creds) is False


def test_grafana_first_authenticated_webhook_completes_the_connection_without_rca(monkeypatch):
    stored = {"webhook_secret": SECRET, "awaiting_first_webhook": True}
    client, spec, sink, mod = _client(monkeypatch, "grafana", stored)
    monkeypatch.setattr(mod, "get_token_data", lambda *a, **k: dict(stored))
    monkeypatch.setattr(mod, "webhook_secret_lock", lambda *a, **k: nullcontext())
    import utils.secrets.secret_ref_utils as sru
    monkeypatch.setattr(sru, "get_token_owner_id", lambda user_id, provider: user_id)

    def write(user_id, payload, provider, **k):
        stored.clear()
        stored.update(payload)

    monkeypatch.setattr(mod, "store_tokens_in_db", write)

    assert mod.grafana_connected(stored) is False
    assert _post(client, spec, headers={HEADER: SECRET}).status_code == 200
    assert stored == {"webhook_secret": SECRET}
    assert mod.grafana_connected(stored) is True
    assert sink[0][1]["skip_rca"] is True

    assert _post(client, spec, headers={HEADER: SECRET}).status_code == 200
    assert sink[1][1]["skip_rca"] is False


def test_grafana_status_and_connector_list_agree(monkeypatch):
    mod = _load("routes.grafana.grafana_routes")
    assert mod.grafana_connected({}) is False  # nothing stored, or a legacy row without a secret
    assert mod.grafana_connected({"webhook_secret": SECRET, "awaiting_first_webhook": True}) is False
    assert mod.grafana_connected({"webhook_secret": SECRET}) is True

    import routes.connector_status as cs
    monkeypatch.setattr(cs, "get_token_data", lambda *a, **k: {"webhook_secret": SECRET, "awaiting_first_webhook": True})
    assert cs._check_grafana(USER, "org") == {"connected": False}
    monkeypatch.setattr(cs, "get_token_data", lambda *a, **k: {"webhook_secret": SECRET})
    assert cs._check_grafana(USER, "org") == {"connected": True}


# --------------------------------------------------------------------------- #
# Endpoints that reveal or change the secret need connectors:write (viewers have read)
# --------------------------------------------------------------------------- #

ROUTES_DIR = Path(__file__).resolve().parents[2] / "routes"
WRITE_ROUTES = {
    "grafana/grafana_routes.py": "get_webhook_url",
    "datadog/datadog_routes.py": "webhook_url",
    "pagerduty/pagerduty_routes.py": "get_webhook_url",
    "newrelic/newrelic_routes.py": "webhook_url",
    "opsgenie/opsgenie_routes.py": "webhook_url",
    "jira/jira_routes.py": "get_webhook_url",
    "dynatrace/dynatrace_routes.py": "get_webhook_url",
    "netdata/netdata_routes.py": "get_webhook_url",
    "splunk/splunk_routes.py": "get_webhook_url",
    "webhook_secret_routes.py": "rotate_webhook_secret_route",
}


@pytest.mark.parametrize("rel_path,func_name", sorted(WRITE_ROUTES.items()))
def test_secret_endpoints_require_connectors_write(rel_path, func_name):
    tree = ast.parse((ROUTES_DIR / rel_path).read_text())
    func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == func_name)
    perms = [
        tuple(arg.value for arg in dec.args)
        for dec in func.decorator_list
        if isinstance(dec, ast.Call) and getattr(dec.func, "id", None) == "require_permission"
    ]
    assert perms == [("connectors", "write")]


@pytest.mark.parametrize("rel_path", sorted(p for p in WRITE_ROUTES if p != "webhook_secret_routes.py"))
def test_every_vendor_registers_the_rotate_route(rel_path):
    assert "register_webhook_secret_routes(" in (ROUTES_DIR / rel_path).read_text()


# --------------------------------------------------------------------------- #
# Shared helper
# --------------------------------------------------------------------------- #

def test_generate_webhook_secret_is_distinct_and_high_entropy():
    secrets_ = {webhook_auth.generate_webhook_secret() for _ in range(50)}
    assert len(secrets_) == 50
    assert all(len(s) >= 43 for s in secrets_)  # token_urlsafe(32) -> 43 chars, 256 bits


@pytest.mark.parametrize("stored,presented", [
    (None, None), ("", ""), (None, SECRET), ("", SECRET), (SECRET, None), (SECRET, ""),
])
def test_secret_matches_never_matches_a_missing_value(stored, presented):
    assert webhook_auth.secret_matches(stored, presented) is False


def test_secret_matches_uses_constant_time_compare(monkeypatch):
    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(webhook_auth.hmac, "compare_digest", lambda a, b: calls.append((a, b)) or real(a, b))
    assert webhook_auth.secret_matches(SECRET, SECRET) is True
    assert webhook_auth.secret_matches(SECRET, SECRET + "x") is False
    assert len(calls) == 2


def test_secret_matches_handles_non_ascii_without_raising():
    assert webhook_auth.secret_matches(SECRET, "sécret") is False


def test_presented_secrets_collects_every_location():
    app = flask.Flask(__name__)
    headers = {HEADER: "h", "Authorization": _basic("u", "p")}
    with app.test_request_context(headers=headers, query_string={"secret": "q"}):
        assert webhook_auth.presented_secrets(flask.request) == ["h", "p", "q"]
    with app.test_request_context(headers={"Authorization": "Bearer "}):
        assert webhook_auth.presented_secrets(flask.request) == []


def test_with_webhook_secret_keeps_or_creates(monkeypatch):
    monkeypatch.setattr(webhook_auth, "_read_creds_strict", lambda u, p: {"webhook_secret": SECRET})
    assert webhook_auth.with_webhook_secret(USER, "netdata", {"api_token": "t"}) == {
        "api_token": "t", "webhook_secret": SECRET,
    }
    monkeypatch.setattr(webhook_auth, "_read_creds_strict", lambda u, p: {})
    fresh = webhook_auth.with_webhook_secret(USER, "netdata", {"api_token": "t"})
    assert fresh["webhook_secret"] and fresh["webhook_secret"] != SECRET


def test_with_webhook_secret_raises_when_the_store_is_unreadable(monkeypatch):
    def unreadable(u, p):
        raise RuntimeError("vault unavailable")

    monkeypatch.setattr(webhook_auth, "_read_creds_strict", unreadable)
    with pytest.raises(RuntimeError):
        webhook_auth.with_webhook_secret(USER, "netdata", {"api_token": "t"})


def test_redact_webhook_headers():
    headers = {HEADER: SECRET, "Authorization": "Bearer x", "Api-Key": "k", "Content-Type": "application/json"}
    assert webhook_auth.redact_webhook_headers(headers) == {
        HEADER: "<REDACTED>", "Authorization": "<REDACTED>", "Api-Key": "<REDACTED>",
        "Content-Type": "application/json",
    }


def test_rejection_never_logs_the_secret(monkeypatch, caplog):
    _load("routes.datadog.datadog_routes")
    app = flask.Flask(__name__)
    with app.test_request_context(headers={HEADER: "attacker-guess-" + SECRET}):
        resp, status = webhook_auth.reject_unauthenticated_webhook(
            {"webhook_secret": SECRET}, flask.request, provider_name="Datadog", log_prefix="[DATADOG]", user_id=USER,
        )
    assert status == 401
    assert SECRET not in caplog.text
