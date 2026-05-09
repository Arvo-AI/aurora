"""Tests that OAuth callback handlers never echo credential material in
their HTTP responses, audit events, or log output.

When a token exchange POST fails at the network layer, ``requests`` can
embed the outbound request body (which contains ``client_secret``) in the
exception text.  The callback route must catch that exception and return a
canned error, never forwarding the raw text.

Providers covered: Atlassian (Confluence/Jira) and Notion.
"""

import logging
import os
import sys
from unittest.mock import MagicMock

import pytest

_server_dir = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))


_CLIENT_SECRET_MARKER = "client_secret_DO_NOT_ECHO_XYZ"


def _exc_carrying_client_secret() -> Exception:
    return ValueError(
        f"HTTPError: POST https://api.atlassian.com/oauth/token — "
        f"request body contained client_secret={_CLIENT_SECRET_MARKER}"
    )


def _evict_route_modules(*prefixes: str) -> None:
    for mod in list(sys.modules):
        if any(mod == p or mod.startswith(p + ".") for p in prefixes):
            del sys.modules[mod]


def _install_audit_stub() -> MagicMock:
    stub = MagicMock(name="routes.audit_routes")
    stub.record_audit_event = MagicMock()
    sys.modules["routes.audit_routes"] = stub
    return stub


# ---------------------------------------------------------------------------
# Atlassian callback
# ---------------------------------------------------------------------------


_ATLASSIAN_HEAVY = (
    "connectors.atlassian_auth",
    "connectors.atlassian_auth.auth",
    "connectors.confluence_connector",
    "connectors.confluence_connector.client",
    "connectors.jira_connector",
    "connectors.jira_connector.client",
    "utils.web.limiter_ext",
    "config.rate_limiting",
)


class TestAtlassianOAuthCallbackRedaction:
    """The Atlassian connect route must not echo ``client_secret`` material
    when a token exchange fails due to a network-level exception.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        for pkg in _ATLASSIAN_HEAVY:
            sys.modules.setdefault(pkg, MagicMock())
        _install_audit_stub()
        limiter_stub = sys.modules["utils.web.limiter_ext"]
        limiter_stub.limiter = MagicMock()
        limiter_stub.limiter.limit = lambda *a, **kw: (lambda f: f)

    def _build_app(self, monkeypatch):
        _evict_route_modules("routes.atlassian")

        import utils.auth.rbac_decorators as rbac_mod
        import utils.auth.oauth2_state_cache as state_cache_mod
        import utils.auth.token_management as tok_mod
        import utils.db.connection_pool as pool_mod

        monkeypatch.setattr(
            rbac_mod, "get_user_id_from_request", MagicMock(return_value="u-1")
        )
        monkeypatch.setattr(
            rbac_mod, "get_org_id_from_request", MagicMock(return_value="org-1")
        )
        monkeypatch.setattr(
            rbac_mod, "enforce_with_reload", MagicMock(return_value=True)
        )
        monkeypatch.setattr(rbac_mod, "_audit_auth_failure", MagicMock())
        monkeypatch.setattr(
            state_cache_mod,
            "retrieve_oauth2_state",
            MagicMock(return_value={"user_id": "u-1", "endpoint": "atlassian:confluence"}),
        )
        sys.modules["connectors.atlassian_auth.auth"].exchange_code_for_token = (
            MagicMock(side_effect=_exc_carrying_client_secret())
        )
        monkeypatch.setattr(tok_mod, "store_tokens_in_db", MagicMock())
        monkeypatch.setattr(pool_mod, "db_pool", MagicMock())

        from flask import Flask
        from routes.atlassian.atlassian_routes import atlassian_bp

        app = Flask(__name__)
        app.register_blueprint(atlassian_bp, url_prefix="/atlassian")
        return app

    def test_exchange_failure_response_does_not_contain_secret(
        self, monkeypatch, caplog
    ):
        app = self._build_app(monkeypatch)

        with app.test_client() as client:
            with caplog.at_level(logging.DEBUG):
                resp = client.post(
                    "/atlassian/connect",
                    json={
                        "authType": "oauth",
                        "code": "auth-code-xyz",
                        "state": "state-token-abc",
                        "products": ["confluence"],
                    },
                )

        assert _CLIENT_SECRET_MARKER not in resp.get_data(as_text=True)
        for record in caplog.records:
            assert _CLIENT_SECRET_MARKER not in record.getMessage(), (
                f"client_secret found in log record: {record.getMessage()!r}"
            )

    def test_exchange_failure_returns_generic_error(self, monkeypatch):
        app = self._build_app(monkeypatch)

        with app.test_client() as client:
            resp = client.post(
                "/atlassian/connect",
                json={
                    "authType": "oauth",
                    "code": "auth-code-xyz",
                    "state": "state-token-abc",
                    "products": ["confluence"],
                },
            )

        body = resp.get_json()
        assert resp.status_code in (400, 500, 502)
        assert "error" in body
        assert _CLIENT_SECRET_MARKER not in body["error"]


# ---------------------------------------------------------------------------
# Notion callback
# ---------------------------------------------------------------------------


_NOTION_HEAVY = (
    "connectors.notion_connector",
    "connectors.notion_connector.auth",
    "connectors.notion_connector.client",
    "utils.web.limiter_ext",
    "config.rate_limiting",
)


class TestNotionOAuthCallbackRedaction:
    """The Notion connect route must not echo ``client_secret`` material
    when a token exchange fails due to a network-level exception.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        for pkg in _NOTION_HEAVY:
            sys.modules.setdefault(pkg, MagicMock())
        _install_audit_stub()
        limiter_stub = sys.modules["utils.web.limiter_ext"]
        limiter_stub.limiter = MagicMock()
        limiter_stub.limiter.limit = lambda *a, **kw: (lambda f: f)

    def _build_app(self, monkeypatch):
        _evict_route_modules("routes.notion")

        import utils.auth.rbac_decorators as rbac_mod
        import utils.auth.oauth2_state_cache as state_cache_mod
        import utils.auth.token_management as tok_mod
        import utils.db.connection_pool as pool_mod

        monkeypatch.setattr(
            rbac_mod, "get_user_id_from_request", MagicMock(return_value="u-1")
        )
        monkeypatch.setattr(
            rbac_mod, "get_org_id_from_request", MagicMock(return_value="org-1")
        )
        monkeypatch.setattr(
            rbac_mod, "enforce_with_reload", MagicMock(return_value=True)
        )
        monkeypatch.setattr(rbac_mod, "_audit_auth_failure", MagicMock())
        monkeypatch.setattr(
            state_cache_mod,
            "retrieve_oauth2_state",
            MagicMock(return_value={"user_id": "u-1", "endpoint": "notion"}),
        )
        sys.modules["connectors.notion_connector.auth"].exchange_code_for_token = (
            MagicMock(side_effect=_exc_carrying_client_secret())
        )
        monkeypatch.setattr(tok_mod, "store_tokens_in_db", MagicMock())
        monkeypatch.setattr(pool_mod, "db_pool", MagicMock())

        from flask import Flask
        from routes.notion.notion_routes import notion_bp

        app = Flask(__name__)
        app.register_blueprint(notion_bp, url_prefix="/notion")
        return app

    def test_exchange_failure_response_does_not_contain_secret(
        self, monkeypatch, caplog
    ):
        app = self._build_app(monkeypatch)

        with app.test_client() as client:
            with caplog.at_level(logging.DEBUG):
                resp = client.post(
                    "/notion/oauth/callback",
                    json={"code": "auth-code-xyz", "state": "state-token-abc"},
                )

        assert _CLIENT_SECRET_MARKER not in resp.get_data(as_text=True)
        for record in caplog.records:
            assert _CLIENT_SECRET_MARKER not in record.getMessage(), (
                f"client_secret found in log record: {record.getMessage()!r}"
            )

    def test_exchange_failure_returns_generic_error(self, monkeypatch):
        app = self._build_app(monkeypatch)

        with app.test_client() as client:
            resp = client.post(
                "/notion/oauth/callback",
                json={"code": "auth-code-xyz", "state": "state-token-abc"},
            )

        body = resp.get_json()
        assert resp.status_code in (400, 500, 502)
        assert "error" in body
        assert _CLIENT_SECRET_MARKER not in body["error"]
