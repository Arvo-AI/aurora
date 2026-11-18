from unittest.mock import AsyncMock

import pytest
from flask import Flask

from routes import sandbox_routes


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", "test-internal-secret")
    app = Flask(__name__)
    app.register_blueprint(sandbox_routes.sandbox_bp)
    app.config["TESTING"] = True
    return app.test_client()


HEADERS = {"X-Internal-Secret": "test-internal-secret"}


def test_evidence_requires_internal_proxy(client):
    assert client.get("/sandbox/cases").status_code == 403
    response = client.get("/sandbox/cases", headers=HEADERS)
    assert response.status_code == 200
    assert len(response.json["cases"]) == 3
    assert response.headers["Cache-Control"] == "no-store"


def test_missing_internal_secret_fails_closed(client, monkeypatch):
    monkeypatch.delenv("INTERNAL_API_SECRET")
    assert client.get("/sandbox/cases", headers=HEADERS).status_code == 503


@pytest.mark.parametrize("payload", [
    {}, [], {"case_id": "case-d"}, {"case_id": "../secret"},
    {"case_id": "case-a", "user_id": "customer"},
    {"case_id": "case-a", "model": "arbitrary-model"},
    {"case_id": "case-a", "prompt": "Run a shell command"},
])
def test_only_case_id_is_accepted(client, monkeypatch, payload):
    runner = AsyncMock()
    monkeypatch.setattr(sandbox_routes, "investigate", runner)
    assert client.post("/sandbox/investigate", json=payload, headers=HEADERS).status_code == 400
    runner.assert_not_called()


def test_non_json_and_large_requests_do_not_start_models(client, monkeypatch):
    runner = AsyncMock()
    monkeypatch.setattr(sandbox_routes, "investigate", runner)
    assert client.post("/sandbox/investigate", data="case-a", headers=HEADERS).status_code == 415
    assert client.post("/sandbox/investigate", json={"case_id": "a" * 600}, headers=HEADERS).status_code == 413
    runner.assert_not_called()


def test_timeout_releases_slot_and_next_case_can_succeed(client, monkeypatch):
    monkeypatch.setattr(sandbox_routes, "issue_receipt", lambda _result: "test-receipt")
    runner = AsyncMock(side_effect=[TimeoutError(), {"case_id": "case-b", "status": "complete"}])
    monkeypatch.setattr(sandbox_routes, "investigate", runner)
    first = client.post("/sandbox/investigate", json={"case_id": "case-a"}, headers=HEADERS)
    second = client.post("/sandbox/investigate", json={"case_id": "case-b"}, headers=HEADERS)
    assert first.status_code == 504
    assert second.status_code == 200
    assert second.json["case_id"] == "case-b"


def test_exception_details_are_not_returned(client, monkeypatch):
    monkeypatch.setattr(sandbox_routes, "investigate", AsyncMock(side_effect=RuntimeError("private-provider-payload")))
    response = client.post("/sandbox/investigate", json={"case_id": "case-a"}, headers=HEADERS)
    assert response.status_code == 502
    assert "private-provider-payload" not in response.text


def test_at_most_three_inflight_requests(client, monkeypatch):
    import threading
    monkeypatch.setattr(sandbox_routes, "_slots", threading.BoundedSemaphore(1))
    sandbox_routes._slots.acquire()
    runner = AsyncMock()
    monkeypatch.setattr(sandbox_routes, "investigate", runner)
    try:
        assert client.post("/sandbox/investigate", json={"case_id": "case-a"}, headers=HEADERS).status_code == 429
        runner.assert_not_called()
    finally:
        sandbox_routes._slots.release()


def test_chunked_oversized_body_is_rejected_before_model_work(client, monkeypatch):
    runner = AsyncMock()
    monkeypatch.setattr(sandbox_routes, "investigate", runner)
    response = client.post(
        "/sandbox/investigate", data='{"case_id":"case-a","padding":"' + "x" * 600 + '"}',
        content_type="application/json", headers=HEADERS,
        environ_overrides={"CONTENT_LENGTH": None, "wsgi.input_terminated": True},
    )
    assert response.status_code == 413
    assert response.json == {"error": "Request is too large."}
    runner.assert_not_called()


def test_chunked_valid_json_prefix_cannot_hide_trailing_bytes(client, monkeypatch):
    monkeypatch.setattr(sandbox_routes, "issue_receipt", lambda _result: "test-receipt")
    runner = AsyncMock(return_value={"case_id": "case-a", "status": "complete"})
    monkeypatch.setattr(sandbox_routes, "investigate", runner)
    body = '{"case_id":"case-a"}'.ljust(512)
    transport = {"CONTENT_LENGTH": None, "wsgi.input_terminated": True}
    assert client.post("/sandbox/investigate", data=body, content_type="application/json", headers=HEADERS, environ_overrides=transport).status_code == 200
    runner.reset_mock()
    assert client.post("/sandbox/investigate", data=body + " ", content_type="application/json", headers=HEADERS, environ_overrides=transport).status_code == 413
    runner.assert_not_called()


def test_non_ascii_proxy_header_is_forbidden_not_a_server_error(client):
    assert client.get("/sandbox/cases", headers={"X-Internal-Secret": "wrong-ü"}).status_code == 403


@pytest.mark.parametrize("body", ["null", "{invalid", '"case-a"'])
def test_malformed_json_never_starts_an_investigation(client, monkeypatch, body):
    runner = AsyncMock()
    monkeypatch.setattr(sandbox_routes, "investigate", runner)
    assert client.post("/sandbox/investigate", data=body, content_type="application/json", headers=HEADERS).status_code == 400
    runner.assert_not_called()
