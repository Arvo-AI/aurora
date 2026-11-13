"""Multiple RabbitMQ brokers, scoped the same way Splunk instances are.

The account id is the management host:port. That is the scope connector access
grants, so a team can be allowed one broker and refused the other.
"""

import json
import sys
import types

import pytest

from utils.rabbitmq_accounts import (
    RabbitMQAccessError,
    account_allowed,
    account_summary,
    accounts_from_creds,
    clean_name,
    find_account,
    name_conflict,
    rabbitmq_denial,
    resolve_targets,
    sync_rabbitmq_connections,
    upsert_account,
)

USER = "11111111-2222-3333-4444-555555555555"


def _broker(host, password="pw", name=None, vhost="/"):
    return {
        "account_id": host,
        "name": name,
        "url": f"http://{host}",
        "username": "monitor",
        "password": password,
        "vhost": vhost,
        "cluster_name": f"rabbit@{host.split(':')[0]}",
    }


# --- storage shape -----------------------------------------------------------


def test_legacy_blob_is_one_broker():
    accounts = accounts_from_creds({
        "url": "http://10.0.0.1:15672",
        "username": "monitor",
        "password": "s3cret",
        "vhost": "orders",
        "cluster_name": "rabbit@prod",
    })
    assert len(accounts) == 1
    assert accounts[0]["account_id"] == "10.0.0.1:15672"
    assert accounts[0]["vhost"] == "orders"
    assert accounts[0]["password"] == "s3cret"
    summary = account_summary(accounts[0])
    assert "password" not in summary
    assert "s3cret" not in json.dumps(summary)
    assert summary["displayName"] == "rabbit@prod"


def test_incomplete_blob_is_not_connected():
    assert accounts_from_creds({"url": "http://10.0.0.1:15672", "username": "monitor"}) == []
    assert accounts_from_creds({}) == []


def test_accounts_list_wins_over_the_primary_mirror():
    accounts = accounts_from_creds({
        **_broker("10.0.0.1:15672", "primary"),
        "accounts": [_broker("10.0.0.1:15672", "primary"), _broker("10.0.0.2:15672", "other")],
    })
    assert [a["account_id"] for a in accounts] == ["10.0.0.1:15672", "10.0.0.2:15672"]
    assert accounts[1]["password"] == "other"


def test_upsert_replaces_the_same_host_and_keeps_its_name():
    existing = [_broker("10.0.0.1:15672", "old", name="prod"), _broker("10.0.0.2:15672")]
    updated, replaced = upsert_account(existing, _broker("10.0.0.1:15672", "new"))
    assert replaced is True
    assert updated[0]["password"] == "new"
    assert updated[0]["name"] == "prod"
    assert updated[1]["account_id"] == "10.0.0.2:15672"


def test_upsert_appends_a_new_host():
    updated, replaced = upsert_account([_broker("10.0.0.1:15672")], _broker("10.0.0.2:15672"))
    assert replaced is False
    assert [a["account_id"] for a in updated] == ["10.0.0.1:15672", "10.0.0.2:15672"]


def test_name_conflict_ignores_the_same_broker():
    accounts = [_broker("10.0.0.1:15672", name="Prod")]
    assert name_conflict(accounts, "prod", "10.0.0.2:15672") is True
    assert name_conflict(accounts, "prod", "10.0.0.1:15672") is False
    assert name_conflict(accounts, None, "10.0.0.2:15672") is False


@pytest.mark.parametrize("raw", [123, "x" * 101, "bad\nname"])
def test_clean_name_rejects_unusable_names(raw):
    with pytest.raises(ValueError):
        clean_name(raw)


def test_clean_name_treats_blank_as_unset():
    assert clean_name("  ") is None
    assert clean_name(None) is None
    assert clean_name(" prod ") == "prod"


def test_find_account_accepts_id_url_or_name():
    accounts = [_broker("10.0.0.1:15672", name="prod"), _broker("10.0.0.2:15672", name="staging")]
    assert find_account(accounts, "10.0.0.2:15672")["name"] == "staging"
    assert find_account(accounts, "http://10.0.0.2:15672/")["name"] == "staging"
    assert find_account(accounts, "PROD")["account_id"] == "10.0.0.1:15672"
    assert find_account(accounts, "missing") is None
    assert find_account(accounts, "  ") is None


# --- access ------------------------------------------------------------------


def test_ungranted_broker_is_refused(monkeypatch):
    accounts_mod = sys.modules["utils.rabbitmq_accounts"]
    monkeypatch.setattr(accounts_mod, "account_allowed", lambda user_id, account_id: account_id != "10.0.0.2:15672")
    monkeypatch.setattr(accounts_mod, "rabbitmq_denial", lambda user_id: "denied for this team")
    stored = [_broker("10.0.0.1:15672"), _broker("10.0.0.2:15672")]

    # No selector returns only the granted broker, not a fan-out onto the other.
    assert [a["account_id"] for a in resolve_targets("user", stored, None)] == ["10.0.0.1:15672"]

    with pytest.raises(RabbitMQAccessError) as exc:
        resolve_targets("user", stored, "10.0.0.2:15672")
    assert exc.value.code == "CONNECTOR_DENIED"
    assert exc.value.message == "denied for this team"


def test_unknown_broker_does_not_list_hidden_ones(monkeypatch):
    accounts_mod = sys.modules["utils.rabbitmq_accounts"]
    monkeypatch.setattr(accounts_mod, "account_allowed", lambda user_id, account_id: account_id == "10.0.0.1:15672")
    stored = [_broker("10.0.0.1:15672"), _broker("10.0.0.2:15672")]
    with pytest.raises(RabbitMQAccessError) as exc:
        resolve_targets("user", stored, "10.9.9.9:15672")
    assert exc.value.code is None
    assert "10.0.0.1:15672" in exc.value.message
    assert "10.0.0.2" not in exc.value.message


def test_every_broker_ungranted_is_a_denial(monkeypatch):
    accounts_mod = sys.modules["utils.rabbitmq_accounts"]
    monkeypatch.setattr(accounts_mod, "account_allowed", lambda user_id, account_id: False)
    stored = [_broker("10.0.0.1:15672")]
    with pytest.raises(RabbitMQAccessError) as exc:
        resolve_targets("user", stored, None)
    assert exc.value.code == "CONNECTOR_DENIED"


def test_account_allowed_uses_rabbitmq_scope(monkeypatch):
    seen = {}

    def may_use(user_id, connector, scope_id=""):
        seen["args"] = (user_id, connector, scope_id)
        return scope_id == "10.0.0.1:15672"

    fake = types.ModuleType("utils.auth.connector_access")
    fake.may_use = may_use
    fake.denial_text = lambda user_id, connector: f"no {connector} for {user_id}"
    monkeypatch.setitem(sys.modules, "utils.auth.connector_access", fake)

    assert account_allowed("user-1", "10.0.0.1:15672") is True
    assert account_allowed("user-1", "10.0.0.2:15672") is False
    assert seen["args"] == ("user-1", "rabbitmq", "10.0.0.2:15672")
    assert rabbitmq_denial("user-1") == "no rabbitmq for user-1"


def test_account_allowed_denies_when_the_check_fails(monkeypatch):
    def may_use(user_id, connector, scope_id=""):
        raise RuntimeError("db down")

    fake = types.ModuleType("utils.auth.connector_access")
    fake.may_use = may_use
    monkeypatch.setitem(sys.modules, "utils.auth.connector_access", fake)
    assert account_allowed("user-1", "10.0.0.1:15672") is False


def test_sync_writes_one_connection_per_broker_and_retires_the_rest(monkeypatch):
    saved = []
    retired = []

    def save(user_id, provider, account_id, **kwargs):
        saved.append((provider, account_id, kwargs.get("status")))
        return True

    import utils.db.connection_utils as connection_utils

    monkeypatch.setattr(connection_utils, "save_connection_metadata", save)
    monkeypatch.setattr(
        connection_utils,
        "get_all_user_connections",
        lambda user_id, provider, raise_on_error=False: [
            {"account_id": "10.9.9.9:15672"},
            {"account_id": "10.0.0.1:15672"},
        ],
    )
    monkeypatch.setattr(
        connection_utils,
        "delete_connection_secret",
        lambda user_id, provider, account_id: retired.append((provider, account_id)) or True,
    )

    sync_rabbitmq_connections("user-1", [_broker("10.0.0.1:15672"), _broker("10.0.0.2:15672")])

    assert saved == [
        ("rabbitmq", "10.0.0.1:15672", "active"),
        ("rabbitmq", "10.0.0.2:15672", "active"),
    ]
    assert retired == [("rabbitmq", "10.9.9.9:15672")]


# --- routes ------------------------------------------------------------------


@pytest.fixture
def routes(monkeypatch):
    pytest.importorskip("flask")
    from routes.rabbitmq import rabbitmq_routes as module

    monkeypatch.setattr(module, "clear_credentials_cache", lambda user_id: None)
    monkeypatch.setattr(module, "account_allowed", lambda user_id, account_id: True)
    return module


def _app():
    import flask

    return flask.Flask(__name__)


def _call(fn, *args):
    result = fn.__wrapped__(USER, *args)
    if isinstance(result, tuple):
        return result[0].get_json(), result[1]
    return result.get_json(), result.status_code


def _fake_broker_calls(monkeypatch, routes, fail_hosts=()):
    def whoami(self):
        if any(host in self.url for host in fail_hosts):
            raise routes.RabbitMQAPIError("bad password", 401, kind="auth")
        return {"name": self.username, "tags": ["monitoring"]}

    monkeypatch.setattr(routes.RabbitMQClient, "whoami", whoami)
    monkeypatch.setattr(routes.RabbitMQClient, "overview", lambda self: {"cluster_name": "rabbit@x", "rabbitmq_version": "4.0"})
    monkeypatch.setattr(routes.RabbitMQClient, "queues", lambda self, **kwargs: {"items": [], "total_count": 0})


def test_connect_adds_a_second_broker(monkeypatch, routes):
    _fake_broker_calls(monkeypatch, routes)
    stored, prediscovery = [], []
    monkeypatch.setattr(routes, "list_rabbitmq_accounts", lambda user_id, raise_on_error=False: [_broker("10.0.0.1:15672", name="prod")])

    def store(user_id, accounts, schedule_prediscovery=None):
        stored.append(accounts)
        prediscovery.append(schedule_prediscovery)

    monkeypatch.setattr(routes, "store_rabbitmq_accounts", store)

    with _app().test_request_context("/rabbitmq/connect", method="POST", json={
        "url": "http://10.0.0.2:15672", "username": "monitor", "password": "pw2", "name": "staging",
    }):
        body, status = _call(routes.connect)

    assert status == 200
    assert body["replaced"] is False
    assert [a["accountId"] for a in body["accounts"]] == ["10.0.0.1:15672", "10.0.0.2:15672"]
    assert body["connectedAccount"]["name"] == "staging"
    assert "pw2" not in json.dumps(body)
    assert [a["account_id"] for a in stored[0]] == ["10.0.0.1:15672", "10.0.0.2:15672"]
    assert stored[0][1]["password"] == "pw2"
    assert prediscovery == [True]


def test_connect_rejects_a_duplicate_name(monkeypatch, routes):
    _fake_broker_calls(monkeypatch, routes)
    stored = []
    monkeypatch.setattr(routes, "list_rabbitmq_accounts", lambda user_id, raise_on_error=False: [_broker("10.0.0.1:15672", name="prod")])
    monkeypatch.setattr(routes, "store_rabbitmq_accounts", lambda user_id, accounts, **kwargs: stored.append(accounts))

    with _app().test_request_context("/rabbitmq/connect", method="POST", json={
        "url": "http://10.0.0.2:15672", "username": "monitor", "password": "pw", "name": "Prod",
    }):
        _body, status = _call(routes.connect)

    assert status == 409
    assert stored == []


def test_connect_returns_500_instead_of_overwriting(monkeypatch, routes):
    _fake_broker_calls(monkeypatch, routes)
    stored = []

    def load(user_id, *, raise_on_error=False):
        if raise_on_error:
            raise RuntimeError("vault down")
        return []

    monkeypatch.setattr(routes, "list_rabbitmq_accounts", load)
    monkeypatch.setattr(routes, "store_rabbitmq_accounts", lambda *args: stored.append(args))

    with _app().test_request_context("/rabbitmq/connect", method="POST", json={
        "url": "http://10.0.0.2:15672", "username": "monitor", "password": "pw",
    }):
        body, status = _call(routes.connect)

    assert status == 500
    assert stored == []
    assert body["error"] == "Failed to load existing RabbitMQ connections"


def test_status_keeps_working_brokers_when_one_fails(monkeypatch, routes):
    _fake_broker_calls(monkeypatch, routes, fail_hosts=("10.0.0.2",))
    monkeypatch.setattr(routes, "list_rabbitmq_accounts", lambda user_id, raise_on_error=False: [
        _broker("10.0.0.1:15672"), _broker("10.0.0.2:15672"),
    ])
    monkeypatch.setattr(routes, "sync_rabbitmq_connections", lambda user_id, accounts: None)

    with _app().test_request_context("/rabbitmq/status"):
        body, status = _call(routes.status)

    assert status == 200
    assert body["connected"] is True
    assert "error" not in body
    by_id = {a["accountId"]: a for a in body["accounts"]}
    assert by_id["10.0.0.1:15672"]["valid"] is True
    assert by_id["10.0.0.2:15672"]["valid"] is False
    # Top-level fields still describe the first broker for older readers.
    assert body["url"] == "http://10.0.0.1:15672"


def test_status_migrates_a_legacy_connection(monkeypatch, routes):
    _fake_broker_calls(monkeypatch, routes)
    synced = []
    legacy = accounts_from_creds({
        "url": "http://10.0.0.1:15672", "username": "monitor", "password": "pw", "vhost": "orders",
    })
    monkeypatch.setattr(routes, "list_rabbitmq_accounts", lambda user_id, raise_on_error=False: legacy)
    monkeypatch.setattr(routes, "sync_rabbitmq_connections", lambda user_id, accounts: synced.append(accounts))

    with _app().test_request_context("/rabbitmq/status"):
        body, _status = _call(routes.status)

    assert body["connected"] is True
    assert body["vhost"] == "orders"
    assert body["accounts"][0]["accountId"] == "10.0.0.1:15672"
    assert synced
    assert synced[0][0]["account_id"] == "10.0.0.1:15672"


def test_status_with_nothing_stored(monkeypatch, routes):
    monkeypatch.setattr(routes, "list_rabbitmq_accounts", lambda user_id, raise_on_error=False: [])
    with _app().test_request_context("/rabbitmq/status"):
        body, _status = _call(routes.status)
    assert body == {"connected": False, "accounts": []}


def test_disconnect_one_broker_keeps_the_others(monkeypatch, routes):
    stored = []
    deleted = []
    monkeypatch.setattr(routes, "list_rabbitmq_accounts", lambda user_id, raise_on_error=False: [
        _broker("10.0.0.1:15672", name="prod"), _broker("10.0.0.2:15672", name="staging"),
    ])
    monkeypatch.setattr(routes, "store_rabbitmq_accounts", lambda user_id, accounts, **kwargs: stored.append((accounts, kwargs)))
    monkeypatch.setattr(routes, "delete_user_secret", lambda *args: deleted.append(args) or (True, 1))

    with _app().test_request_context("/rabbitmq/disconnect?account=staging", method="DELETE"):
        body, status = _call(routes.disconnect)

    assert status == 200
    accounts, kwargs = stored[0]
    assert [a["account_id"] for a in accounts] == ["10.0.0.1:15672"]
    assert kwargs == {"schedule_prediscovery": False}
    assert [a["accountId"] for a in body["accounts"]] == ["10.0.0.1:15672"]
    assert deleted == []


def test_disconnect_unknown_broker_is_404(monkeypatch, routes):
    monkeypatch.setattr(routes, "list_rabbitmq_accounts", lambda user_id, raise_on_error=False: [_broker("10.0.0.1:15672")])
    with _app().test_request_context("/rabbitmq/disconnect", method="POST", json={"accountId": "10.9.9.9:15672"}):
        _body, status = _call(routes.disconnect_post)
    assert status == 404


def test_disconnect_last_broker_removes_everything(monkeypatch, routes):
    retired = []
    monkeypatch.setattr(routes, "list_rabbitmq_accounts", lambda user_id, raise_on_error=False: [_broker("10.0.0.1:15672")])
    monkeypatch.setattr(routes, "retire_rabbitmq_connections", lambda user_id: retired.append(user_id))
    monkeypatch.setattr(routes, "delete_user_secret", lambda user_id, provider: (True, 1))

    with _app().test_request_context("/rabbitmq/disconnect?account=10.0.0.1:15672", method="DELETE"):
        body, status = _call(routes.disconnect)

    assert status == 200
    assert body["connected"] is False
    assert retired == [USER]


def test_disconnect_does_not_retire_when_the_read_fails(monkeypatch, routes):
    retired = []

    def load(user_id, *, raise_on_error=False):
        if raise_on_error:
            raise RuntimeError("vault down")
        return []

    monkeypatch.setattr(routes, "list_rabbitmq_accounts", load)
    monkeypatch.setattr(routes, "retire_rabbitmq_connections", lambda user_id: retired.append(user_id))

    with _app().test_request_context("/rabbitmq/disconnect", method="DELETE"):
        body, status = _call(routes.disconnect)

    assert status == 500
    assert retired == []
    assert body["error"] == "Failed to disconnect RabbitMQ"


# --- agent tools -------------------------------------------------------------


@pytest.fixture
def tool(monkeypatch):
    import chat.backend.agent.tools.rabbitmq_tool as module

    return module


def _targets(monkeypatch, tool, targets):
    monkeypatch.setattr(tool, "_tool_targets", lambda user_id, account_id: (targets, None))


def test_one_broker_returns_a_flat_result_with_the_broker(monkeypatch, tool):
    _targets(monkeypatch, tool, [_broker("10.0.0.1:15672", name="prod")])
    monkeypatch.setattr(tool.RabbitMQClient, "overview", lambda self: {"cluster_name": "rabbit@prod"})

    out = json.loads(tool.rabbitmq_get_overview(user_id=USER))

    assert out["cluster_name"] == "rabbit@prod"
    assert out["account_id"] == "10.0.0.1:15672"
    assert out["broker"] == "prod"
    assert list(out)[0] == "brokers"


def test_fanout_keeps_the_other_broker_when_one_fails(monkeypatch, tool):
    _targets(monkeypatch, tool, [_broker("10.0.0.1:15672"), _broker("10.0.0.2:15672")])

    def nodes(self):
        if "10.0.0.1" in self.url:
            raise tool.RabbitMQAPIError("Timed out reaching the RabbitMQ management API", kind="timeout")
        return [{"name": "rabbit@b", "mem_alarm": False}]

    monkeypatch.setattr(tool.RabbitMQClient, "nodes", nodes)

    out = json.loads(tool.rabbitmq_list_nodes(user_id=USER))

    by_id = out["results_by_account"]
    assert "Timed out" in by_id["10.0.0.1:15672"]["error"]
    assert by_id["10.0.0.2:15672"]["nodes"][0]["name"] == "rabbit@b"
    assert by_id["10.0.0.2:15672"]["account_id"] == "10.0.0.2:15672"
    assert [b["account_id"] for b in out["brokers"]] == ["10.0.0.1:15672", "10.0.0.2:15672"]


def test_queue_name_survives_the_broker_fields(monkeypatch, tool):
    _targets(monkeypatch, tool, [_broker("10.0.0.1:15672", name="prod"), _broker("10.0.0.2:15672")])
    monkeypatch.setattr(tool.RabbitMQClient, "queue", lambda self, vhost, name: {"name": name, "vhost": vhost})

    out = json.loads(tool.rabbitmq_get_queue(user_id=USER, name="orders"))

    for item in out["results_by_account"].values():
        assert item["name"] == "orders"


def test_each_broker_uses_its_own_default_vhost(monkeypatch, tool):
    _targets(monkeypatch, tool, [_broker("10.0.0.1:15672", vhost="a"), _broker("10.0.0.2:15672", vhost="b")])
    monkeypatch.setattr(tool.RabbitMQClient, "bindings", lambda self, vhost, queue: [{"vhost": vhost}])

    out = json.loads(tool.rabbitmq_list_bindings(user_id=USER, queue="q"))

    assert out["results_by_account"]["10.0.0.1:15672"]["vhost"] == "a"
    assert out["results_by_account"]["10.0.0.2:15672"]["vhost"] == "b"


def test_account_id_is_passed_to_target_resolution(monkeypatch, tool):
    seen = []
    accounts = [_broker("10.0.0.1:15672"), _broker("10.0.0.2:15672", name="staging")]
    monkeypatch.setattr(tool, "list_rabbitmq_accounts", lambda user_id: accounts)
    monkeypatch.setattr(tool, "resolve_targets", lambda user_id, stored, selector: seen.append(selector) or [stored[1]])
    monkeypatch.setattr(tool.RabbitMQClient, "overview", lambda self: {"ok": True})

    out = json.loads(tool.rabbitmq_get_overview(user_id=USER, account_id="staging"))

    assert seen == ["staging"]
    assert out["account_id"] == "10.0.0.2:15672"


def test_denied_broker_returns_the_denial(monkeypatch, tool):
    monkeypatch.setattr(tool, "list_rabbitmq_accounts", lambda user_id: [_broker("10.0.0.1:15672")])

    def deny(user_id, stored, selector):
        raise RabbitMQAccessError("denied", "CONNECTOR_DENIED")

    monkeypatch.setattr(tool, "resolve_targets", deny)

    out = json.loads(tool.rabbitmq_get_overview(user_id=USER, account_id="10.0.0.1:15672"))

    assert out == {"error": "denied", "code": "CONNECTOR_DENIED"}
    assert tool.is_rabbitmq_connected(USER) is False


def test_not_connected(monkeypatch, tool):
    monkeypatch.setattr(tool, "list_rabbitmq_accounts", lambda user_id: [])
    assert "not connected" in json.loads(tool.rabbitmq_list_nodes(user_id=USER))["error"]
    assert tool.is_rabbitmq_connected(USER) is False


def test_list_brokers_never_returns_a_password(monkeypatch, tool):
    _targets(monkeypatch, tool, [_broker("10.0.0.1:15672", "hunter2", name="prod", vhost="orders")])

    text = tool.rabbitmq_list_brokers(user_id=USER)
    out = json.loads(text)

    assert "hunter2" not in text
    assert out["count"] == 1
    assert out["brokers"][0]["default_vhost"] == "orders"
    assert out["brokers"][0]["broker"] == "prod"


def test_every_broker_tool_takes_account_id(tool):
    for name in dir(tool):
        model = getattr(tool, name)
        if name.startswith("RabbitMQ") and name.endswith("Args") and name != "RabbitMQListBrokersArgs":
            assert "account_id" in model.model_fields, name


# --- connector status card ---------------------------------------------------


def test_status_card_is_connected_when_any_broker_answers(monkeypatch):
    from routes import connector_status

    monkeypatch.setattr(
        connector_status,
        "_probe_rabbitmq",
        lambda account: {"url": account["url"], "username": "monitor"} if "10.0.0.2" in account["url"] else None,
    )
    creds = {**_broker("10.0.0.1:15672"), "accounts": [_broker("10.0.0.1:15672"), _broker("10.0.0.2:15672")]}

    result = connector_status._check_rabbitmq(creds)

    assert result["connected"] is True
    assert result["url"] == "http://10.0.0.2:15672"
    assert result["accountCount"] == 2


def test_status_card_reads_a_legacy_blob(monkeypatch):
    from routes import connector_status

    monkeypatch.setattr(connector_status, "_probe_rabbitmq", lambda account: None)
    result = connector_status._check_rabbitmq({"url": "http://10.0.0.1:15672", "username": "u", "password": "p"})
    assert result == {"connected": False, "accountCount": 1}
    assert connector_status._check_rabbitmq({}) == {"connected": False}
