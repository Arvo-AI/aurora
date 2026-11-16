"""Real Flask routes and RBAC decorator; stub only identity, policy and LLM I/O."""

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest
from flask import Flask, request
from flask_limiter import Limiter


@pytest.fixture
def api(monkeypatch):
    server = Path(__file__).resolve().parents[2]
    identity = types.ModuleType("utils.auth.stateless_auth")
    identity.get_user_id_from_request = lambda: request.headers.get("X-Test-User")
    identity.get_org_id_from_request = lambda: request.headers.get("X-Test-Org")
    policy = types.ModuleType("utils.auth.enforcer")
    policy.enforce_with_reload = Mock(return_value=True)
    monkeypatch.setitem(sys.modules, identity.__name__, identity)
    monkeypatch.setitem(sys.modules, policy.__name__, policy)

    def load(name, path):
        spec = importlib.util.spec_from_file_location(name, server / path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        return module

    decorators = load("utils.auth.rbac_decorators", "utils/auth/rbac_decorators.py")
    monkeypatch.setattr(decorators, "_audit_auth_failure", Mock())
    limiter_module = types.ModuleType("utils.web.limiter_ext")
    limiter_module.limiter = Limiter(key_func=lambda: request.headers.get("X-Test-User", "anonymous"), storage_uri="memory://")
    monkeypatch.setitem(sys.modules, limiter_module.__name__, limiter_module)
    route = load("routes.ci_diagnosis_routes", "routes/ci_diagnosis_routes.py")
    service = Mock(return_value={"status": "insufficient_evidence", "summary": "Need the error output."})
    monkeypatch.setattr(route, "diagnose", service)
    monkeypatch.setattr(route, "get_hook", lambda name: lambda *args: (True, None))
    app = Flask(__name__)
    app.config["TESTING"] = True
    limiter_module.limiter.init_app(app)
    app.register_blueprint(route.ci_diagnosis_bp)
    return app.test_client(), service, route, policy


HEADERS = {"X-Test-User": "candidate", "X-Test-Org": "test-org"}
PAYLOAD = {"log": "Build failed", "consent": True}


@pytest.mark.parametrize("headers,code", [({}, 401), ({"X-Test-User": "candidate"}, 403)])
def test_authentication_and_org_required(api, headers, code):
    client, service, _, _ = api
    assert client.post("/api/ci-diagnosis", json=PAYLOAD, headers=headers).status_code == code
    service.assert_not_called()


def test_permission_required(api):
    client, service, _, policy = api
    policy.enforce_with_reload.return_value = False
    assert client.post("/api/ci-diagnosis", json=PAYLOAD, headers=HEADERS).status_code == 403
    policy.enforce_with_reload.assert_called_once_with("candidate", "test-org", "chat", "write")
    service.assert_not_called()


@pytest.mark.parametrize("payload", [None, [], {}, {"log": "x"}, {"log": "x", "consent": "true"}, {"log": [], "consent": True}, {"log": "x" * 20_001, "consent": True}])
def test_bad_input_does_not_call_provider(api, payload):
    client, service, _, _ = api
    assert client.post("/api/ci-diagnosis", json=payload, headers=HEADERS).status_code == 400
    service.assert_not_called()


def test_request_body_limit(api):
    client, service, _, _ = api
    assert client.post("/api/ci-diagnosis", data=b"x" * 150_001, content_type="application/json", headers=HEADERS).status_code == 413
    service.assert_not_called()


@pytest.mark.parametrize("blocked", ["verify_entitlement", "before_llm_call"])
def test_honors_organization_gates(api, monkeypatch, blocked):
    client, service, route, _ = api
    monkeypatch.setattr(route, "get_hook", lambda name: lambda *a: (name != blocked, "blocked"))
    assert client.post("/api/ci-diagnosis", json=PAYLOAD, headers=HEADERS).status_code == 403
    service.assert_not_called()


def test_identity_is_server_resolved_and_response_is_not_cached(api):
    client, service, _, _ = api
    response = client.post("/api/ci-diagnosis", json={**PAYLOAD, "user_id": "attacker", "org_id": "other"}, headers=HEADERS)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    service.assert_called_once_with(["Build failed"], "candidate", "test-org")


def test_endpoint_limit_when_deployment_limiter_enabled(api):
    client, service, _, _ = api
    for _ in range(5):
        assert client.post("/api/ci-diagnosis", json=PAYLOAD, headers=HEADERS).status_code == 200
    assert client.post("/api/ci-diagnosis", json=PAYLOAD, headers=HEADERS).status_code == 429
    assert service.call_count == 5


@pytest.mark.parametrize("error_name,code", [("ProviderUnavailable", 503), ("InvalidDiagnosis", 502), ("RuntimeError", 500)])
def test_safe_retryable_errors(api, caplog, error_name, code):
    client, service, route, _ = api
    marker = "PRIVATE_LOG_CONTENT"
    error_type = RuntimeError if error_name == "RuntimeError" else getattr(route, error_name)
    service.side_effect = error_type(marker)
    response = client.post("/api/ci-diagnosis", json=PAYLOAD, headers=HEADERS)
    assert response.status_code == code
    assert marker not in response.get_data(as_text=True)
    assert marker not in caplog.text
