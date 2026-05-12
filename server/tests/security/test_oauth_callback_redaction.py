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


# ---------------------------------------------------------------------------
# Atlassian callback
# ---------------------------------------------------------------------------


class TestAtlassianOAuthCallbackRedaction:
    """The Atlassian connect route must not echo ``client_secret`` material
    when a token exchange fails due to a network-level exception.
    """

    @pytest.fixture(autouse=True)
    def _inject_failure(self, atlassian_oauth_app):
        sys.modules["connectors.atlassian_auth.auth"].exchange_code_for_token = (
            MagicMock(side_effect=_exc_carrying_client_secret())
        )
        self.app = atlassian_oauth_app

    def test_exchange_failure_response_does_not_contain_secret(self, caplog):
        with self.app.test_client() as client:
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
        assert _CLIENT_SECRET_MARKER not in caplog.text
        for record in caplog.records:
            assert _CLIENT_SECRET_MARKER not in record.getMessage(), (
                f"client_secret found in log record: {record.getMessage()!r}"
            )

    def test_exchange_failure_returns_generic_error(self):
        with self.app.test_client() as client:
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


class TestNotionOAuthCallbackRedaction:
    """The Notion connect route must not echo ``client_secret`` material
    when a token exchange fails due to a network-level exception.
    """

    @pytest.fixture(autouse=True)
    def _inject_failure(self, notion_oauth_app):
        sys.modules["connectors.notion_connector.auth"].exchange_code_for_token = (
            MagicMock(side_effect=_exc_carrying_client_secret())
        )
        self.app = notion_oauth_app

    def test_exchange_failure_response_does_not_contain_secret(self, caplog):
        with self.app.test_client() as client:
            with caplog.at_level(logging.DEBUG):
                resp = client.post(
                    "/notion/oauth/callback",
                    json={"code": "auth-code-xyz", "state": "state-token-abc"},
                )

        assert _CLIENT_SECRET_MARKER not in resp.get_data(as_text=True)
        assert _CLIENT_SECRET_MARKER not in caplog.text
        for record in caplog.records:
            assert _CLIENT_SECRET_MARKER not in record.getMessage(), (
                f"client_secret found in log record: {record.getMessage()!r}"
            )

    def test_exchange_failure_returns_generic_error(self):
        with self.app.test_client() as client:
            resp = client.post(
                "/notion/oauth/callback",
                json={"code": "auth-code-xyz", "state": "state-token-abc"},
            )

        body = resp.get_json()
        assert resp.status_code in (400, 500, 502)
        assert "error" in body
        assert _CLIENT_SECRET_MARKER not in body["error"]
