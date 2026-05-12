"""Shared helpers for security tests."""

import os
import sys
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Helpers shared by non-Flask tests
# ---------------------------------------------------------------------------

import re

from utils.security.signature_match import check_signature
from utils.auth.command_policy import _UNIVERSAL_DENY_RULES


def sig_blocks(cmd: str) -> bool:
    """Return True if the signature matcher catches *cmd*."""
    return check_signature(cmd).matched


def deny_blocks(cmd: str) -> bool:
    """Return True if any universal deny rule matches *cmd*."""
    return any(re.search(raw["pattern"], cmd) for raw in _UNIVERSAL_DENY_RULES)


def any_layer_blocks(cmd: str) -> bool:
    """Return True if either the signature matcher or denylist catches *cmd*."""
    return sig_blocks(cmd) or deny_blocks(cmd)


# ---------------------------------------------------------------------------
# OAuth callback test apps
#
# Flask app creation lives here — not in the test file — so SonarCloud's
# S4502 CSRF detector sees the instantiation in one controlled place.
# Aurora has no Flask-side CSRF middleware: CSRF for OAuth flows is handled
# by single-use, Redis-backed state tokens (utils.auth.oauth2_state_cache).
# The additional trust boundary (Origin / Referer enforcement) is in the
# Next.js proxy layer, upstream of Flask entirely.
# ---------------------------------------------------------------------------

os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")


def _make_oauth_test_app(blueprint, url_prefix: str):
    """Return a minimal Flask test app with *blueprint* registered.

    S4502 — why CSRFProtect is intentionally absent here:
    Aurora's Flask layer is a pure JSON REST API; it never serves HTML forms
    and has no flask-wtf / CSRFProtect dependency in production.  CSRF
    protection for OAuth flows is implemented at the application layer via
    single-use, Redis-backed, TTL-expiring state tokens
    (utils.auth.oauth2_state_cache).  Additional Origin/Referer enforcement
    lives in the Next.js proxy, upstream of Flask entirely.
    Adding CSRFProtect to a throwaway test app would give a false sense of
    security and is not consistent with the production architecture.
    """
    from flask import Flask as _Flask
    application = _Flask(__name__)  # NOSONAR
    application.config["TESTING"] = True
    application.register_blueprint(blueprint, url_prefix=url_prefix)
    return application


_LIMITER_EXT = "utils.web.limiter_ext"

_ATLASSIAN_HEAVY = (
    "connectors.atlassian_auth",
    "connectors.atlassian_auth.auth",
    "connectors.confluence_connector",
    "connectors.confluence_connector.client",
    "connectors.jira_connector",
    "connectors.jira_connector.client",
    _LIMITER_EXT,
    "config.rate_limiting",
)

_NOTION_HEAVY = (
    "connectors.notion_connector",
    "connectors.notion_connector.auth",
    "connectors.notion_connector.client",
    _LIMITER_EXT,
    "config.rate_limiting",
)


def _stub_heavy_packages(packages: tuple) -> None:
    for pkg in packages:
        sys.modules.setdefault(pkg, MagicMock())
    stub = MagicMock(name="routes.audit_routes")
    stub.record_audit_event = MagicMock()
    sys.modules["routes.audit_routes"] = stub
    limiter_stub = sys.modules[_LIMITER_EXT]
    limiter_stub.limiter = MagicMock()
    limiter_stub.limiter.limit = lambda *a, **kw: (lambda f: f)


def _patch_rbac_deps(monkeypatch, oauth_state_endpoint: str) -> None:
    """Monkeypatch RBAC, state-cache, token-management, and DB pool deps."""
    import utils.auth.rbac_decorators as rbac_mod
    import utils.auth.oauth2_state_cache as state_cache_mod
    import utils.auth.token_management as tok_mod
    import utils.db.connection_pool as pool_mod

    monkeypatch.setattr(rbac_mod, "get_user_id_from_request", MagicMock(return_value="u-1"))
    monkeypatch.setattr(rbac_mod, "get_org_id_from_request", MagicMock(return_value="org-1"))
    monkeypatch.setattr(rbac_mod, "enforce_with_reload", MagicMock(return_value=True))
    monkeypatch.setattr(rbac_mod, "_audit_auth_failure", MagicMock())
    monkeypatch.setattr(
        state_cache_mod,
        "retrieve_oauth2_state",
        MagicMock(return_value={"user_id": "u-1", "endpoint": oauth_state_endpoint}),
    )
    monkeypatch.setattr(tok_mod, "store_tokens_in_db", MagicMock())
    monkeypatch.setattr(pool_mod, "db_pool", MagicMock())


@pytest.fixture()
def atlassian_oauth_app(monkeypatch):
    """Flask test app with the Atlassian blueprint, heavy deps stubbed."""
    _stub_heavy_packages(_ATLASSIAN_HEAVY)
    _patch_rbac_deps(monkeypatch, "atlassian:confluence")

    to_delete = [m for m in sys.modules if m == "routes.atlassian" or m.startswith("routes.atlassian.")]
    for mod in to_delete:
        del sys.modules[mod]

    from routes.atlassian.atlassian_routes import atlassian_bp
    return _make_oauth_test_app(atlassian_bp, "/atlassian")


@pytest.fixture()
def notion_oauth_app(monkeypatch):
    """Flask test app with the Notion blueprint, heavy deps stubbed."""
    _stub_heavy_packages(_NOTION_HEAVY)
    _patch_rbac_deps(monkeypatch, "notion")

    to_delete = [m for m in sys.modules if m == "routes.notion" or m.startswith("routes.notion.")]
    for mod in to_delete:
        del sys.modules[mod]

    from routes.notion.notion_routes import notion_bp
    return _make_oauth_test_app(notion_bp, "/notion")
