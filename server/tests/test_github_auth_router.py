"""Unit tests for :mod:`utils.auth.github_auth_router`.

The auth router is the SINGLE entry point any Aurora subsystem uses to
obtain a working GitHub credential. Its routing rules are
security-critical — a bug here can either silently downgrade auth
(falling through to OAuth when an App token would have been preferred)
or leak the wrong account's auth across users.

These tests pin the four routing rules:

1. App auth is preferred whenever a non-suspended installation is
   linked to ``(user_id, repo_full_name)``.
2. OAuth is the fallback when App auth is unavailable OR when minting
   raises :class:`GitHubAppInstallationSuspended` (the race-window case).
3. ``NoGitHubAuthError`` is raised when neither credential is available
   — never silently return ``None``.
4. ``make_auth_header`` produces the same ``token <value>`` shape for
   both methods (per GitHub REST API conventions; ``Bearer`` is reserved
   for the JWT-based App endpoints handled internally by
   :mod:`utils.auth.github_app_token`).

Also covers the Task 16 helper :func:`get_any_auth_for_user`, which
follows the same routing rules but ignores per-repo linkage.

All DB and HTTP boundaries are stubbed via :class:`pytest.MonkeyPatch`
to keep these tests fully isolated.
"""

from __future__ import annotations

from typing import Any

import pytest

from utils.auth import github_auth_router as router_module
from utils.auth.github_app_token import GitHubAppInstallationSuspended
from utils.auth.github_auth_router import (
    AuthResult,
    NoGitHubAuthError,
    get_any_auth_for_user,
    get_auth_for_user_repo,
    make_auth_header,
)


def _patch_repo_lookup(
    monkeypatch: pytest.MonkeyPatch,
    installation_id: int | None,
    has_active: bool | None,
) -> None:
    monkeypatch.setattr(
        router_module,
        "_lookup_repo_installation",
        lambda _user_id, _repo: (installation_id, has_active),
    )


def _patch_any_lookup(
    monkeypatch: pytest.MonkeyPatch,
    installation_id: int | None,
) -> None:
    monkeypatch.setattr(
        router_module,
        "_lookup_any_active_installation",
        lambda _user_id: installation_id,
    )


def _patch_oauth(
    monkeypatch: pytest.MonkeyPatch,
    token: str | None,
) -> None:
    monkeypatch.setattr(
        router_module,
        "_try_oauth",
        lambda _user_id: token,
    )


def _patch_mint(
    monkeypatch: pytest.MonkeyPatch,
    behavior: Any,
) -> None:
    """Patch ``get_installation_token`` either to return a token or raise.

    Pass a string for the happy path; pass an exception INSTANCE to make
    the mint call raise that exception (e.g. for the suspended-race
    fallback test).
    """

    if isinstance(behavior, BaseException):
        def _raise(_installation_id: int) -> str:
            raise behavior
        monkeypatch.setattr(
            router_module, "get_installation_token", _raise
        )
    else:
        monkeypatch.setattr(
            router_module,
            "get_installation_token",
            lambda _installation_id: behavior,
        )


def test_oauth_only_user_returns_oauth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No App link + OAuth credential present → method=oauth."""

    _patch_repo_lookup(monkeypatch, installation_id=None, has_active=None)
    _patch_oauth(monkeypatch, token="ghp_oauth_only_token")  # NOSONAR: stub
    _patch_mint(
        monkeypatch,
        AssertionError("App mint MUST NOT be called when no installation is linked"),
    )

    result = get_auth_for_user_repo("user-oauth-1", "owner/repo")

    assert isinstance(result, AuthResult)
    assert result.method == "oauth"
    assert result.token == "ghp_oauth_only_token"
    assert result.installation_id is None


def test_app_only_user_returns_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """App link present + no OAuth credential → method=app."""

    _patch_repo_lookup(monkeypatch, installation_id=4242, has_active=True)
    _patch_oauth(monkeypatch, token=None)
    _patch_mint(monkeypatch, "ghs_app_only_minted")

    result = get_auth_for_user_repo("user-app-1", "owner/repo")

    assert result.method == "app"
    assert result.token == "ghs_app_only_minted"
    assert result.installation_id == 4242


def test_user_with_both_prefers_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both creds available → App wins.

    OAuth MUST NOT be invoked at all when App is reachable; we plant a
    sentinel that would assert-fail if ``_try_oauth`` is consulted on
    the App-preferred path.
    """

    _patch_repo_lookup(monkeypatch, installation_id=7373, has_active=True)
    monkeypatch.setattr(
        router_module,
        "_try_oauth",
        lambda _user_id: pytest.fail(
            "OAuth MUST NOT be consulted when App auth is available"
        ),
    )
    _patch_mint(monkeypatch, "ghs_preferred_app")

    result = get_auth_for_user_repo("user-both-1", "owner/repo")

    assert result.method == "app"
    assert result.token == "ghs_preferred_app"
    assert result.installation_id == 7373


def test_suspended_app_falls_back_to_oauth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Suspended-at-mint race → OAuth fallback succeeds.

    The DB snapshot says the installation is active (``has_active=True``)
    so the router enters the App path, but the live GitHub call returns
    suspended. The router MUST catch the exception, log at INFO, and
    return the OAuth result instead of raising.
    """

    _patch_repo_lookup(monkeypatch, installation_id=8484, has_active=True)
    _patch_oauth(monkeypatch, token="ghp_oauth_fallback")  # NOSONAR: stub
    _patch_mint(
        monkeypatch,
        GitHubAppInstallationSuspended("installation_id=8484 suspended"),
    )

    result = get_auth_for_user_repo("user-fallback-1", "owner/repo")

    assert result.method == "oauth"
    assert result.token == "ghp_oauth_fallback"
    assert result.installation_id is None


def test_no_auth_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No App link AND no OAuth credential → NoGitHubAuthError."""

    _patch_repo_lookup(monkeypatch, installation_id=None, has_active=None)
    _patch_oauth(monkeypatch, token=None)
    _patch_mint(
        monkeypatch,
        AssertionError("Mint MUST NOT be called when no installation is linked"),
    )

    with pytest.raises(NoGitHubAuthError):
        get_auth_for_user_repo("user-empty-1", "owner/repo")


def test_make_auth_header_format() -> None:
    """Both methods produce ``Authorization: token <value>``.

    The ``Bearer`` prefix is reserved for the JWT-based App endpoints
    (``/app/installations/...``), which never reach this router. Per
    GitHub's REST API conventions installation tokens use the same
    ``token`` prefix as personal access tokens.
    """

    app_result = AuthResult(
        method="app", token="ghs_app_value", installation_id=1
    )
    oauth_result = AuthResult(
        method="oauth", token="ghp_oauth_value", installation_id=None
    )

    assert make_auth_header(app_result) == {
        "Authorization": "token ghs_app_value"
    }
    assert make_auth_header(oauth_result) == {
        "Authorization": "token ghp_oauth_value"
    }


def test_get_any_auth_for_user_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``get_any_auth_for_user`` returns App auth for any non-suspended link.

    Unlike :func:`get_auth_for_user_repo`, this helper does not require
    a specific repo — it picks the first linked, non-suspended
    installation. Used by agent tools that don't yet know which repo
    they will operate on.
    """

    _patch_any_lookup(monkeypatch, installation_id=160_002)
    _patch_oauth(
        monkeypatch,
        token=None,
    )
    _patch_mint(monkeypatch, "ghs_any_user_app_token")

    result = get_any_auth_for_user("user-any-1")

    assert result.method == "app"
    assert result.token == "ghs_any_user_app_token"
    assert result.installation_id == 160_002
