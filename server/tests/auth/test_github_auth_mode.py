"""Tests for GitHub auth-mode resolution (utils.auth.github_auth_mode).

The crux is the deprecation split: NEW OAuth connections (login/CTA) are
gated separately from honouring EXISTING OAuth tokens, so deprecating OAuth
onboarding never orphans users who connected via OAuth before the App.
"""

import importlib

from utils.auth import github_auth_mode as m


def _set_mode(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("GITHUB_AUTH_MODE", raising=False)
    else:
        monkeypatch.setenv("GITHUB_AUTH_MODE", value)


class TestAuthModeResolution:
    def test_defaults_to_app(self, monkeypatch):
        _set_mode(monkeypatch, None)
        assert m.get_auth_mode() == "app"

    def test_invalid_value_falls_back_to_app(self, monkeypatch):
        _set_mode(monkeypatch, "nonsense")
        assert m.get_auth_mode() == "app"

    def test_case_and_whitespace_insensitive(self, monkeypatch):
        _set_mode(monkeypatch, "  HyBriD  ")
        assert m.get_auth_mode() == "hybrid"


class TestOAuthLoginVsTokenHonouring:
    """The deprecation split — the whole point of separating these two."""

    def test_app_mode_hides_login_but_honours_existing_tokens(self, monkeypatch):
        _set_mode(monkeypatch, "app")
        # New OAuth onboarding is OFF (CTA hidden, /github/login 404s)...
        assert m.is_oauth_login_enabled() is False
        # ...but existing OAuth tokens keep working (no orphaning).
        assert m.is_oauth_token_honored() is True
        assert m.is_app_enabled() is True

    def test_oauth_mode_enables_both(self, monkeypatch):
        _set_mode(monkeypatch, "oauth")
        assert m.is_oauth_login_enabled() is True
        assert m.is_oauth_token_honored() is True

    def test_hybrid_mode_enables_both(self, monkeypatch):
        _set_mode(monkeypatch, "hybrid")
        assert m.is_oauth_login_enabled() is True
        assert m.is_oauth_token_honored() is True
        assert m.is_app_enabled() is True

    def test_existing_tokens_honoured_in_every_valid_mode(self, monkeypatch):
        # Invariant: deprecating OAuth must never drop an existing connection,
        # so token honouring is True regardless of mode (including the default).
        for mode in ("app", "oauth", "hybrid", "typo-defaults-to-app"):
            _set_mode(monkeypatch, mode)
            assert m.is_oauth_token_honored() is True

    def test_login_only_enabled_for_explicit_oauth_modes(self, monkeypatch):
        for mode, expected in (("app", False), ("oauth", True), ("hybrid", True)):
            _set_mode(monkeypatch, mode)
            assert m.is_oauth_login_enabled() is expected


def test_module_reimports_clean():
    # Guard against an import-time regression in the module itself.
    importlib.reload(m)
