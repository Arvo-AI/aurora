"""/pipeline routes through Flask's test client: auth, no vendor credentials, limits."""

from __future__ import annotations

import importlib
import json
import pathlib
import sys
from contextlib import contextmanager
from types import ModuleType

import pytest

flask = pytest.importorskip("flask")

USER = "11111111-2222-3333-4444-555555555555"
SECRET = "pipeline-test-secret"
class _Cursor:
    def __init__(self, db):
        self.db = db
        self._row = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.db["sql"].append((s, params))
        if s.startswith("SELECT COUNT(*) FROM pipeline_failure_events"):
            self._row = (0, None)
        elif s.startswith("INSERT INTO pipeline_failure_events"):
            self.db["stored"] = json.loads(params[9])
            self._row = (42,)
        elif s.startswith("SELECT payload, received_at FROM pipeline_failure_events"):
            self._row = (self.db.get("stored"), None) if self.db.get("stored") else None
        else:
            self._row = None

    def fetchone(self):
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return _Cursor(self.db)

    def commit(self):
        self.db["commits"] += 1


class _Pool:
    def __init__(self, db):
        self.db = db

    @contextmanager
    def get_admin_connection(self):
        yield _Conn(self.db)


@pytest.fixture
def env(monkeypatch):
    name = "routes.pipeline.pipeline_routes"
    routes = importlib.reload(sys.modules[name]) if name in sys.modules else importlib.import_module(name)

    db = {"sql": [], "commits": 0}
    queued = []
    token_lookups = []

    fake_tasks = ModuleType("routes.pipeline.tasks")

    class _Task:
        @staticmethod
        def delay(*args, **kwargs):
            queued.append((args, kwargs))

    fake_tasks.process_pipeline_failure = _Task()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "routes.pipeline.tasks", fake_tasks)
    monkeypatch.setattr(routes, "db_pool", _Pool(db))
    monkeypatch.setattr(routes, "set_rls_context", lambda *a, **k: "org-1")

    def _get_token_data(user_id, provider, *a, **k):
        token_lookups.append(provider)
        return {"webhook_secret": SECRET} if provider == "pipeline" and user_id == USER else None

    monkeypatch.setattr(routes, "get_token_data", _get_token_data)
    stored_tokens = []
    monkeypatch.setattr(routes, "store_tokens_in_db",
                        lambda user_id, data, provider, **kw: stored_tokens.append((provider, data, kw)))

    app = flask.Flask(__name__)
    app.register_blueprint(routes.pipeline_bp, url_prefix="/pipeline")

    # RBAC: bypass the session lookup but keep the decorator's user injection.
    for endpoint, view in list(app.view_functions.items()):
        original = getattr(routes, view.__name__, None)
        if endpoint.startswith("pipeline.") and original is not None and hasattr(original, "__wrapped__"):
            inner = original.__wrapped__
            app.view_functions[endpoint] = (lambda f: (lambda *a, **k: f(USER, *a, **k)))(inner)

    return {"app": app, "client": app.test_client(), "routes": routes, "db": db, "queued": queued,
            "token_lookups": token_lookups, "stored_tokens": stored_tokens}


# --- UI submit ---------------------------------------------------------------

def test_empty_submission_is_rejected(env):
    resp = env["client"].post("/pipeline/submit", json={})
    assert resp.status_code == 400
    assert env["queued"] == []


def test_request_size_limit(env, monkeypatch):
    monkeypatch.setattr(env["routes"], "MAX_REQUEST_BYTES", 1000)
    resp = env["client"].post("/pipeline/submit", json={"kind": "log", "log_text": "x" * 5000})
    assert resp.status_code == 413
    assert env["queued"] == []


# --- webhook settings --------------------------------------------------------

def test_rotate_secret_stores_only_an_aurora_issued_secret(env):
    resp = env["client"].post("/pipeline/webhook-secret")
    assert resp.status_code == 200
    body = resp.get_json()
    provider, data, kw = env["stored_tokens"][0]
    assert provider == "pipeline"
    assert set(data) == {"webhook_secret"} and len(data["webhook_secret"]) == 64
    assert kw.get("schedule_prediscovery") is False
    assert body["webhookUrl"].endswith(f"/pipeline/webhook/{USER}")
    assert "aurora_on_failure" in body["callbackSnippet"]
    assert "X-Aurora-Signature" in body["callbackSnippet"]


def test_callback_snippet_is_valid_python(env):
    body = env["client"].get("/pipeline/webhook-url").get_json()
    compile(body["callbackSnippet"], "aurora_callback.py", "exec")


def test_pipeline_package_imports_no_vendor_connector():
    pkg = pathlib.Path(__file__).resolve().parents[3]
    for path in list((pkg / "routes" / "pipeline").glob("*.py")) + list((pkg / "services" / "pipeline_intake").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "from connectors" not in text and "import connectors" not in text, path
