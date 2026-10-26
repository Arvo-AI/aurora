"""Multiple Splunk instances, scoped the same way AWS accounts are.

The account id is host:port. That is the scope connector access grants, so a
team can be allowed one Splunk and refused the other.
"""

import sys
import types

from utils.splunk_accounts import (
    account_allowed,
    account_summary,
    accounts_from_creds,
    find_account,
    resolve_targets,
    splunk_denial,
    sync_splunk_connections,
    upsert_account,
    SplunkAccessError,
)


def _instance(host, token="token-value"):
    return {
        "account_id": host,
        "api_token": token,
        "base_url": f"https://{host}",
        "server_name": host.split(":")[0],
    }


def test_legacy_blob_is_one_instance():
    accounts = accounts_from_creds({
        "api_token": "secret-token",
        "base_url": "https://Prod.Example.com:8089",
        "server_name": "prod",
    })
    assert len(accounts) == 1
    assert accounts[0]["account_id"] == "prod.example.com:8089"
    assert accounts[0]["api_token"] == "secret-token"
    assert "api_token" not in account_summary(accounts[0])


def test_accounts_list_wins_over_the_primary_mirror():
    accounts = accounts_from_creds({
        "api_token": "primary",
        "base_url": "https://a.example:8089",
        "accounts": [
            _instance("a.example:8089", "primary"),
            _instance("b.example:8089", "other"),
        ],
    })
    assert [a["account_id"] for a in accounts] == ["a.example:8089", "b.example:8089"]


def test_upsert_replaces_the_same_host_in_place():
    existing = [_instance("a.example:8089", "old"), _instance("b.example:8089")]
    updated, replaced = upsert_account(existing, _instance("a.example:8089", "new"))
    assert replaced is True
    assert updated[0]["api_token"] == "new"
    assert updated[1]["account_id"] == "b.example:8089"
    assert updated[0] is not existing[0]


def test_upsert_appends_a_new_host():
    updated, replaced = upsert_account([_instance("a.example:8089")], _instance("b.example:8089"))
    assert replaced is False
    assert [a["account_id"] for a in updated] == ["a.example:8089", "b.example:8089"]


def test_find_account_accepts_a_url():
    accounts = [_instance("a.example:8089"), _instance("b.example:8089")]
    match = find_account(accounts, "https://B.Example:8089/")
    assert match["account_id"] == "b.example:8089"


def test_ungranted_instance_is_refused(monkeypatch):
    import utils.splunk_accounts as accounts_mod

    monkeypatch.setattr(
        accounts_mod,
        "account_allowed",
        lambda user_id, account_id: account_id != "b.example:8089",
    )
    monkeypatch.setattr(accounts_mod, "splunk_denial", lambda user_id: "denied for this team")
    stored = [_instance("a.example:8089"), _instance("b.example:8089")]

    # No selector returns only the granted instance, not a fan-out onto the other.
    assert [a["account_id"] for a in resolve_targets("user", stored, None)] == ["a.example:8089"]

    try:
        resolve_targets("user", stored, "b.example:8089")
    except SplunkAccessError as exc:
        assert exc.code == "CONNECTOR_DENIED"
        assert exc.message == "denied for this team"
    else:
        raise AssertionError("expected the ungranted Splunk to be refused")


def test_unknown_instance_does_not_list_hidden_ones(monkeypatch):
    import utils.splunk_accounts as accounts_mod

    monkeypatch.setattr(
        accounts_mod,
        "account_allowed",
        lambda user_id, account_id: account_id == "a.example:8089",
    )
    try:
        resolve_targets("user", [_instance("a.example:8089"), _instance("b.example:8089")], "missing.example:8089")
    except SplunkAccessError as exc:
        assert exc.code is None
        assert "a.example:8089" in exc.message
        assert "b.example" not in exc.message
    else:
        raise AssertionError("expected an unknown instance")


def test_account_allowed_uses_splunk_scope(monkeypatch):
    seen = {}

    def may_use(user_id, connector, scope_id=""):
        seen["args"] = (user_id, connector, scope_id)
        return scope_id == "a.example:8089"

    fake = types.ModuleType("utils.auth.connector_access")
    fake.may_use = may_use
    fake.denial_text = lambda user_id, connector: f"no {connector} for {user_id}"
    monkeypatch.setitem(sys.modules, "utils.auth.connector_access", fake)

    assert account_allowed("user-1", "a.example:8089") is True
    assert account_allowed("user-1", "b.example:8089") is False
    assert seen["args"] == ("user-1", "splunk", "b.example:8089")
    assert splunk_denial("user-1") == "no splunk for user-1"


def test_sync_writes_one_connection_per_instance_and_retires_the_rest(monkeypatch):
    saved = []
    retired = []

    def save(user_id, provider, account_id, **kwargs):
        saved.append((user_id, provider, account_id, kwargs.get("status")))
        return True

    def get_all(user_id, provider, raise_on_error=False):
        return [{"account_id": "old.example:8089"}, {"account_id": "a.example:8089"}]

    def delete(user_id, provider, account_id):
        retired.append((provider, account_id))
        return True

    import utils.db.connection_utils as connection_utils

    monkeypatch.setattr(connection_utils, "save_connection_metadata", save)
    monkeypatch.setattr(connection_utils, "get_all_user_connections", get_all)
    monkeypatch.setattr(connection_utils, "delete_connection_secret", delete)

    sync_splunk_connections("user-1", [_instance("a.example:8089"), _instance("b.example:8089")])

    assert saved == [
        ("user-1", "splunk", "a.example:8089", "active"),
        ("user-1", "splunk", "b.example:8089", "active"),
    ]
    assert retired == [("splunk", "old.example:8089")]
