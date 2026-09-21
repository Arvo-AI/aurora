"""Tests multi-organization Datadog connections.

Several Datadog orgs can be connected at once -- commonly one per environment,
e.g. a separate dev and prod -- because each org needs its own API +
application key pair. They live in a single Vault blob under ``accounts``, with
the primary also mirrored at the top level.

Three properties are load-bearing and get explicit coverage:

1. A pre-multi-account blob (one bare credential dict, no ``accounts`` key)
   still resolves. Anything less silently disconnects every existing user.
2. ``is_datadog_connected`` is true for both blob shapes. It gates tool
   registration, so a false negative removes ``query_datadog`` from the agent's
   toolset with no error anywhere to explain the absence.
3. Selection never silently falls back to the primary on a bad label. Returning
   the wrong org's data is the exact failure this feature exists to prevent:
   healthy data from the wrong environment reads as "the service is fine".

Pure functions only: no DB, no network, no Datadog credentials.
"""

import os
import sys

import pytest

_server_dir = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))

from routes.datadog import datadog_routes  # noqa: E402
from routes.datadog.datadog_routes import (  # noqa: E402
    _account_label,
    _account_summary,
    list_datadog_accounts,
    select_datadog_account,
)
from routes.connector_status import _check_datadog  # noqa: E402


def _account(label=None, **overrides):
    account = {
        "api_key": f"api-{label or 'primary'}",
        "app_key": f"app-{label or 'primary'}",
        "site": "datadoghq.com",
        "base_url": "https://api.datadoghq.com",
        "org_name": None,
        "service_account_name": None,
    }
    if label:
        account["label"] = label
    account.update(overrides)
    return account


@pytest.fixture
def stored_creds(monkeypatch):
    """Swap the Vault-backed credential read for an in-memory blob."""
    holder = {}

    def _set(blob):
        holder["blob"] = blob

    monkeypatch.setattr(
        datadog_routes, "_get_stored_datadog_credentials",
        lambda user_id: holder.get("blob"),
    )
    return _set


# ---------------------------------------------------------------------------
# Backwards compatibility: the shape already in production
# ---------------------------------------------------------------------------


def test_legacy_blob_resolves_as_single_account(stored_creds):
    """A blob written before multi-org support has no 'accounts' key."""
    stored_creds({
        "api_key": "api-legacy",
        "app_key": "app-legacy",
        "site": "datadoghq.eu",
        "org_name": "Acme Prod",
    })

    accounts = list_datadog_accounts("u1")

    assert len(accounts) == 1
    assert accounts[0]["api_key"] == "api-legacy"
    # No explicit label, so it falls back to org_name.
    assert _account_label(accounts[0]) == "Acme Prod"


def test_label_falls_back_through_org_name_then_site_then_default():
    assert _account_label({"label": "dev", "org_name": "Acme", "site": "x"}) == "dev"
    assert _account_label({"org_name": "Acme", "site": "x"}) == "Acme"
    assert _account_label({"site": "datadoghq.eu"}) == "datadoghq.eu"
    assert _account_label({}) == "default"
    # Whitespace-only label must not produce an unselectable empty string.
    assert _account_label({"label": "   "}) == "default"


def test_no_connection_yields_empty_list(stored_creds):
    stored_creds(None)
    assert list_datadog_accounts("u1") == []


# ---------------------------------------------------------------------------
# Multi-account resolution
# ---------------------------------------------------------------------------


def test_multi_account_blob_resolves_all_in_order(stored_creds):
    prod, dev = _account("prod"), _account("dev")
    stored_creds({**prod, "accounts": [prod, dev]})

    accounts = list_datadog_accounts("u1")

    assert [_account_label(a) for a in accounts] == ["prod", "dev"]


def test_accounts_missing_either_key_are_dropped(stored_creds):
    """A half-written account would build a client that 401s on every call."""
    good = _account("prod")
    stored_creds({
        **good,
        "accounts": [
            good,
            _account("no-app-key", app_key=None),
            _account("no-api-key", api_key=None),
            "not-a-dict",
        ],
    })

    assert [_account_label(a) for a in list_datadog_accounts("u1")] == ["prod"]


def test_empty_accounts_list_falls_back_to_top_level_mirror(stored_creds):
    """The mirror is the recovery path if the list is ever emptied in place."""
    stored_creds({**_account("prod"), "accounts": []})

    assert [_account_label(a) for a in list_datadog_accounts("u1")] == ["prod"]


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def test_no_selector_returns_primary():
    accounts = [_account("prod"), _account("dev")]
    assert _account_label(select_datadog_account(accounts, None)) == "prod"
    assert _account_label(select_datadog_account(accounts, "")) == "prod"
    assert _account_label(select_datadog_account(accounts, "   ")) == "prod"


def test_selector_matches_label_case_insensitively_and_trims():
    accounts = [_account("prod"), _account("dev")]
    for selector in ("dev", "DEV", "  Dev  "):
        assert _account_label(select_datadog_account(accounts, selector)) == "dev"


def test_unknown_selector_returns_none_rather_than_primary():
    """Falling back to the primary here would hand back prod data for a dev
    query -- silently, and indistinguishably from a correct answer."""
    accounts = [_account("prod"), _account("dev")]
    assert select_datadog_account(accounts, "staging") is None


def test_select_on_empty_list_returns_none():
    assert select_datadog_account([], None) is None
    assert select_datadog_account([], "prod") is None


# ---------------------------------------------------------------------------
# Summaries must not leak credentials
# ---------------------------------------------------------------------------


def test_account_summary_excludes_credentials():
    summary = _account_summary(_account("prod", org_name="Acme Prod"))

    assert summary == {
        "label": "prod",
        "site": "datadoghq.com",
        "orgName": "Acme Prod",
        "serviceAccountName": None,
        "validatedAt": None,
    }
    assert "api_key" not in summary and "app_key" not in summary


# ---------------------------------------------------------------------------
# is_datadog_connected gates tool registration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blob,expected", [
    (None, False),
    ({"api_key": "a", "app_key": "b"}, True),                      # legacy
    ({"api_key": "a"}, False),                                     # half-written
    ({"api_key": "a", "app_key": "b",
      "accounts": [_account("prod"), _account("dev")]}, True),     # multi
])
def test_is_datadog_connected_across_blob_shapes(stored_creds, blob, expected):
    from chat.backend.agent.tools.datadog_tool import is_datadog_connected

    stored_creds(blob)
    assert is_datadog_connected("u1") is expected


# ---------------------------------------------------------------------------
# Status check: one dead org must not hide a healthy one
# ---------------------------------------------------------------------------


def _fake_validate(monkeypatch, valid_by_api_key):
    class _Response:
        def __init__(self, valid):
            self._valid = valid

        def json(self):
            return {"valid": self._valid}

    def _get(url, headers=None, timeout=None):
        return _Response(valid_by_api_key.get(headers["DD-API-KEY"], False))

    monkeypatch.setattr("routes.connector_status.requests.get", _get)


def test_check_datadog_connected_when_any_account_valid(monkeypatch):
    """A revoked prod key must not report the whole provider disconnected while
    a healthy dev org is still connected and queryable."""
    prod, dev = _account("prod"), _account("dev")
    _fake_validate(monkeypatch, {"api-prod": False, "api-dev": True})

    result = _check_datadog({**prod, "accounts": [prod, dev]})

    assert result["connected"] is True
    assert [a["connected"] for a in result["accounts"]] == [False, True]


def test_check_datadog_disconnected_only_when_all_invalid(monkeypatch):
    prod, dev = _account("prod"), _account("dev")
    _fake_validate(monkeypatch, {})

    assert _check_datadog({**prod, "accounts": [prod, dev]})["connected"] is False


def test_check_datadog_legacy_blob_omits_accounts_key(monkeypatch):
    """Single-org responses keep their existing shape, so no consumer of
    {connected, site} has to learn about accounts."""
    _fake_validate(monkeypatch, {"api-primary": True})

    result = _check_datadog(_account())

    assert result == {"connected": True, "site": "datadoghq.com"}
