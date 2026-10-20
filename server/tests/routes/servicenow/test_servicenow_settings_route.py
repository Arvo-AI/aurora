"""PUT/GET /servicenow/rca-settings and GET /servicenow/field-options through Flask's test client."""

import importlib
import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest

flask = pytest.importorskip("flask")


def _install_import_stubs() -> None:
    rca_pb = ModuleType("chat.background.rca_prompt_builder")
    rca_pb.build_rca_prompt = lambda *a, **k: ("prompt", "rail")  # type: ignore[attr-defined]
    sys.modules.setdefault("chat.background.rca_prompt_builder", rca_pb)
    celery_cfg = ModuleType("celery_config")

    class _App:
        def task(self, *dargs, **dkwargs):
            def _decorator(fn):
                fn.delay = lambda *a, **k: None  # type: ignore[attr-defined]
                return fn
            if dargs and callable(dargs[0]) and not dkwargs:
                return dargs[0]
            return _decorator

    celery_cfg.celery_app = _App()  # type: ignore[attr-defined]
    sys.modules.setdefault("celery_config", celery_cfg)


_install_import_stubs()

from routes.servicenow import servicenow_routes as routes  # noqa: E402

USER = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def app(monkeypatch):
    global routes
    name = "routes.servicenow.servicenow_routes"
    # tests/auth evicts routes.* from sys.modules; reload() then raises ImportError.
    routes = importlib.reload(sys.modules[name]) if name in sys.modules else importlib.import_module(name)
    application = flask.Flask(__name__)
    application.register_blueprint(routes.servicenow_bp, url_prefix="/servicenow")

    # RBAC: inject the user like the decorator would, without a session.
    def _passthrough(resource, action):
        def _wrap(fn):
            def _inner(*args, **kwargs):
                return fn(USER, *args, **kwargs)
            _inner.__name__ = fn.__name__
            return _inner
        return _wrap
    stored = {}
    monkeypatch.setattr(routes, "_store_settings", lambda user_id, updates: stored.update(dict(updates)) or True)
    monkeypatch.setattr(routes, "get_rca_settings", lambda user_id: {"stored": dict(stored)})
    monkeypatch.setattr(routes, "_stored_credentials", lambda user_id: {"instance_url": "https://acme.service-now.com"})
    monkeypatch.setattr(routes, "resolve_org_id", lambda user_id: "org-1")
    monkeypatch.setattr(routes, "get_org_id_from_request", lambda: "org-1")
    cache = ModuleType("chat.backend.agent.tools.mcp_tools")
    cache.clear_credentials_cache = lambda user_id: None  # type: ignore[attr-defined]
    cache.clear_credentials_cache_for_org = lambda org_id: stored.__setitem__("_cache_cleared", org_id)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "chat.backend.agent.tools.mcp_tools", cache)
    seed = ModuleType("services.memory.servicenow_memory")
    seed.seed_servicenow_memory = lambda user_id, org_id=None: stored.__setitem__("_seeded", org_id)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "services.memory.servicenow_memory", seed)
    application.config["STORED"] = stored
    # The decorator already wrapped the views at import; swap the view functions' auth by rebinding.
    for endpoint, view in list(application.view_functions.items()):
        if endpoint.startswith("servicenow.") and endpoint not in ("servicenow.webhook",):
            original = getattr(routes, view.__name__, None)
            if original is not None and hasattr(original, "__wrapped__"):
                application.view_functions[endpoint] = _passthrough("connectors", "write")(original.__wrapped__)
    return application


def _put(app, body):
    return app.test_client().put("/servicenow/rca-settings", json=body)


@pytest.mark.skipif(
    not hasattr(routes.update_rca_settings, "__wrapped__"),
    reason="require_permission does not expose __wrapped__ in this build",
)
class TestSettings:
    def test_saves_every_field_atomically_and_clears_the_tool_cache(self, app):
        resp = _put(app, {
            "rcaMode": "filters",
            "rcaRules": [{"priorities": ["1", "2"]}],
            "workNotesEnabled": True,
            "writeMode": "full",
            "resolveSync": True,
            "resolveCloseCode": "  Solution   provided ",
        })
        assert resp.status_code == 200, resp.get_json()
        stored = app.config["STORED"]
        assert stored[routes.PREF_RCA_MODE] == "filters"
        assert stored[routes.PREF_RCA_RULES][0]["priorities"] == ["1", "2"]
        assert stored[routes.PREF_WORK_NOTES] is True
        assert stored[routes.PREF_WRITE_MODE] == "full"
        assert stored[routes.PREF_RESOLVE_SYNC] is True
        assert stored[routes.PREF_RESOLVE_CLOSE_CODE] == "Solution provided"
        assert stored["_cache_cleared"] == "org-1"
        assert "_seeded" not in stored

    def test_auto_mode_seeds_the_policy_memory(self, app):
        assert _put(app, {"rcaMode": "auto"}).status_code == 200
        assert app.config["STORED"]["_seeded"] == "org-1"

    @pytest.mark.parametrize("body, message", [
        ({}, "is required"),
        ({"rcaMode": "sometimes"}, "rcaMode must be one of"),
        ({"rcaRules": [{}]}, "at least one"),
        ({"workNotesEnabled": "yes"}, "must be a boolean"),
        ({"writeMode": "all"}, "writeMode must be one of"),
        ({"resolveSync": 1}, "must be a boolean"),
        ({"resolveCloseCode": ""}, "1–80"),
        ({"resolveCloseCode": 5}, "must be text"),
    ])
    def test_invalid_bodies_are_400_and_store_nothing(self, app, body, message):
        resp = _put(app, body)
        assert resp.status_code == 400
        assert message in resp.get_json()["error"]
        assert app.config["STORED"] == {}

    def test_switching_work_notes_off_resets_the_scope_and_the_resolve_sync(self, app):
        """Off means off: a remembered full scope must not survive the switch being turned off."""
        assert _put(app, {"workNotesEnabled": False}).status_code == 200
        stored = app.config["STORED"]
        assert stored[routes.PREF_WORK_NOTES] is False
        assert stored[routes.PREF_WRITE_MODE] == "notes_only"
        assert stored[routes.PREF_RESOLVE_SYNC] is False

    def test_switching_work_notes_on_leaves_the_scope_alone(self, app):
        assert _put(app, {"workNotesEnabled": True}).status_code == 200
        stored = app.config["STORED"]
        assert stored[routes.PREF_WORK_NOTES] is True
        assert routes.PREF_WRITE_MODE not in stored

    def test_write_back_needs_a_connection(self, app, monkeypatch):
        monkeypatch.setattr(routes, "_stored_credentials", lambda user_id: None)
        resp = _put(app, {"workNotesEnabled": True})
        assert resp.status_code == 400
        assert "Connect ServiceNow" in resp.get_json()["error"]
        assert app.config["STORED"] == {}

    def test_field_options_reads_the_instance(self, app, monkeypatch):
        client = MagicMock()
        client.list_assignment_groups.return_value = ["Network", "Platform"]
        client.list_categories.return_value = ["Software"]
        monkeypatch.setattr(routes.ServiceNowClient, "from_credentials", classmethod(lambda cls, c: client))
        resp = app.test_client().get("/servicenow/field-options")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["assignmentGroups"] == ["Network", "Platform"]
        assert data["categories"] == ["Software"]
        assert [p["value"] for p in data["priorities"]] == ["1", "2", "3", "4", "5"]
        assert data["errors"] == []


@pytest.mark.skipif(
    not hasattr(routes.status, "__wrapped__"),
    reason="require_permission does not expose __wrapped__ in this build",
)
class TestStatusProbe:
    """A failed live probe must not turn a stored connection into 'disconnected', and no exception text leaks."""

    @pytest.mark.parametrize("status_code, message", [
        (503, "ServiceNow returned HTTP 503"),
        (None, "Could not reach ServiceNow (timeout or network error)"),
        (401, "ServiceNow rejected the credentials (HTTP 401)"),
    ])
    def test_probe_failure_keeps_connected_and_reports_a_fixed_error(self, app, monkeypatch, status_code, message):
        class _Client:
            def test_connection(self):
                raise routes.ServiceNowAPIError("socket detail that must not reach the client", status_code)
        monkeypatch.setattr(routes.ServiceNowClient, "from_credentials", staticmethod(lambda creds: _Client()))
        resp = app.test_client().get("/servicenow/status")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["connected"] is True
        assert body["instanceUrl"] == "https://acme.service-now.com"
        assert body["error"] == message
        assert "socket detail" not in resp.get_data(as_text=True)

    def test_healthy_probe_has_no_error(self, app, monkeypatch):
        class _Client:
            def test_connection(self):
                return {"ok": True}
        monkeypatch.setattr(routes.ServiceNowClient, "from_credentials", staticmethod(lambda creds: _Client()))
        body = app.test_client().get("/servicenow/status").get_json()
        assert body["connected"] is True and "error" not in body
