"""Alert webhooks must be authenticated by the connection's webhook secret.

``/<vendor>/webhook/<user_id>`` routes skip session auth (the vendor has no
Aurora login). The ``user_id`` is not a credential: any org member reads every
member's id from ``GET /api/orgs/current``. Before this fix, nine of these
routes accepted any POST carrying a valid ``user_id`` and started an RCA
investigation as that user, using that user's connected credentials.

These tests drive the real blueprints through Flask's test client with the
credential store and Celery dispatch replaced by in-memory fakes.
"""

import ast
import hmac
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

flask = pytest.importorskip("flask")

from utils.web import webhook_auth  # noqa: E402

USER = "11111111-2222-3333-4444-555555555555"
SECRET = "s3cr3t-" + "x" * 36
HEADER = webhook_auth.WEBHOOK_HEADER_NAME


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
    monkeypatch.setattr(mod, "list_splunk_accounts", lambda user_id, **k: [{"account_id": "x"}] if creds is not None else [])
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
    # context variables. Re-import the helper, then the route module that
    # imports from it, so both use the Flask the test client drives.
    global webhook_auth
    webhook_auth = importlib.reload(sys.modules["utils.web.webhook_auth"])
    if module_name in sys.modules:
        return importlib.reload(sys.modules[module_name])
    return importlib.import_module(module_name)


@pytest.fixture(params=sorted(VENDORS))
def vendor(request):
    return request.param


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


CONNECTED = {"api_token": "vendor-api-token", "webhook_secret": SECRET}


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
    assert sink == []


def test_legacy_connection_without_stored_secret_is_rejected(monkeypatch, vendor):
    client, spec, sink, _ = _client(monkeypatch, vendor, {"api_token": "vendor-api-token"})
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
        import base64
        headers = {"Authorization": "Basic " + base64.b64encode(f"aurora:{SECRET}".encode()).decode()}

    resp = _post(client, spec, headers=headers, query=query)

    assert resp.status_code in (200, 202), resp.get_data(as_text=True)
    assert len(sink) == 1
    args, kwargs = sink[0]
    assert USER in list(args) + list(kwargs.values())


def test_secret_is_not_stored_with_the_alert(monkeypatch, vendor):
    """Handlers persist request headers as alert metadata; the secret must be redacted."""
    client, spec, sink, _ = _client(monkeypatch, vendor, CONNECTED)
    _post(client, spec, headers={HEADER: SECRET})
    assert len(sink) == 1
    assert SECRET not in repr(sink)


# --------------------------------------------------------------------------- #
# Grafana: an anonymous request no longer creates or re-activates a connection
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


def test_grafana_webhook_url_creates_the_connection_with_a_secret(monkeypatch):
    mod = _load("routes.grafana.grafana_routes")
    writes = []
    monkeypatch.setattr(mod, "get_token_data", lambda *a, **k: {})
    monkeypatch.setattr(mod, "store_tokens_in_db", lambda user_id, payload, provider, **k: writes.append(payload))
    monkeypatch.setattr(mod, "external_backend_url", lambda *a, **k: "https://aurora.example.com")
    import utils.secrets.secret_ref_utils as sru
    monkeypatch.setattr(sru, "get_token_owner_id", lambda user_id, provider: user_id)

    app = flask.Flask(__name__)
    with app.test_request_context():
        body = mod.get_webhook_url.__wrapped__(USER).get_json()

    assert len(writes) == 1
    secret = writes[0]["webhook_secret"]
    assert body["webhookSecret"] == secret
    assert body["headerName"] == HEADER
    assert body["webhookUrl"] == f"https://aurora.example.com/grafana/alerts/webhook/{USER}?secret={secret}"


# --------------------------------------------------------------------------- #
# Endpoints that reveal the secret need connectors:write (viewers have read)
# --------------------------------------------------------------------------- #

ROUTES_DIR = Path(__file__).resolve().parents[2] / "routes"
WEBHOOK_URL_ROUTES = {
    "grafana/grafana_routes.py": "get_webhook_url",
    "datadog/datadog_routes.py": "webhook_url",
    "pagerduty/pagerduty_routes.py": "get_webhook_url",
    "newrelic/newrelic_routes.py": "webhook_url",
    "opsgenie/opsgenie_routes.py": "webhook_url",
    "jira/jira_routes.py": "get_webhook_url",
    "dynatrace/dynatrace_routes.py": "get_webhook_url",
    "netdata/netdata_routes.py": "get_webhook_url",
    "splunk/splunk_routes.py": "get_webhook_url",
}


@pytest.mark.parametrize("rel_path,func_name", sorted(WEBHOOK_URL_ROUTES.items()))
def test_webhook_url_endpoint_requires_connectors_write(rel_path, func_name):
    tree = ast.parse((ROUTES_DIR / rel_path).read_text())
    func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == func_name)
    perms = [
        tuple(arg.value for arg in dec.args)
        for dec in func.decorator_list
        if isinstance(dec, ast.Call) and getattr(dec.func, "id", None) == "require_permission"
    ]
    assert perms == [("connectors", "write")]


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


def test_with_webhook_secret_keeps_an_existing_secret():
    assert webhook_auth.with_webhook_secret({"api_token": "t"}, {"webhook_secret": SECRET}) == {
        "api_token": "t", "webhook_secret": SECRET,
    }
    fresh = webhook_auth.with_webhook_secret({"api_token": "t"}, {})
    assert fresh["webhook_secret"] and fresh["webhook_secret"] != SECRET


def test_ensure_webhook_secret_adds_one_to_a_legacy_connection(monkeypatch):
    import utils.auth.token_management as tm
    import utils.secrets.secret_ref_utils as sru
    writes = []
    monkeypatch.setattr(tm, "store_tokens_in_db", lambda user_id, payload, provider, **k: writes.append((user_id, payload, k)))
    monkeypatch.setattr(sru, "get_token_owner_id", lambda user_id, provider: "owner-id")

    assert webhook_auth.ensure_webhook_secret(USER, "datadog", {"webhook_secret": SECRET}) == SECRET
    assert writes == []

    secret = webhook_auth.ensure_webhook_secret(USER, "datadog", {"api_key": "k"})
    assert secret
    assert writes == [("owner-id", {"api_key": "k", "webhook_secret": secret}, {"schedule_prediscovery": False})]


def test_ensure_webhook_secret_returns_none_when_the_store_fails(monkeypatch):
    import utils.auth.token_management as tm
    import utils.secrets.secret_ref_utils as sru

    def boom(*a, **k):
        raise RuntimeError("vault down")

    monkeypatch.setattr(tm, "store_tokens_in_db", boom)
    monkeypatch.setattr(sru, "get_token_owner_id", lambda user_id, provider: user_id)
    assert webhook_auth.ensure_webhook_secret(USER, "datadog", {"api_key": "k"}) is None


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
