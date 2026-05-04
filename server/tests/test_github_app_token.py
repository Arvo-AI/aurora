"""Unit tests for the GitHub App installation token cache + per-installation lock.

Verifies the security and concurrency contracts of
:func:`get_installation_token` against a mocked GitHub installation-token
endpoint:

* First call mints (one HTTP round-trip).
* Cache hits skip the mint entirely.
* Expired tokens trigger a fresh mint.
* 404 maps to :class:`GitHubAppInstallationNotFound`.
* 403 + suspended marker maps to :class:`GitHubAppInstallationSuspended`.
* 20 concurrent threads against a single installation MUST mint exactly once
  (the no-stampede contract — proves the per-installation lock + double-
  checked locking are in place).

HTTP is mocked via the ``responses`` library through the ``responses_mock``
fixture from ``conftest.py``. ``mint_app_jwt`` is patched at the module
boundary so tests don't need a real Vault-backed private key.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

from utils.auth import github_app_token
from utils.auth.github_app_token import (
    GitHubAppInstallationNotFound,
    GitHubAppInstallationSuspended,
    GitHubAppTokenError,
    clear_cache,
    get_installation_token,
)

INSTALLATION_TOKENS_URL_TEMPLATE = (
    "https://api.github.com/app/installations/{installation_id}/access_tokens"
)


@pytest.fixture(autouse=True)
def _reset_module_cache() -> Iterator[None]:
    """Clear the token cache + per-installation locks before AND after each test."""
    clear_cache()
    yield
    clear_cache()


@pytest.fixture(autouse=True)
def _stub_jwt_mint() -> Iterator[None]:
    """Replace :func:`mint_app_jwt` so tests don't need a real private key."""
    with patch.object(github_app_token, "mint_app_jwt", return_value="stub-jwt"):
        yield


def _future_iso(seconds: int = 3600) -> str:
    """Return an ISO-8601 timestamp ``seconds`` in the future (UTC, ``Z`` suffix)."""
    future = int(time.time()) + seconds
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(future))


def _past_iso(seconds: int = 3600) -> str:
    """Return an ISO-8601 timestamp ``seconds`` in the past (UTC, ``Z`` suffix)."""
    past = int(time.time()) - seconds
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(past))


def test_first_call_mints(responses_mock: Any) -> None:
    installation_id = 1001
    url = INSTALLATION_TOKENS_URL_TEMPLATE.format(installation_id=installation_id)
    responses_mock.add(
        responses_mock.POST,
        url,
        json={"token": "ghs_first_call_token", "expires_at": _future_iso()},  # NOSONAR: stub
        status=201,
    )

    token = get_installation_token(installation_id)

    assert token == "ghs_first_call_token"  # NOSONAR: stub
    assert len(responses_mock.calls) == 1


def test_cache_hit_skips_mint(responses_mock: Any) -> None:
    installation_id = 1002
    url = INSTALLATION_TOKENS_URL_TEMPLATE.format(installation_id=installation_id)
    responses_mock.add(
        responses_mock.POST,
        url,
        json={"token": "ghs_cached_token", "expires_at": _future_iso()},  # NOSONAR: stub
        status=201,
    )

    first = get_installation_token(installation_id)
    second = get_installation_token(installation_id)

    assert first == second == "ghs_cached_token"  # NOSONAR: stub
    assert len(responses_mock.calls) == 1


def test_expired_remints(responses_mock: Any) -> None:
    installation_id = 1003
    url = INSTALLATION_TOKENS_URL_TEMPLATE.format(installation_id=installation_id)
    responses_mock.add(
        responses_mock.POST,
        url,
        json={"token": "ghs_expired_token", "expires_at": _past_iso()},  # NOSONAR: stub
        status=201,
    )
    responses_mock.add(
        responses_mock.POST,
        url,
        json={"token": "ghs_fresh_token", "expires_at": _future_iso()},  # NOSONAR: stub
        status=201,
    )

    first = get_installation_token(installation_id)
    second = get_installation_token(installation_id)

    assert first == "ghs_expired_token"  # NOSONAR: stub
    assert second == "ghs_fresh_token"  # NOSONAR: stub
    assert len(responses_mock.calls) == 2


def test_404_raises_not_found(responses_mock: Any) -> None:
    installation_id = 1004
    url = INSTALLATION_TOKENS_URL_TEMPLATE.format(installation_id=installation_id)
    responses_mock.add(
        responses_mock.POST,
        url,
        json={"message": "Not Found"},
        status=404,
    )

    with pytest.raises(GitHubAppInstallationNotFound) as exc_info:
        get_installation_token(installation_id)

    assert "1004" in str(exc_info.value)
    assert isinstance(exc_info.value, GitHubAppTokenError)


def test_403_suspended_raises_suspended(responses_mock: Any) -> None:
    installation_id = 1005
    url = INSTALLATION_TOKENS_URL_TEMPLATE.format(installation_id=installation_id)
    responses_mock.add(
        responses_mock.POST,
        url,
        body='{"message": "This installation has been suspended"}',
        status=403,
        content_type="application/json",
    )

    with pytest.raises(GitHubAppInstallationSuspended) as exc_info:
        get_installation_token(installation_id)

    assert "1005" in str(exc_info.value)
    assert isinstance(exc_info.value, GitHubAppTokenError)


def test_concurrent_calls_no_stampede(responses_mock: Any) -> None:
    """20 threads, single installation_id, MUST mint exactly once.

    A 50ms sleep inside the mocked endpoint deterministically forces lock
    contention — without it the first thread can mint+cache before the
    others even reach the per-installation lock, and the test would still
    pass but wouldn't actually exercise the concurrency contract.
    """
    installation_id = 1006
    url = INSTALLATION_TOKENS_URL_TEMPLATE.format(installation_id=installation_id)

    def _delayed_response(_request: Any) -> tuple[int, dict[str, str], str]:
        time.sleep(0.05)
        body = (
            '{"token": "ghs_no_stampede_token", '
            f'"expires_at": "{_future_iso()}"}}'
        )
        return 201, {"Content-Type": "application/json"}, body

    responses_mock.add_callback(
        responses_mock.POST,
        url,
        callback=_delayed_response,
        content_type="application/json",
    )

    results: list[str] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(20)

    def _worker() -> None:
        try:
            barrier.wait()
            token = get_installation_token(installation_id)
            results.append(token)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(results) == 20
    assert set(results) == {"ghs_no_stampede_token"}
    assert len(responses_mock.calls) == 1
