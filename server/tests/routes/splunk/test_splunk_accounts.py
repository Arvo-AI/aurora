"""Multiple Splunk instances, scoped the same way AWS accounts are.

The account id is host:port. That is the scope connector access grants, so a
team can be allowed one Splunk and refused the other.
"""

import sys
import types

import pytest

from utils.splunk_accounts import (
    account_allowed,
    account_summary,
    accounts_from_creds,
    find_account,
    list_splunk_accounts,
    resolve_targets,
    splunk_denial,
    sync_splunk_connections,
    upsert_account,
    SplunkAccessError,
)

USER = "11111111-2222-3333-4444-555555555555"


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
    accounts_mod = sys.modules["utils.splunk_accounts"]

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
    accounts_mod = sys.modules["utils.splunk_accounts"]

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


def test_account_allowed_denies_when_the_check_fails(monkeypatch):
    def may_use(user_id, connector, scope_id=""):
        raise RuntimeError("db down")

    fake = types.ModuleType("utils.auth.connector_access")
    fake.may_use = may_use
    monkeypatch.setitem(sys.modules, "utils.auth.connector_access", fake)

    assert account_allowed("user-1", "a.example:8089") is False


def test_account_allowed_stays_open_without_the_access_module(monkeypatch):
    monkeypatch.delitem(sys.modules, "utils.auth.connector_access", raising=False)
    assert account_allowed("user-1", "a.example:8089") is True


class _Cursor:
    def __init__(self, row):
        self._row = row

    def execute(self, *args, **kwargs):
        return None

    def fetchone(self):
        return self._row

    def close(self):
        return None


class _Conn:
    def __init__(self, row):
        self._row = row

    def cursor(self):
        return _Cursor(self._row)

    def commit(self):
        return None

    def close(self):
        return None


def _stub_token_read(monkeypatch, connect):
    monkeypatch.setattr("utils.secrets.secret_ref_utils.resolve_org", lambda user_id: None)
    monkeypatch.setattr("utils.secrets.secret_ref_utils.set_rls_context", lambda *args, **kwargs: None)
    monkeypatch.setattr("utils.secrets.secret_ref_utils.connect_to_db_as_admin", connect)


def test_vault_read_failure_is_not_an_empty_account_list(monkeypatch):
    def connect():
        raise ConnectionError("down")

    _stub_token_read(monkeypatch, connect)
    from utils.auth.token_management import get_token_data

    assert get_token_data(USER, "splunk", org_id=USER) == {}
    with pytest.raises(ConnectionError):
        get_token_data(USER, "splunk", org_id=USER, raise_on_error=True)
    assert list_splunk_accounts(USER) == []
    with pytest.raises(ConnectionError):
        list_splunk_accounts(USER, raise_on_error=True)


def test_missing_splunk_row_stays_not_connected(monkeypatch):
    _stub_token_read(monkeypatch, lambda: _Conn(None))
    from utils.auth.token_management import get_token_data

    assert get_token_data(USER, "splunk", org_id=USER, raise_on_error=True) == {}


def test_missing_vault_secret_is_not_a_read_failure(monkeypatch):
    from utils.secrets.secret_ref_utils import SecretRefManager

    _stub_token_read(
        monkeypatch,
        lambda: _Conn(("vault:kv/data/aurora/users/example", None, None)),
    )
    monkeypatch.setattr(
        SecretRefManager,
        "get_secret",
        lambda self, ref: (_ for _ in ()).throw(Exception("secret not found")),
    )
    from utils.auth.token_management import get_token_data

    assert get_token_data(USER, "splunk", org_id=USER, raise_on_error=True) == {}


def _load_splunk_routes(monkeypatch):
    # The alert task imports Celery and the background package, which dials Redis.
    if "celery_config" not in sys.modules:
        celery_cfg = types.ModuleType("celery_config")

        class _App:
            def task(self, *dargs, **dkwargs):
                def _decorator(fn):
                    fn.delay = lambda *a, **k: None
                    return fn
                if dargs and callable(dargs[0]) and not dkwargs:
                    return dargs[0]
                return _decorator

        celery_cfg.celery_app = _App()
        monkeypatch.setitem(sys.modules, "celery_config", celery_cfg)
    # chat.background.__init__ imports the Redis-backed task module.
    if "chat.background" not in sys.modules:
        background = types.ModuleType("chat.background")
        background.__path__ = []
        prompt_builder = types.ModuleType("chat.background.rca_prompt_builder")
        prompt_builder.build_rca_prompt = lambda *args, **kwargs: ("prompt", "rail")
        background.rca_prompt_builder = prompt_builder
        monkeypatch.setitem(sys.modules, "chat.background", background)
        monkeypatch.setitem(sys.modules, "chat.background.rca_prompt_builder", prompt_builder)
    from routes.splunk import splunk_routes as routes
    return routes


def test_connect_returns_500_instead_of_overwriting(monkeypatch):
    flask = pytest.importorskip("flask")
    routes = _load_splunk_routes(monkeypatch)

    stored = []

    def load(user_id, *, raise_on_error=False):
        if raise_on_error:
            raise RuntimeError("vault down")
        return []

    monkeypatch.setattr(routes, "list_splunk_accounts", load)
    monkeypatch.setattr(routes, "store_splunk_accounts", lambda *args, **kwargs: stored.append(args))
    monkeypatch.setattr(
        routes.SplunkClient,
        "get_server_info",
        lambda self: {"entry": [{"content": {"serverName": "a", "version": "9"}}]},
    )
    monkeypatch.setattr(
        routes.SplunkClient,
        "get_current_user",
        lambda self: {"entry": [{"content": {"username": "svc"}}]},
    )

    app = flask.Flask(__name__)
    with app.test_request_context(
        "/splunk/connect",
        method="POST",
        json={"apiToken": "tok", "baseUrl": "https://a.example:8089"},
    ):
        response, status = routes.connect.__wrapped__(USER)

    assert status == 500
    assert stored == []
    assert response.get_json()["error"] == "Failed to load existing Splunk credentials"


def test_disconnect_does_not_retire_when_the_read_fails(monkeypatch):
    flask = pytest.importorskip("flask")
    routes = _load_splunk_routes(monkeypatch)

    retired = []

    def load(user_id, *, raise_on_error=False):
        if raise_on_error:
            raise RuntimeError("vault down")
        return []

    monkeypatch.setattr(routes, "list_splunk_accounts", load)
    monkeypatch.setattr(routes, "retire_splunk_connections", lambda user_id: retired.append(user_id))

    app = flask.Flask(__name__)
    with app.test_request_context("/splunk/disconnect", method="DELETE"):
        response, status = routes.disconnect.__wrapped__(USER)

    assert status == 500
    assert retired == []
    assert response.get_json()["error"] == "Failed to disconnect Splunk"


def test_fanout_shares_one_output_budget(monkeypatch):
    import chat.backend.agent.tools.splunk_tool as tool

    seen = []

    def fake_search(creds, query, earliest_time, latest_time, max_count, output_budget=tool.MAX_OUTPUT_SIZE):
        seen.append(output_budget)
        return {"success": True, "results": []}

    targets = [
        {"account_id": "a.example:8089", "base_url": "https://a.example:8089", "api_token": "t", "server_name": "a"},
        {"account_id": "b.example:8089", "base_url": "https://b.example:8089", "api_token": "t", "server_name": "b"},
        {"account_id": "c.example:8089", "base_url": "https://c.example:8089", "api_token": "t", "server_name": "c"},
        {"account_id": "d.example:8089", "base_url": "https://d.example:8089", "api_token": "t", "server_name": "d"},
    ]
    monkeypatch.setattr(tool, "_search_one", fake_search)
    monkeypatch.setattr(tool, "_tool_targets", lambda user_id, account_id: (targets, None))

    tool.search_splunk("index=main", user_id=USER)

    assert seen == [tool.MAX_OUTPUT_SIZE // 4] * 4


def test_one_instance_keeps_the_full_output_budget(monkeypatch):
    import chat.backend.agent.tools.splunk_tool as tool

    seen = []

    def fake_search(creds, query, earliest_time, latest_time, max_count, output_budget=tool.MAX_OUTPUT_SIZE):
        seen.append(output_budget)
        return {"success": True, "results": []}

    target = {"account_id": "a.example:8089", "base_url": "https://a.example:8089", "api_token": "t", "server_name": "a"}
    monkeypatch.setattr(tool, "_search_one", fake_search)
    monkeypatch.setattr(tool, "_tool_targets", lambda user_id, account_id: ([target], None))

    tool.search_splunk("index=main", user_id=USER)

    assert seen == [tool.MAX_OUTPUT_SIZE]


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
