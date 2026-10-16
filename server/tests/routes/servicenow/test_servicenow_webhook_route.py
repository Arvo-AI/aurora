"""HTTP-level tests of the inbound ServiceNow webhook through Flask's test client.

The route is intentionally outside the RBAC decorators (the Business Rule has
no Aurora session); the shared secret is the only gate. Heavy import-time
dependencies are stubbed exactly as in test_servicenow_connector.py.
"""

import importlib
import sys
from types import ModuleType

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
SECRET = "s3cr3t-" + "x" * 30
SYS_ID = "e" * 32


@pytest.fixture
def app(monkeypatch):
    # Earlier modules in the full suite reload Flask's globals, which leaves this
    # route module holding a request proxy bound to stale context variables.
    # Re-import it so the view reads the same ``flask.request`` the test client sets.
    global routes
    routes = importlib.reload(routes)
    application = flask.Flask(__name__)
    application.register_blueprint(routes.servicenow_bp, url_prefix="/servicenow")
    monkeypatch.setattr(routes, "get_token_data", lambda user_id, provider: (
        {"instance_url": "https://acme.service-now.com", "webhook_secret": SECRET} if user_id == USER else None
    ))
    enqueued = []
    # The Celery stub installed by whichever test module loaded first may or may not
    # give the task a .delay attribute; set it either way.
    monkeypatch.setattr(routes.process_servicenow_event, "delay",
                        lambda payload, user_id: enqueued.append((payload, user_id)), raising=False)
    application.config["ENQUEUED"] = enqueued
    return application


def _post(client, user=USER, headers=None, json=None):
    body = {"sys_id": SYS_ID, "number": "INC0010001", "priority": "1", "event": "inserted"} if json is None else json
    return client.post(f"/servicenow/webhook/{user}", json=body, headers=headers or {})


def test_accepts_with_header_secret_and_enqueues(app):
    resp = _post(app.test_client(), headers={"X-Aurora-Webhook-Secret": SECRET})
    assert resp.status_code == 200
    assert resp.get_json() == {"received": True}
    assert app.config["ENQUEUED"] == [({"sys_id": SYS_ID, "number": "INC0010001", "priority": "1", "event": "inserted"}, USER)]


def test_accepts_bearer_secret(app):
    resp = _post(app.test_client(), headers={"Authorization": f"Bearer {SECRET}"})
    assert resp.status_code == 200
    assert len(app.config["ENQUEUED"]) == 1


def test_rejects_missing_or_wrong_secret(app):
    client = app.test_client()
    assert _post(client).status_code == 401
    assert _post(client, headers={"X-Aurora-Webhook-Secret": SECRET[:-1]}).status_code == 401
    assert app.config["ENQUEUED"] == []


def test_unknown_user_is_404_without_leaking_the_secret_check(app):
    resp = _post(app.test_client(), user="99999999-0000-0000-0000-000000000000",
                 headers={"X-Aurora-Webhook-Secret": SECRET})
    assert resp.status_code == 404
    assert app.config["ENQUEUED"] == []


def test_malformed_user_id_is_400(app):
    resp = _post(app.test_client(), user="..%2F..", headers={"X-Aurora-Webhook-Secret": SECRET})
    assert resp.status_code in (400, 404)
    assert app.config["ENQUEUED"] == []


@pytest.mark.parametrize("body", [{}, {"number": "INC1"}, {"sys_id": "not-a-sys-id"}, ["list"]])
def test_rejects_payload_without_a_valid_sys_id(app, body):
    resp = _post(app.test_client(), headers={"X-Aurora-Webhook-Secret": SECRET}, json=body)
    assert resp.status_code == 400
    assert app.config["ENQUEUED"] == []


def test_broker_failure_is_503(app, monkeypatch):
    def _boom(payload, user_id):
        raise RuntimeError("broker down")
    monkeypatch.setattr(routes.process_servicenow_event, "delay", _boom, raising=False)
    resp = _post(app.test_client(), headers={"X-Aurora-Webhook-Secret": SECRET})
    assert resp.status_code == 503
