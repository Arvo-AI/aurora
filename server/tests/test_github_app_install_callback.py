"""Unit tests for the GitHub App install callback (anti-spoofing path).

The callback is the most security-critical surface in the GitHub App
migration: GitHub redirects the user back to Aurora with
``?installation_id=<int>&state=<user_id>`` query params, both of which
arrive over an untrusted channel (the user's browser). The route MUST:

1. Verify ``installation_id`` against the GitHub API (a 404 means the
   installation does not exist for our app — definitive spoof).
2. Verify ``state`` resolves to a known Aurora user (rejects forged
   user-id substitution).
3. Render the error template with HARD-CODED constant strings only
   (no XSS via reflected query params).
4. UPSERT the installation row + INSERT the user->installation join row
   atomically (idempotent on repeat installs).

Mocking strategy keeps these tests fully isolated and fast: ``responses``
intercepts the GitHub API, ``mint_app_jwt`` is stubbed to a fixed
string, ``validate_user_exists`` is stubbed per-test, and
``db_pool.get_admin_connection`` yields a ``MagicMock`` cursor that
captures executed SQL + parameters without a Postgres round-trip.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock

import flask
import pytest
import werkzeug

from routes.github.github_app import github_app_bp

if not hasattr(werkzeug, "__version__"):
    werkzeug.__version__ = "test"  # type: ignore[attr-defined]

_TEMPLATE_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), "..", "connectors", "github_templates"
    )
)


@pytest.fixture(scope="function")
def flask_app() -> flask.Flask:
    app = flask.Flask(__name__, template_folder=_TEMPLATE_DIR)
    app.register_blueprint(github_app_bp, url_prefix="/github")
    app.config["TESTING"] = True
    app.config["GITHUB_APP_ENABLED"] = True
    return app


@pytest.fixture(scope="function")
def client(flask_app: flask.Flask) -> Any:
    return flask_app.test_client()


def _gh_api_url(installation_id: int) -> str:
    return f"https://api.github.com/app/installations/{installation_id}"


def _gh_api_payload(
    installation_id: int = 9_900_001,
    account_login: str = "test-org",
    account_id: int = 5_500_001,
    account_type: str = "Organization",
    suspended_at: str | None = None,
) -> dict[str, Any]:
    return {
        "id": installation_id,
        "account": {
            "login": account_login,
            "id": account_id,
            "type": account_type,
        },
        "target_type": account_type,
        "permissions": {
            "contents": "read",
            "metadata": "read",
            "issues": "read",
            "pull_requests": "read",
        },
        "events": [
            "installation",
            "installation_repositories",
            "pull_request",
            "issues",
        ],
        "repository_selection": "selected",
        "suspended_at": suspended_at,
    }


@contextmanager
def _fake_admin_connection(cursor: MagicMock):
    conn = MagicMock(name="connection")

    @contextmanager
    def _cursor_cm():
        yield cursor

    conn.cursor = _cursor_cm
    conn.commit = MagicMock(name="commit")
    yield conn


@pytest.fixture(scope="function")
def patched_db(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    cursor = MagicMock(name="cursor")
    cursor.rowcount = 1

    @contextmanager
    def _conn_cm():
        with _fake_admin_connection(cursor) as conn:
            yield conn

    from routes.github import github_app as route_module

    monkeypatch.setattr(route_module.db_pool, "get_admin_connection", _conn_cm)
    return cursor


@pytest.fixture(scope="function")
def patched_jwt(monkeypatch: pytest.MonkeyPatch) -> str:
    token = "eyJTEST.JWT.PAYLOAD"
    from routes.github import github_app as route_module

    monkeypatch.setattr(route_module, "mint_app_jwt", lambda: token)
    return token


@pytest.fixture(scope="function")
def patched_user_exists(monkeypatch: pytest.MonkeyPatch):
    state = {"value": True}

    def _set(value: bool) -> None:
        state["value"] = value

    from routes.github import github_app as route_module

    monkeypatch.setattr(
        route_module,
        "validate_user_exists",
        lambda _user_id: state["value"],
    )
    return _set


def test_callback_verifies_installation_id_with_api(
    client: Any,
    responses_mock: Any,
    patched_db: MagicMock,
    patched_jwt: str,
    patched_user_exists: Any,
) -> None:
    """Anti-spoof invariant #1: route MUST call GitHub with the App JWT.

    The route cannot trust the ``installation_id`` query param — it
    must independently verify the installation exists for our App.
    """

    patched_user_exists(True)
    payload = _gh_api_payload()
    responses_mock.get(
        _gh_api_url(payload["id"]),
        json=payload,
        status=200,
    )

    response = client.get(
        "/github/app/install/callback",
        query_string={
            "installation_id": str(payload["id"]),
            "state": "user-abc",
            "setup_action": "install",
        },
    )

    assert response.status_code == 200
    assert len(responses_mock.calls) == 1
    call = responses_mock.calls[0]
    assert call.request.url == _gh_api_url(payload["id"])
    assert call.request.headers["Authorization"] == f"Bearer {patched_jwt}"
    assert (
        call.request.headers["Accept"] == "application/vnd.github+json"
    )


def test_callback_rejects_unknown_state_user(
    client: Any,
    responses_mock: Any,
    patched_db: MagicMock,
    patched_jwt: str,
    patched_user_exists: Any,
) -> None:
    """Anti-spoof invariant #2: unknown ``state`` MUST short-circuit
    before any DB write or GitHub API call.

    State validation is the cheaper of the two checks; running it first
    limits the rate-limit blast radius of forged-state attacks.
    """

    patched_user_exists(False)

    response = client.get(
        "/github/app/install/callback",
        query_string={
            "installation_id": "9900001",
            "state": "ghost-user",
            "setup_action": "install",
        },
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "User identity could not be verified" in body
    assert len(responses_mock.calls) == 0
    patched_db.execute.assert_not_called()


def test_callback_rejects_404_installation(
    client: Any,
    responses_mock: Any,
    patched_db: MagicMock,
    patched_jwt: str,
    patched_user_exists: Any,
) -> None:
    """A GitHub 404 for the installation_id is definitive proof of a spoof.

    Insert ZERO rows even though state was valid and the JWT minted
    cleanly.
    """

    patched_user_exists(True)
    responses_mock.get(
        _gh_api_url(7777777),
        json={"message": "Not Found"},
        status=404,
    )

    response = client.get(
        "/github/app/install/callback",
        query_string={
            "installation_id": "7777777",
            "state": "user-abc",
            "setup_action": "install",
        },
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "GitHub installation could not be verified" in body
    assert len(responses_mock.calls) == 1
    patched_db.execute.assert_not_called()


def test_callback_persists_installation_metadata(
    client: Any,
    responses_mock: Any,
    patched_db: MagicMock,
    patched_jwt: str,
    patched_user_exists: Any,
) -> None:
    """Happy path persists every metadata field returned by GitHub."""

    patched_user_exists(True)
    payload = _gh_api_payload(
        installation_id=9_900_002,
        account_login="acme-org",
        account_id=4_242_424,
        account_type="Organization",
    )
    responses_mock.get(_gh_api_url(payload["id"]), json=payload, status=200)

    response = client.get(
        "/github/app/install/callback",
        query_string={
            "installation_id": str(payload["id"]),
            "state": "user-meta-1",
            "setup_action": "install",
        },
    )
    assert response.status_code == 200

    assert patched_db.execute.call_count == 2

    upsert_sql, upsert_params = patched_db.execute.call_args_list[0].args
    assert "INSERT INTO github_installations" in upsert_sql
    assert "ON CONFLICT (installation_id) DO UPDATE" in upsert_sql

    (
        installation_id,
        account_login,
        account_id,
        account_type,
        target_type,
        permissions_json,
        events_json,
        repository_selection,
        suspended_at,
    ) = upsert_params
    assert installation_id == payload["id"]
    assert account_login == "acme-org"
    assert account_id == 4_242_424
    assert account_type == "Organization"
    assert target_type == "Organization"
    assert json.loads(permissions_json) == payload["permissions"]
    assert json.loads(events_json) == payload["events"]
    assert repository_selection == "selected"
    assert suspended_at is None


def test_callback_persists_user_link(
    client: Any,
    responses_mock: Any,
    patched_db: MagicMock,
    patched_jwt: str,
    patched_user_exists: Any,
) -> None:
    """Happy path also inserts the user->installation join row."""

    patched_user_exists(True)
    payload = _gh_api_payload(installation_id=9_900_003)
    responses_mock.get(_gh_api_url(payload["id"]), json=payload, status=200)

    response = client.get(
        "/github/app/install/callback",
        query_string={
            "installation_id": str(payload["id"]),
            "state": "user-link-target",
            "setup_action": "install",
        },
    )
    assert response.status_code == 200
    assert patched_db.execute.call_count == 2

    join_sql, join_params = patched_db.execute.call_args_list[1].args
    assert "INSERT INTO user_github_installations" in join_sql
    assert (
        "ON CONFLICT (user_id, installation_id) DO NOTHING" in join_sql
    )
    user_id, installation_id = join_params
    assert user_id == "user-link-target"
    assert installation_id == payload["id"]


def test_callback_idempotent_on_repeat_install(
    client: Any,
    responses_mock: Any,
    patched_db: MagicMock,
    patched_jwt: str,
    patched_user_exists: Any,
) -> None:
    """Two callbacks with the same installation_id MUST both succeed AND
    use ``ON CONFLICT`` semantics on every write.

    Idempotency at the DB layer is enforced by the schema's UNIQUE
    constraints; this test pins the SQL contract that exercises it. If
    a future refactor swapped ``ON CONFLICT`` for a naive INSERT, the
    second install would 500 in production — and this test would fail.
    """

    patched_user_exists(True)
    payload = _gh_api_payload(installation_id=9_900_004)
    responses_mock.get(_gh_api_url(payload["id"]), json=payload, status=200)
    responses_mock.get(_gh_api_url(payload["id"]), json=payload, status=200)

    qs = {
        "installation_id": str(payload["id"]),
        "state": "user-repeat",
        "setup_action": "install",
    }
    first = client.get("/github/app/install/callback", query_string=qs)
    second = client.get("/github/app/install/callback", query_string=qs)

    assert first.status_code == 200
    assert second.status_code == 200

    assert patched_db.execute.call_count == 4
    for call in patched_db.execute.call_args_list:
        sql = call.args[0]
        assert "ON CONFLICT" in sql, (
            "Every install write MUST use ON CONFLICT for idempotency"
        )


def test_callback_xss_safe(
    client: Any,
    responses_mock: Any,
    patched_db: MagicMock,
    patched_jwt: str,
    patched_user_exists: Any,
) -> None:
    """Malicious query params MUST NEVER appear in the rendered HTML.

    The route's defence-in-depth: every error path renders one of a
    fixed set of HARD-CODED constant strings. Even though the error
    template does ``{{ error }}`` substitution (Jinja autoescape covers
    the HTML context), the route never feeds the raw query value into
    that variable. We assert the payload appears NOWHERE in the
    response so a future regression that echoes the raw value fails
    this test.
    """

    patched_user_exists(True)
    malicious = "<script>alert('xss')</script>"

    response = client.get(
        "/github/app/install/callback",
        query_string={
            "installation_id": malicious,
            "state": "user-xss",
            "setup_action": "install",
        },
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "GitHub installation could not be verified" in body
    assert "alert(" not in body
    assert "alert(&#39;xss&#39;)" not in body
    assert "alert(\\'xss\\')" not in body
    assert "xss" not in body
    assert "&lt;script&gt;" not in body
    assert len(responses_mock.calls) == 0
    patched_db.execute.assert_not_called()
