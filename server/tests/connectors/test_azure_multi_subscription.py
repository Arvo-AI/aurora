"""Azure multi-subscription support (DEV-1499) and read-only fail-closed behaviour.

Covers the logic that silently breaks without a test: the read-only command
classifier, per-subscription command pinning, and Resource Graph paging.
"""
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import typing

import pytest


def _load(fn_names, path):
    """Exec named top-level functions out of a module without importing it.

    cloud_exec_tool pulls in the whole agent/DB stack at import time, which is
    not available in unit tests.
    """
    src = open(path).read()
    ns = {"shlex": shlex}
    for fn in fn_names:
        match = re.search(r"^def " + fn + r"\(.*?(?=^def )", src, re.S | re.M)
        assert match, f"{fn} not found in {path}"
        exec(match.group(0), ns)
    return ns


CLOUD_EXEC = "chat/backend/agent/tools/cloud_exec_tool.py"
SETUP_SCRIPT = "connectors/azure_connector/setup-aurora-access.sh"


@pytest.fixture(scope="module")
def helpers():
    return _load(
        ["is_read_only_command", "_apply_azure_subscription", "_is_azure_cli_command",
         "_azure_can_fan_out"],
        CLOUD_EXEC,
    )


@pytest.mark.parametrize("command,read_only", [
    # The bug this replaced: substring matching saw "logs" in the resource name.
    ("az group delete --name my-logs-rg", False),
    ("az vm list", True),
    ("kubectl logs pod-a", True),
    ("kubectl delete pod x", False),
    ("az group create --name foo", False),
    ("az aks show --name c --resource-group r", True),
    ("az monitor log-analytics query -w W --analytics-query Q", True),
    ("az role assignment create --assignee x --role Owner", False),
    ("kubectl exec -it pod -- sh", False),
    ("", False),
])
def test_read_only_classifier(helpers, command, read_only):
    assert helpers["is_read_only_command"](command) is read_only


def test_unparseable_command_is_not_read_only(helpers):
    assert helpers["is_read_only_command"]('az vm list --name "unclosed') is False


@pytest.mark.parametrize("command,expected", [
    ("vm list", "az vm list --subscription SUB1"),
    ("az vm list", "az vm list --subscription SUB1"),
    # An explicit subscription from the agent must win.
    ("az vm list --subscription OTHER", "az vm list --subscription OTHER"),
    # Resource Graph takes --subscriptions (plural) and is scoped by the caller.
    ('az graph query -q "Resources"', 'az graph query -q "Resources"'),
])
def test_apply_azure_subscription(helpers, command, expected):
    assert helpers["_apply_azure_subscription"](command, "SUB1") == expected


@pytest.mark.parametrize("command,is_az", [
    ("az vm list", True),
    ("vm list", True),
    ("kubectl get pods", False),
    ("helm list", False),
    ("terraform plan", False),
])
def test_only_az_commands_fan_out(helpers, command, is_az):
    """kubectl inherits context from `aks get-credentials`, so it must not fan out."""
    assert helpers["_is_azure_cli_command"](command) is is_az


def test_get_credentials_never_fans_out(helpers):
    """Parallel `aks get-credentials` would race on the shared kubeconfig."""
    assert helpers["_azure_can_fan_out"]("az vm list") is True
    assert helpers["_azure_can_fan_out"](
        "az aks get-credentials --name c --resource-group r") is False


def test_resource_graph_query_is_ordered():
    """skip-token paging returns duplicates and gaps without a stable sort."""
    from services.discovery.providers.azure_asset_discovery import RESOURCE_GRAPH_QUERY
    assert "order by id asc" in RESOURCE_GRAPH_QUERY


def test_resource_graph_follows_skip_tokens(monkeypatch):
    """Tenants over the 1000-record page cap must not silently truncate."""
    from services.discovery.providers import azure_asset_discovery as mod

    pages = [
        {"data": [{"id": "1"}], "skip_token": "tok1"},
        {"data": [{"id": "2"}], "skip_token": None},
    ]
    calls = []

    def fake_run(cmd):
        calls.append(cmd)
        return pages[len(calls) - 1]

    monkeypatch.setattr(mod, "_az_login", lambda creds: None)
    monkeypatch.setattr(mod, "_run_graph_query", fake_run)

    out = mod._query_resource_graph({"subscription_id": "S1"}, ["S1"])
    assert [r["id"] for r in out] == ["1", "2"]
    assert "--skip-token" in calls[1] and "tok1" in calls[1]


def test_resource_graph_batches_subscriptions(monkeypatch):
    """Resource Graph times out at 30s on too many subscriptions, so batch them."""
    from services.discovery.providers import azure_asset_discovery as mod

    calls = []
    monkeypatch.setattr(mod, "_az_login", lambda creds: None)
    monkeypatch.setattr(mod, "_run_graph_query", lambda cmd: calls.append(cmd) or {"data": []})

    subs = [f"S{i}" for i in range(mod._SUBSCRIPTION_BATCH_SIZE * 2 + 1)]
    mod._query_resource_graph({}, subs)
    assert len(calls) == 3  # two full batches plus the remainder


def test_read_only_mode_fails_closed_without_distinct_identity():
    """Azure has no session-policy equivalent, so the read-only SP is the boundary."""
    from utils.auth.cloud_auth import _resolve_azure_credentials, READ_ONLY_MODE

    full_access = {"tenant_id": "T", "client_id": "full", "client_secret": "s",
                   "subscription_id": "S1"}

    # Agent mode uses the full-access identity.
    assert _resolve_azure_credentials(full_access, "agent", "u")["client_id"] == "full"

    # Ask mode must refuse rather than fall back to the write-capable identity.
    with pytest.raises(ValueError, match="read-only"):
        _resolve_azure_credentials(full_access, READ_ONLY_MODE, "u")

    with_ro = dict(full_access, read_only={"client_id": "ro", "client_secret": "s2"})
    resolved = _resolve_azure_credentials(with_ro, READ_ONLY_MODE, "u")
    assert resolved["client_id"] == "ro"
    assert resolved["tenant_id"] == "T"       # inherited
    assert resolved["subscription_id"] == "S1"


# ---------------------------------------------------------------------------
# Fan-out at Pinnacle scale.
#
# _cloud_exec_azure_multi_subscription is real concurrency with on-disk state,
# so it is exercised with real threads and real temp directories. Only the two
# boundaries are faked: credential setup and subprocess execution.
# ---------------------------------------------------------------------------

def _load_fanout(run_command, config_dirs, fail_subs=(), mode="agent"):
    """Exec the real fan-out function with faked module-level dependencies."""
    src = open(CLOUD_EXEC).read()
    ns = {"shlex": shlex, "json": __import__("json"), "time": __import__("time"),
          "Optional": typing.Optional, "logger": __import__("logging").getLogger("test")}

    for fn in ["is_read_only_command", "_apply_azure_subscription",
               "_cloud_exec_azure_multi_subscription"]:
        match = re.search(r"^def " + fn + r"\(.*?(?=^def )", src, re.S | re.M)
        ns_src = match.group(0)
        exec(ns_src, ns)

    def fake_setup(user_id, sub_id):
        if sub_id in fail_subs:
            return False, None, None, None, None
        cfg = tempfile.mkdtemp(prefix="aurora-az-test-")
        config_dirs.append(cfg)
        return True, sub_id, "service_principal", {"AZURE_CONFIG_DIR": cfg}, "az login --x"

    class Result:
        def __init__(self, rc, out, err):
            self.returncode, self.stdout, self.stderr = rc, out, err

    ns.update({
        "setup_azure_environment_isolated": fake_setup,
        "terminal_run": run_command,
        "get_command_timeout": lambda c, t: t or 60,
        "get_mode_from_context": lambda: mode,
        "ModeAccessController": type("M", (), {
            "ensure_cloud_command_allowed": staticmethod(
                lambda m, ro, c: (True, "") if m == "agent" or ro else (False, "blocked"))
        }),
        "_Result": Result,
    })
    return ns["_cloud_exec_azure_multi_subscription"], Result


def test_fanout_across_30_subscriptions_isolates_config_dirs():
    """Each subscription needs its own AZURE_CONFIG_DIR or `az login` races."""
    import json as _json
    config_dirs = []
    seen_dirs, seen_cmds, lock = [], [], threading.Lock()

    def run_command(argv, **kw):
        env = kw["env"]
        with lock:
            seen_dirs.append(env["AZURE_CONFIG_DIR"])
            seen_cmds.append(argv[-1])
        time.sleep(0.01)  # widen the window for a real race
        return type("R", (), {"returncode": 0, "stdout": "[]", "stderr": ""})()

    fanout, _ = _load_fanout(run_command, config_dirs)
    conns = [{"account_id": f"sub-{i:02d}"} for i in range(30)]
    out = _json.loads(fanout("u", conns, "vm list"))

    assert out["success"] is True
    assert len(out["results_by_subscription"]) == 30, "every subscription must report"
    # The core invariant: no two concurrent invocations shared a config dir.
    assert len(set(seen_dirs)) == 30, "AZURE_CONFIG_DIR was reused across threads"
    # Every command was pinned to its own subscription.
    for i in range(30):
        assert any(f"--subscription sub-{i:02d}" in c for c in seen_cmds)


def test_fanout_cleans_up_every_temp_dir():
    """A leaked tempdir per exec would accumulate unboundedly in the worker."""
    config_dirs = []

    def run_command(argv, **kw):
        return type("R", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

    fanout, _ = _load_fanout(run_command, config_dirs)
    fanout("u", [{"account_id": f"s{i}"} for i in range(8)], "vm list")

    assert len(config_dirs) == 8
    assert not [d for d in config_dirs if os.path.exists(d)], "temp dirs leaked"


def test_fanout_isolates_per_subscription_failures():
    """One bad subscription must not fail the whole investigation."""
    import json as _json
    config_dirs = []

    def run_command(argv, **kw):
        cmd = argv[-1]
        if "sub-bad" in cmd:
            return type("R", (), {
                "returncode": 1, "stdout": "",
                "stderr": "(SubscriptionNotFound) The subscription could not be found."})()
        return type("R", (), {"returncode": 0, "stdout": '{"ok":true}', "stderr": ""})()

    fanout, _ = _load_fanout(run_command, config_dirs, fail_subs=("sub-noauth",))
    conns = [{"account_id": s} for s in ("sub-ok", "sub-bad", "sub-noauth")]
    out = _json.loads(fanout("u", conns, "vm list"))
    res = out["results_by_subscription"]

    assert out["success"] is False           # aggregate reflects the failures
    assert res["sub-ok"]["success"] is True  # but the good one still returned data
    assert "SubscriptionNotFound" in res["sub-bad"]["output"]
    assert res["sub-noauth"]["error"] == "Failed to authenticate"
    # Auth failure happens before a tempdir is made, so only 2 were created.
    assert len(config_dirs) == 2


def test_fanout_blocks_write_commands_in_ask_mode():
    """Read-only gating must apply to the fan-out path, not just single-sub."""
    import json as _json

    def run_command(argv, **kw):
        raise AssertionError("must not execute a write command in ask mode")

    fanout, _ = _load_fanout(run_command, [], mode="ask")
    out = _json.loads(fanout("u", [{"account_id": "s1"}], "group delete --name x"))
    assert "error" in out and out["multi_subscription"] is True


def test_setup_azure_environment_allocates_a_distinct_config_dir_each_call():
    """The real fix: `az login` writes to AZURE_CONFIG_DIR, so it cannot be shared.

    This exercises setup_azure_environment_isolated itself rather than a stub,
    because that is where the directory is actually allocated.
    """
    src = open(CLOUD_EXEC).read()
    match = re.search(r"^def setup_azure_environment_isolated\(.*?(?=^def )", src, re.S | re.M)
    ns = {
        "os": os, "tempfile": tempfile,
        "logger": __import__("logging").getLogger("test"),
        "_ISOLATED_HOME": "/tmp/aurora-home",
        "get_mode_from_context": lambda: "agent",
        "generate_azure_access_token": lambda uid, sub, mode=None: {
            "access_token": "t", "subscription_id": sub or "S1", "tenant_id": "T",
            "client_id": "cid", "client_secret": "sec",
        },
    }
    exec(match.group(0), ns)
    setup = ns["setup_azure_environment_isolated"]

    dirs = []
    try:
        for sub in ("sub-a", "sub-b", "sub-c"):
            ok, _s, _m, env, auth_cmd = setup("u", sub)
            assert ok
            dirs.append(env["AZURE_CONFIG_DIR"])
            assert os.path.isdir(env["AZURE_CONFIG_DIR"])
            # The secret must never appear anywhere but the auth command itself.
            assert env["AZURE_CLIENT_SECRET"] == "sec"
        assert len(set(dirs)) == 3, "AZURE_CONFIG_DIR must be unique per invocation"
        assert f"{ns['_ISOLATED_HOME']}/.azure" not in dirs, "must not use the shared dir"
    finally:
        for d in dirs:
            __import__("shutil").rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# Management-group subscription enumeration.
#
# Assignments at MG scope only inherit to descendants, so reporting
# tenant-wide subscription ids here would store ids Aurora has no rights on.
# The Azure response nests child management groups, and a flat filter would
# silently drop every subscription below the first level.
# ---------------------------------------------------------------------------

def _walk_mg(payload):
    """Run the walker embedded in setup-aurora-access.sh against a payload."""
    src = open(SETUP_SCRIPT).read()
    body = re.search(r"list_mg_subscriptions\(\) \{.*?\n\}", src, re.S).group(0)
    snippet = re.search(r"python3 -c '(.*?)'\n", body, re.S).group(1)
    out = subprocess.run(
        [sys.executable, "-c", snippet],
        input=json.dumps(payload), capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, out.stderr
    return [line for line in out.stdout.split("\n") if line]


def _sub(name):
    return {"name": name, "type": "/subscriptions", "children": None}


def _mg(name, children):
    return {"name": name, "type": "Microsoft.Management/managementGroups",
            "children": children}


def test_mg_walker_finds_nested_subscriptions():
    # Verified against real Azure: a subscription in a child MG still inherits
    # roles assigned at the grandparent, so it must be reported.
    payload = _mg("root", [_sub("flat"), _mg("child", [_sub("nested")]),
                           _mg("mid", [_mg("deep", [_sub("deeper")])])])
    assert sorted(_walk_mg(payload)) == ["deeper", "flat", "nested"]


def test_mg_walker_handles_empty_and_malformed():
    assert _walk_mg(_mg("root", [])) == []
    assert _walk_mg({"name": "root"}) == []          # children key absent
    assert _walk_mg(_mg("root", [_mg("empty", None)])) == []


# ---------------------------------------------------------------------------
# Ask mode must refuse credential reads.
#
# These pass a verb check ("list", "show") and mutate nothing, but return keys,
# secrets or connection strings, so allowing them would let Ask mode exfiltrate
# standing credentials. There is no second allowlist gate behind this function.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "az storage account keys list --account-name x",
    "az storage account show-connection-string --name x",
    "az keyvault secret show --name s --vault-name v",
    "az keyvault secret list --vault-name v",
    "az keyvault key list --vault-name v",
    "az ad sp credential list --id x",
    "az redis list-keys --name r",
    "az cosmosdb keys list --name c --resource-group g",
    "az acr credential show --name r",
    "aws secretsmanager get-secret-value --secret-id s",
    "gcloud secrets versions access latest --secret=s",
])
def test_credential_reads_are_not_read_only(helpers, command):
    assert helpers["is_read_only_command"](command) is False, command


@pytest.mark.parametrize("command", [
    # Hyphenated subcommands are single tokens and match no bare verb, so they
    # fell through to the default deny. `aks get-credentials` only writes a local
    # kubeconfig and is the required first step of AKS investigation.
    "az aks get-credentials --name c --resource-group r",
    "az aks list",
    "az storage account list",
    "az keyvault list",
    "az monitor metrics list --resource x",
    "kubectl logs pod-x --tail=100",
])
def test_investigation_reads_stay_allowed(helpers, command):
    assert helpers["is_read_only_command"](command) is True, command


# ---------------------------------------------------------------------------
# Disconnect must mark connections inactive even without a secret_ref column.
#
# secret_ref is optional across schemas, but in PostgreSQL a failed SELECT
# aborts the entire transaction, so the UPDATE that follows was silently
# skipped and Azure kept reporting as connected after a disconnect. The probe
# has to be wrapped in a savepoint.
# ---------------------------------------------------------------------------

def test_disconnect_marks_inactive_when_secret_ref_column_missing(monkeypatch):
    import utils.db.connection_utils as cu

    executed = []

    class Cursor:
        aborted = False

        def __enter__(self): return self
        def __exit__(self, *a): return False
        def fetchone(self): return self._row

        def execute(self, sql, params=None):
            stmt = sql.strip().split("\n")[0]
            executed.append(stmt)
            # Mimic Postgres: the failed probe poisons the transaction, and every
            # later statement fails until the savepoint is rolled back.
            if sql.startswith("ROLLBACK TO SAVEPOINT"):
                Cursor.aborted = False
                return
            if sql.startswith(("SAVEPOINT", "RELEASE SAVEPOINT")):
                return
            if Cursor.aborted:
                raise RuntimeError("current transaction is aborted")
            if "secret_ref" in sql:
                Cursor.aborted = True
                raise RuntimeError('column "secret_ref" does not exist')
            self._row = ("arn",) if sql.startswith("SELECT role_arn") else None

    conn = type("Conn", (), {
        "cursor": lambda self: Cursor(),
        "commit": lambda self: executed.append("COMMIT"),
        "rollback": lambda self: executed.append("ROLLBACK"),
        "close": lambda self: None,
    })()

    monkeypatch.setattr(cu, "connect_to_db_as_admin", lambda: conn)
    monkeypatch.setattr(cu, "set_rls_context", lambda *a, **k: "org")

    assert cu.delete_connection_secret("u", "azure", "sub-1") is True
    assert any("ROLLBACK TO SAVEPOINT" in s for s in executed), "probe must be rolled back, not left aborted"
    assert any(s.startswith("UPDATE user_connections") for s in executed), "UPDATE must still run"
    assert "COMMIT" in executed


# --- UI display of multiple subscriptions (DEV-1499) -------------------------
# Two live render paths named only the default subscription, so a user with two
# connected subscriptions saw "Subscription 1" and no sign of the second.

AZURE_ROUTES = "routes/azure/azure_routes.py"


def test_fetch_data_returns_subscription_count():
    """The connect banner branches on subscription_count, which fetch_data omitted.

    Without it the count is always falsy, so the UI always took the singular
    branch and named just the default subscription. Asserting on the jsonify
    payload, not just the local: computing the count but not returning it is
    exactly the bug.
    """
    src = open(AZURE_ROUTES).read()
    match = re.search(r"^def fetch_data\(.*?(?=^@azure_bp)", src, re.S | re.M)
    assert match, "fetch_data not found"
    body = match.group(0)
    assert "get_all_user_connections" in body, "count must come from user_connections, not the token row"
    payloads = re.findall(r"return jsonify\(\{(.*?)\}\)", body, re.S)
    assert payloads, "fetch_data must return a jsonify payload"
    assert any("subscription_count" in p for p in payloads), \
        "subscription_count must be in the response body, not just computed locally"


def test_subscription_list_resolves_real_names():
    """Every subscription needs its real name; the list used to show raw GUIDs.

    Previously: name = default_name if sub_id == default_id else sub_id, so only
    the default was named and the rest rendered as bare subscription GUIDs.
    """
    src = open(AZURE_ROUTES).read()
    match = re.search(r"^def azure_subscriptions_get\(.*?(?=^@azure_bp)", src, re.S | re.M)
    assert match, "azure_subscriptions_get not found"
    body = match.group(0)
    assert "default_name if sub_id == default_id else sub_id" not in body, \
        "non-default subscriptions must not fall back to showing their GUID as the name"
    assert "fetch_subscriptions" in body, "names must be resolved from ARM"
    assert "names.get(sub_id, sub_id)" in body, "GUID stays only as a last-resort fallback"
