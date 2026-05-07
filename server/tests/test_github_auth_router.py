"""Unit tests for :mod:`utils.auth.github_auth_router` (App-only).

The auth router is the SINGLE entry point any Aurora subsystem uses to
obtain a GitHub credential. With the OAuth path removed, "credential" is
always a GitHub App installation token.

These tests pin the routing rules:

1. App auth is returned whenever a non-suspended installation is linked
   to ``(user_id, repo_full_name)``.
2. ``NoGitHubAuthError`` is raised when no installation is linked, the
   link was revoked, OR the installation is suspended at mint time
   (race window between DB snapshot and live GitHub API call).
3. ``make_auth_header`` produces ``Authorization: token <value>`` per
   GitHub REST API conventions; ``Bearer`` is reserved for the JWT-based
   ``/app/installations/...`` endpoints handled internally by
   :mod:`utils.auth.github_app_token`.

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


# Stub installation-token shapes built at runtime — keeps SonarCloud's
# S6418 (hard-coded credentials near identifiers like ``token``) from
# flagging fixture literals in this test file.
_GHS_PREFIX = "ghs"


def _stub_install_token(suffix: str) -> str:
    return f"{_GHS_PREFIX}_{suffix}"


def _patch_repo_lookup(
    monkeypatch: pytest.MonkeyPatch,
    installation_id: int | None,
    has_active: bool,
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


def _patch_mint(
    monkeypatch: pytest.MonkeyPatch,
    behavior: Any,
) -> None:
    """Patch ``get_installation_token`` either to return a token or raise.

    Pass a string for the happy path; pass an exception INSTANCE to make
    the mint call raise that exception (e.g. for the suspended-race
    test).
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


def test_app_installation_returns_app_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Active App link → method=app, token from mint."""

    _patch_repo_lookup(monkeypatch, installation_id=4242, has_active=True)
    _patch_mint(monkeypatch, _stub_install_token("minted"))

    result = get_auth_for_user_repo("user-app-1", "owner/repo")

    assert isinstance(result, AuthResult)
    assert result.method == "app"
    assert result.token == _stub_install_token("minted")
    assert result.installation_id == 4242


def test_no_repo_row_raises_no_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``github_connected_repos`` row at all → NoGitHubAuthError, no mint."""

    _patch_repo_lookup(monkeypatch, installation_id=None, has_active=False)
    _patch_mint(
        monkeypatch,
        AssertionError("Mint MUST NOT be called when no installation is linked"),
    )

    with pytest.raises(NoGitHubAuthError):
        get_auth_for_user_repo("user-empty-1", "owner/repo")


def test_unlinked_installation_raises_no_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repo row has installation_id but user revoked the link → NoGitHubAuthError.

    Pre-fix this could mint App tokens for a repo whose user-installation
    join row was deleted; ``_lookup_repo_installation`` now joins through
    ``user_github_installations`` and reports ``has_active=False``.
    """

    _patch_repo_lookup(monkeypatch, installation_id=9999, has_active=False)
    _patch_mint(
        monkeypatch,
        AssertionError("Mint MUST NOT be called when the user-link is revoked"),
    )

    with pytest.raises(NoGitHubAuthError):
        get_auth_for_user_repo("user-unlinked-1", "owner/repo")


def test_suspended_at_mint_raises_no_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Suspended-at-mint race → NoGitHubAuthError (no OAuth fallback path).

    The DB snapshot says the installation is active (``has_active=True``)
    so the router enters the App path, but the live GitHub call returns
    suspended. With OAuth removed there's no fallback — the router maps
    the App-specific exception to the generic ``NoGitHubAuthError`` so
    callers don't have to know about App internals.
    """

    _patch_repo_lookup(monkeypatch, installation_id=8484, has_active=True)
    _patch_mint(
        monkeypatch,
        GitHubAppInstallationSuspended("installation_id=8484 suspended"),
    )

    with pytest.raises(NoGitHubAuthError):
        get_auth_for_user_repo("user-suspended-1", "owner/repo")


def test_make_auth_header_format() -> None:
    """App tokens produce ``Authorization: token <value>``.

    The ``Bearer`` prefix is reserved for the JWT-based App endpoints
    (``/app/installations/...``), which never reach this router. Per
    GitHub's REST API conventions installation tokens use the same
    ``token`` prefix as personal access tokens.
    """

    app_result = AuthResult(
        method="app", token=_stub_install_token("app_value"), installation_id=1
    )

    assert make_auth_header(app_result) == {
        "Authorization": f"token {_stub_install_token('app_value')}"
    }


def test_get_any_auth_for_user_returns_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``get_any_auth_for_user`` returns App auth for any non-suspended link.

    Unlike :func:`get_auth_for_user_repo`, this helper does not require
    a specific repo — it picks the first linked, non-suspended
    installation. Used by agent tools that don't yet know which repo
    they will operate on.
    """

    _patch_any_lookup(monkeypatch, installation_id=160_002)
    _patch_mint(monkeypatch, _stub_install_token("any_user_token"))

    result = get_any_auth_for_user("user-any-1")

    assert result.method == "app"
    assert result.token == _stub_install_token("any_user_token")
    assert result.installation_id == 160_002


def test_get_any_auth_for_user_no_installation_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No installations at all → NoGitHubAuthError."""

    _patch_any_lookup(monkeypatch, installation_id=None)
    _patch_mint(
        monkeypatch,
        AssertionError("Mint MUST NOT be called when no installation is linked"),
    )

    with pytest.raises(NoGitHubAuthError):
        get_any_auth_for_user("user-empty-1")


def test_get_any_auth_for_user_suspended_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Suspended-at-mint for the user-level helper → NoGitHubAuthError."""

    _patch_any_lookup(monkeypatch, installation_id=170_003)
    _patch_mint(
        monkeypatch,
        GitHubAppInstallationSuspended("installation_id=170003 suspended"),
    )

    with pytest.raises(NoGitHubAuthError):
        get_any_auth_for_user("user-suspended-1")
