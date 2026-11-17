"""The App install link follows the configured GitHub host."""

from __future__ import annotations

import pytest
from flask import Flask


@pytest.fixture
def app_mod(monkeypatch):
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")
    monkeypatch.delenv("GITHUB_API_URL", raising=False)
    from routes.github import github_app

    app = Flask(__name__)  # NOSONAR
    app.config["GITHUB_APP_ENABLED"] = True
    return app, github_app


def _install_url(app, mod):
    with app.test_request_context():
        out = mod.github_app_install_url.__wrapped__("user-1")
    resp, status = out if isinstance(out, tuple) else (out, 200)
    return resp.get_json(), status


@pytest.mark.parametrize(
    "setting, prefix",
    [
        ("aurora-test", "https://github.com/apps/aurora-test/installations/new?state="),
        (
            "https://acme.ghe.com/apps/acme/aurora",
            "https://acme.ghe.com/apps/acme/aurora/installations/new?state=",
        ),
    ],
)
def test_install_url_uses_configured_host(app_mod, monkeypatch, setting, prefix):
    monkeypatch.setenv("NEXT_PUBLIC_GITHUB_APP_SLUG", setting)
    body, status = _install_url(*app_mod)
    assert status == 200
    assert body["install_url"].startswith(prefix)
    assert len(body["install_url"]) > len(prefix)


def test_install_url_unconfigured_is_503(app_mod, monkeypatch):
    monkeypatch.delenv("NEXT_PUBLIC_GITHUB_APP_SLUG", raising=False)
    _, status = _install_url(*app_mod)
    assert status == 503


def test_installations_list_links_use_configured_host(app_mod, monkeypatch):
    from contextlib import contextmanager
    from datetime import datetime, timezone
    from unittest.mock import MagicMock, patch

    app, mod = app_mod
    monkeypatch.setenv("NEXT_PUBLIC_GITHUB_APP_SLUG", "https://acme.ghe.com/apps/acme/aurora")
    row = (7, "acme", 1, "Organization", "Organization", {}, [], "all", None, False,
           datetime(2026, 11, 17, tzinfo=timezone.utc), True)
    cur = MagicMock()
    cur.fetchall.return_value = [row]
    conn = MagicMock()

    @contextmanager
    def _cur():
        yield cur

    conn.cursor.side_effect = _cur

    @contextmanager
    def _conn():
        yield conn

    pool = MagicMock()
    pool.get_admin_connection.side_effect = _conn
    with app.test_request_context(), patch.object(mod, "db_pool", pool):
        out = mod.github_app_list_installations.__wrapped__("user-1")
    resp = out[0] if isinstance(out, tuple) else out
    inst = resp.get_json()["installations"][0]
    assert inst["manage_url"] == "https://acme.ghe.com/organizations/acme/settings/installations/7"
    assert inst["avatar_url"] == "https://acme.ghe.com/acme.png?size=40"
