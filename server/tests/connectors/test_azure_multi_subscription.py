"""Azure multi-subscription support (DEV-1499) and read-only fail-closed behaviour.

Covers the logic that silently breaks without a test: the read-only command
classifier, per-subscription command pinning, and Resource Graph paging.
"""
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import types
import typing

import pytest


def _read(path):
    """Read a source file, closing the handle deterministically."""
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _load(fn_names, path):
    """Exec named top-level functions out of a module without importing it.

    cloud_exec_tool pulls in the whole agent/DB stack at import time, which is
    not available in unit tests.
    """
    src = _read(path)
    ns = {"shlex": shlex}
    for fn in fn_names:
        match = re.search(r"^def " + fn + r"\(.*?(?=^def )", src, re.S | re.M)
        assert match, f"{fn} not found in {path}"
        exec(match.group(0), ns)
    return ns


CLOUD_EXEC = "chat/backend/agent/tools/cloud_exec_tool.py"
SETUP_SCRIPT = "connectors/azure_connector/setup-aurora-access.sh"
BILLING = "connectors/azure_connector/billing.py"


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
    assert "--skip-token" in calls[1], "second page did not pass a skip token"
    assert "tok1" in calls[1], "second page used the wrong skip token"


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
    src = _read(CLOUD_EXEC)
    ns = {"shlex": shlex, "json": __import__("json"), "time": __import__("time"),
          "contextvars": __import__("contextvars"),
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
            # argv is a list, so join to inspect it. Auth runs as its own
            # invocation now, so both it and the command land here.
            seen_cmds.append(" ".join(argv))
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
        assert any(f"--subscription sub-{i:02d}" in c for c in seen_cmds), \
            f"sub-{i:02d} command was not pinned to its subscription"
    # No invocation may go through a shell: auth_command interpolates the client
    # secret unquoted, so `bash -lc` would let a secret with a metacharacter break
    # out of its argument. This is the regression guard for that.
    for argv_str in seen_cmds:
        assert not argv_str.startswith("bash -lc"), \
            "command was executed through a shell; secret interpolation is exploitable"


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
    assert "error" in out, "write command was not rejected in ask mode"
    assert out["multi_subscription"] is True, "result lost its multi-subscription marker"


def test_setup_azure_environment_allocates_a_distinct_config_dir_each_call():
    """The real fix: `az login` writes to AZURE_CONFIG_DIR, so it cannot be shared.

    This exercises setup_azure_environment_isolated itself rather than a stub,
    because that is where the directory is actually allocated.
    """
    src = _read(CLOUD_EXEC)
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
    src = _read(SETUP_SCRIPT)
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

    # Real user ids are UUIDs; org_read_predicate validates them to keep
    # non-UUID input out of the query params.
    assert cu.delete_connection_secret(USER_A, "azure", "sub-1") is True
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
    src = _read(AZURE_ROUTES)
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
    src = _read(AZURE_ROUTES)
    match = re.search(r"^def azure_subscriptions_get\(.*?(?=^@azure_bp)", src, re.S | re.M)
    assert match, "azure_subscriptions_get not found"
    body = match.group(0)
    assert "default_name if sub_id == default_id else sub_id" not in body, \
        "non-default subscriptions must not fall back to showing their GUID as the name"
    assert "fetch_subscriptions" in body, "names must be resolved from ARM"
    assert "names.get(sub_id, sub_id)" in body, "GUID stays only as a last-resort fallback"


# --- Guardrail context must survive the fan-out ------------------------------
# ThreadPoolExecutor workers start with an EMPTY contextvars context. The safety
# judge reads user_id/session_id/state from contextvars, so without an explicit
# copy every fanned-out command was blocked: "missing user context", exit 126.

def test_fan_out_propagates_contextvars_to_workers():
    """A bare pool.submit loses context; copy_context().run keeps it.

    Models the real failure rather than the real stack: the guardrail reads a
    contextvar, and the worker must see the value the caller set.
    """
    import concurrent.futures
    import contextvars as cv

    var = cv.ContextVar("user_id", default=None)
    var.set("user-123")

    def worker():
        return var.get()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        # The bug: plain submit runs with a fresh, empty context.
        assert pool.submit(worker).result() is None, \
            "bare submit unexpectedly saw the caller context; model is invalid"
        # The fix: copy the context per task.
        assert pool.submit(cv.copy_context().run, worker).result() == "user-123"


def test_both_fan_outs_copy_context():
    """Both providers must copy context; a bare submit reintroduces exit-126 blocks."""
    src = _read(CLOUD_EXEC)
    for fn in ("_cloud_exec_aws_multi_account", "_cloud_exec_azure_multi_subscription"):
        match = re.search(r"^def " + fn + r"\(.*?(?=^def )", src, re.S | re.M)
        assert match, f"{fn} not found"
        submits = re.findall(r"pool\.submit\(\s*([^,]+)", match.group(0))
        assert submits, f"{fn} has no pool.submit"
        for first_arg in submits:
            assert "copy_context" in first_arg, \
                f"{fn} submits {first_arg.strip()!r} directly instead of via " \
                "contextvars.copy_context().run; guardrails will fail closed"


# --- Org-scoped writes on user_connections -----------------------------------
# Reads match (user_id OR org_id) but the unique constraint is per-user, so a
# teammate's disconnect/toggle used to match zero rows (disconnect silently
# no-oped) or insert a competing row (toggle shadowed, sub stayed active).
# Affects every provider in user_connections: azure, aws, ovh.

def _conn_utils_with_fake_db(monkeypatch, rows):
    """Wire connection_utils to an in-memory user_connections stand-in."""
    import utils.db.connection_utils as cu

    class Cursor:
        def __init__(self):
            self._result = None
            self.rowcount = 0

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=()):
            s = " ".join(sql.split())
            self.rowcount = 0
            self._result = None
            if s.startswith("SAVEPOINT") or s.startswith("RELEASE") or s.startswith("ROLLBACK TO"):
                return
            # Org predicate carries two ids; user-only carries one.
            org_scoped = "user_id = %s OR org_id = %s" in s
            ids = params[:2] if org_scoped else params[:1]
            if s.startswith("UPDATE user_connections SET status = 'inactive'"):
                ids = params[1:3] if org_scoped else params[1:2]
            if s.startswith("UPDATE user_connections SET role_arn"):
                ids = params[7:9] if org_scoped else params[7:8]

            def visible(r):
                return r["user_id"] in ids or (org_scoped and r["org_id"] in ids)

            if s.startswith("SELECT role_arn"):
                hit = [r for r in rows if visible(r) and r["status"] == "active"]
                self._result = ("arn",) if hit else None
            elif s.startswith("SELECT secret_ref"):
                self._result = None
            elif s.startswith("UPDATE user_connections SET status = 'inactive'"):
                for r in rows:
                    if visible(r):
                        r["status"] = "inactive"
                        self.rowcount += 1
            elif s.startswith("UPDATE user_connections SET role_arn"):
                for r in rows:
                    if visible(r):
                        r["status"] = params[5]
                        self.rowcount += 1
            elif s.startswith("INSERT INTO user_connections"):
                rows.append({"user_id": params[0], "org_id": params[1], "status": params[9]})
                self.rowcount = 1

        def fetchone(self):
            return self._result

    conn = type("Conn", (), {
        "cursor": lambda self: Cursor(),
        "commit": lambda self: None,
        "rollback": lambda self: None,
        "close": lambda self: None,
    })()
    monkeypatch.setattr(cu, "connect_to_db_as_admin", lambda: conn)
    monkeypatch.setattr(cu, "set_rls_context", lambda *a, **k: ORG)
    monkeypatch.setattr(cu, "_resolve_org_id", lambda uid: ORG)
    return cu


ORG = "20ee6a53-a776-4578-8d2c-dfc1afb2fc8c"
USER_A = "533757ff-de70-4700-a285-7dee94bd93b8"
USER_B = "00000000-0000-0000-0000-0000000000bb"


def test_teammate_can_disconnect_org_shared_connection(monkeypatch):
    """B disconnecting an account A connected must actually deactivate it."""
    rows = [{"user_id": USER_A, "org_id": ORG, "status": "active"}]
    cu = _conn_utils_with_fake_db(monkeypatch, rows)

    assert cu.delete_connection_secret(USER_B, "azure", "sub-1") is True, \
        "teammate disconnect returned False; the row was not matched"
    assert rows[0]["status"] == "inactive", "A's connection was left active after B disconnected"


def test_teammate_write_does_not_create_shadow_row(monkeypatch):
    """B writing an account A connected must update A's row, not insert a competing one.

    The Azure subscription toggle that first exposed this is retired, but the same
    path runs on reconnect and for aws/ovh, and a duplicate row would make
    cloud_exec fan out to the same account twice.
    """
    rows = [{"user_id": USER_A, "org_id": ORG, "status": "active"}]
    cu = _conn_utils_with_fake_db(monkeypatch, rows)

    assert cu.save_connection_metadata(USER_B, "azure", "sub-1", status="inactive") is True
    assert len(rows) == 1, f"expected the existing row to be updated, got {len(rows)} rows (shadow row inserted)"
    assert rows[0]["status"] == "inactive", "write did not take effect on the org-shared row"


def test_save_connection_metadata_still_inserts_when_absent(monkeypatch):
    """The update-first path must not break first-time connects."""
    rows = []
    cu = _conn_utils_with_fake_db(monkeypatch, rows)

    assert cu.save_connection_metadata(USER_A, "azure", "sub-1") is True
    assert len(rows) == 1, "first connect did not insert exactly one row"
    assert rows[0]["status"] == "active", "inserted row was not active"


# --- Retired per-subscription toggle -----------------------------------------
# The toggle looked like an access boundary but was not one: the service
# principal keeps its Azure role assignments, and any cloud_exec call passing an
# explicit subscription id skipped the status filter entirely. Scope now lives
# where Azure enforces it (setup-aurora-access.sh with a management group).

def test_subscription_post_is_retired():
    """POST /api/azure-subscriptions must not silently persist a selection again."""
    src = _read(AZURE_ROUTES)
    # Last function in the file, so anchor on end-of-string as well as the next def.
    match = re.search(r"def azure_subscriptions_post\(.*?(?=\n@|\ndef |\Z)", src, re.S)
    assert match, "azure_subscriptions_post not found"
    body = match.group(0)

    # Match the actual return, not a bare "410": the docstring mentions the status
    # too, so a substring check still passes after the return value is changed.
    assert re.search(r"\)\s*,\s*410\b", body), \
        "retired endpoint must return 410 Gone so stale clients fail loudly"
    assert "save_connection_metadata" not in body, \
        "POST still writes connection metadata; the toggle was supposed to be retired"


def test_azure_ui_has_no_subscription_toggle():
    """The Azure connector UI must not render a per-subscription switch.

    Resolved relative to this file, not the cwd: the server container mounts only
    server/ at /app, so a cwd-relative path silently misses and the check passes
    vacuously. Skips explicitly when client/ is genuinely absent.
    """
    component = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "../../../client/src/components/azure-provider-integration.tsx",
    )
    if not os.path.exists(component):
        pytest.skip("client/ not present in this checkout")
    src = _read(component)
    assert "showToggle={false}" in src, "Azure subscription toggle is rendered again"
    assert "saveProjects" not in src, "UI still posts a subscription selection"


# --- Login reconciles removals -----------------------------------------------

def test_subscription_listing_follows_nextlink():
    """A truncated list would make login deactivate valid subscriptions.

    Login now deactivates any persisted subscription absent from this list, so if
    ARM paginates and only page one is read, every subscription past the first page
    is marked inactive and silently drops out of fan-out.
    """
    src = _read(BILLING)
    match = re.search(r"^def fetch_subscriptions\(.*?(?=^def |\Z)", src, re.S | re.M)
    assert match, "fetch_subscriptions not found"
    ns = {"requests": None, "logging": logging}

    pages = [
        {"value": [{"subscriptionId": "s0", "state": "Enabled"},
                   {"subscriptionId": "s1", "state": "Enabled"}],
         "nextLink": "https://management.azure.com/next"},
        {"value": [{"subscriptionId": "s2", "state": "Enabled"}]},
    ]
    seen_urls = []

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    def _get(url, **_kw):
        seen_urls.append(url)
        return _Resp(pages[len(seen_urls) - 1])

    ns["requests"] = typing.cast(typing.Any, types.SimpleNamespace(get=_get))
    exec(match.group(0), ns)

    out = ns["fetch_subscriptions"]("token")
    assert [s["subscriptionId"] for s in out] == ["s0", "s1", "s2"], \
        "nextLink was not followed; subscription list is truncated"
    assert seen_urls[1].endswith("/next"), "second request did not use nextLink"


def test_subscription_listing_returns_empty_on_failure():
    """Failure must yield [], which trips login's existing 'no subscriptions' 400.

    Returning a partial list here instead would let the reconcile in azure_login
    deactivate subscriptions just because ARM was briefly unreachable.
    """
    src = _read(BILLING)
    match = re.search(r"^def fetch_subscriptions\(.*?(?=^def |\Z)", src, re.S | re.M)

    def _boom(_url, **_kw):
        raise RuntimeError("ARM unreachable")

    ns = {"requests": typing.cast(typing.Any, types.SimpleNamespace(get=_boom)), "logging": logging}
    exec(match.group(0), ns)
    assert ns["fetch_subscriptions"]("token") == []


def test_login_deactivates_subscriptions_that_lost_access():
    """Login is the only writer of these rows, so it must reconcile removals."""
    src = _read("connectors/azure_connector/auth.py")
    match = re.search(r"enabled_ids\s*=.*?(?=\n            logging\.info)", src, re.S)
    assert match, "login does not compute the enabled-subscription set"
    body = match.group(0)
    assert "delete_connection_secret" in body, \
        "login never deactivates subscriptions that are no longer accessible"
    assert "not in enabled_ids" in body, \
        "deactivation is not keyed on absence from the current enabled set"
