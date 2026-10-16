"""ServiceNowClient against an in-process mock of the Table API and the OAuth
token endpoint: real HTTP round trips through ``requests`` (no network).

The mock implements the response shapes documented for
GET/PATCH /api/now/table/incident and POST /oauth_token.do.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

requests = pytest.importorskip("requests")

from routes.servicenow import servicenow_client as sn  # noqa: E402

SYS_ID = "f" * 32
RECORD = {
    "sys_id": SYS_ID, "number": "INC0010001", "short_description": "Database down",
    "priority": "1 - Critical", "state": "In Progress", "assignment_group": "DBA",
    "cmdb_ci": "pg-prod-1", "opened_at": "2026-10-08 21:15:03",
}


class _State:
    def __init__(self):
        self.requests = []
        self.basic_ok = ("aurora", "pw")
        self.token = "tok-123"
        self.token_calls = 0
        self.lock = threading.Lock()


class _Handler(BaseHTTPRequestHandler):
    state: _State

    def log_message(self, *args):  # silence
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            import base64
            user, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
            return (user, pw) == self.state.basic_ok
        if auth.startswith("Bearer "):
            return auth[7:] == self.state.token
        return False

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode() if length else ""
        parsed = urlparse(self.path)
        with self.state.lock:
            self.state.requests.append(("POST", parsed.path, dict(self.headers), raw))
        if parsed.path == "/oauth_token.do":
            form = parse_qs(raw)
            self.state.token_calls += 1
            if form.get("grant_type") == ["client_credentials"] and form.get("client_secret") == ["cs"]:
                return self._send(200, {"access_token": self.state.token, "token_type": "Bearer", "expires_in": 1799})
            return self._send(401, {"error": "access_denied"})
        return self._send(404, {"error": {"message": "not found"}})

    def do_GET(self):
        parsed = urlparse(self.path)
        with self.state.lock:
            self.state.requests.append(("GET", parsed.path, dict(self.headers), parsed.query))
        if not self._authed():
            return self._send(401, {"error": {"message": "User Not Authenticated"}})
        if parsed.path == "/api/now/table/incident":
            q = parse_qs(parsed.query)
            if q.get("sysparm_query", [""])[0].startswith("number=INC9"):
                return self._send(200, {"result": []})
            return self._send(200, {"result": [RECORD]})
        if parsed.path == f"/api/now/table/incident/{SYS_ID}":
            return self._send(200, {"result": RECORD})
        if parsed.path.startswith("/api/now/table/incident/"):
            return self._send(404, {"error": {"message": "No Record found"}})
        return self._send(404, {"error": {"message": "not found"}})

    def do_PATCH(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode() if length else ""
        parsed = urlparse(self.path)
        with self.state.lock:
            self.state.requests.append(("PATCH", parsed.path, dict(self.headers), raw))
        if not self._authed():
            return self._send(401, {"error": {"message": "User Not Authenticated"}})
        if parsed.path == f"/api/now/table/incident/{SYS_ID}":
            body = json.loads(raw)
            if "work_notes" not in body:
                return self._send(400, {"error": {"message": "work_notes missing"}})
            if self.headers.get("X-Force-Status") == "403":
                return self._send(403, {"error": {"message": "Insufficient rights"}})
            return self._send(200, {"result": {"sys_id": SYS_ID, "number": "INC0010001", "sys_updated_on": "2026-10-08 21:30:00"}})
        return self._send(404, {"error": {"message": "No Record found"}})


@pytest.fixture(scope="module")
def mock_server():
    state = _State()
    _Handler.state = state
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield state, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
def client_factory(mock_server, monkeypatch):
    """ServiceNowClient pointed at the mock: the https/domain guard is bypassed ONLY here."""
    state, base = mock_server
    monkeypatch.setattr(sn, "normalize_instance_url", lambda raw: raw.rstrip("/"))
    sn._token_cache.clear()
    state.requests.clear()
    state.token_calls = 0

    def make(**kw):
        params = {"instance_url": base, "auth_type": "basic", "username": "aurora", "password": "pw"}
        params.update(kw)
        return sn.ServiceNowClient(**params), state

    return make


def test_basic_auth_connection_check_hits_the_incident_table(client_factory):
    client, state = client_factory()
    assert client.test_connection() == {"ok": True, "sample_count": 1}
    method, path, headers, query = state.requests[-1]
    assert (method, path) == ("GET", "/api/now/table/incident")
    assert "sysparm_limit=1" in query and headers["Authorization"].startswith("Basic ")


def test_wrong_password_is_a_401_error(client_factory):
    client, _ = client_factory(password="nope")
    with pytest.raises(sn.ServiceNowAPIError) as exc:
        client.test_connection()
    assert exc.value.status_code == 401


def test_list_and_get_parse_the_result_envelope(client_factory):
    client, state = client_factory()
    rows = client.list_incidents(query="active=true^ORDERBYDESCopened_at", limit=5)
    assert rows == [RECORD]
    _, _, _, query = state.requests[-1]
    assert "sysparm_display_value=true" in query and "sysparm_limit=5" in query
    assert client.get_incident(SYS_ID) == RECORD
    assert client.get_incident("INC0010001") == RECORD
    assert client.get_incident("INC9999999") is None
    assert client.get_incident("0" * 32) is None  # 404 → None, not an exception
    with pytest.raises(ValueError):
        client.get_incident("../etc")


def test_add_work_note_patches_the_record(client_factory):
    client, state = client_factory()
    result = client.add_work_note(SYS_ID, "Aurora RCA\nRoot cause: pool exhausted.")
    assert result["sys_updated_on"] == "2026-10-08 21:30:00"
    method, path, headers, raw = state.requests[-1]
    assert (method, path) == ("PATCH", f"/api/now/table/incident/{SYS_ID}")
    assert json.loads(raw) == {"work_notes": "Aurora RCA\nRoot cause: pool exhausted."}
    assert headers["Content-Type"] == "application/json"


def test_forbidden_work_note_surfaces_a_403(client_factory, monkeypatch):
    client, _ = client_factory()
    real_request = requests.request

    def with_force(method, url, **kw):
        kw.setdefault("headers", {})["X-Force-Status"] = "403"
        return real_request(method, url, **kw)

    monkeypatch.setattr(sn.requests, "request", with_force)
    with pytest.raises(sn.ServiceNowAPIError) as exc:
        client.add_work_note(SYS_ID, "note")
    assert exc.value.status_code == 403


def test_oauth_client_credentials_fetches_then_caches_the_token(client_factory):
    client, state = client_factory(auth_type="oauth_client_credentials", username=None, password=None,
                                   client_id="cid", client_secret="cs")
    assert client.test_connection()["ok"] is True
    assert client.list_incidents(limit=1) == [RECORD]
    assert state.token_calls == 1  # second call reused the cached token
    bearer_calls = [r for r in state.requests if r[0] == "GET" and r[2].get("Authorization") == "Bearer tok-123"]
    assert len(bearer_calls) == 2
    token_post = next(r for r in state.requests if r[1] == "/oauth_token.do")
    assert "grant_type=client_credentials" in token_post[3] and "client_id=cid" in token_post[3]


def test_oauth_bad_secret_is_rejected_at_token_time(client_factory):
    client, _ = client_factory(auth_type="oauth_client_credentials", username=None, password=None,
                               client_id="cid", client_secret="wrong")
    with pytest.raises(sn.ServiceNowAPIError) as exc:
        client.test_connection()
    assert exc.value.status_code == 401


def test_unreachable_host_is_a_clean_error(client_factory):
    client, _ = client_factory(instance_url="http://127.0.0.1:9")  # closed port
    client.timeout = 2
    with pytest.raises(sn.ServiceNowAPIError) as exc:
        client.test_connection()
    assert exc.value.status_code is None
