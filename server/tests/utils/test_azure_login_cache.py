"""Tests for utils.cloud.azure_login_cache -- one `az login` per credential set.

The cache holds service principal credentials on disk, so the properties that
matter are isolation (who can land in which directory), lifetime, and that a
login is never repeated or skipped when it should not be. `az` itself is faked:
a login is a callable that writes into the directory, exactly the boundary the
real module sees.
"""

import json
import os
import stat
import sys
import tempfile
import threading
import time

import pytest

# Ensure server/ is on sys.path
_server_dir = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))

from utils.cloud import azure_login_cache as cache


class _Result:
    def __init__(self, returncode=0, stderr=""):
        self.returncode, self.stderr = returncode, stderr


def _env(secret="s3cret", client="client-a", tenant="tenant-a"):
    """An env shaped like setup_azure_environment_isolated's, private dir included."""
    return {
        "AZURE_TENANT_ID": tenant,
        "AZURE_CLIENT_ID": client,
        "AZURE_CLIENT_SECRET": secret,
        "AZURE_CONFIG_DIR": tempfile.mkdtemp(prefix="aurora-az-"),
    }


class _Logins:
    """Counts logins and writes what `az login` would into the config dir."""

    def __init__(self, env, returncode=0, stderr="", delay=0.0):
        self.env, self.returncode, self.stderr, self.delay = env, returncode, stderr, delay
        self.count = 0
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            self.count += 1
        time.sleep(self.delay)
        if self.returncode == 0:
            path = os.path.join(self.env["AZURE_CONFIG_DIR"], "service_principal_entries.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{}")
        return _Result(self.returncode, self.stderr)


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Give every test its own cache root, a known key, and a fresh sweep clock."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-key")
    # Unset means pod isolation is on (terminal_run's default), which disables the cache.
    monkeypatch.setenv("ENABLE_POD_ISOLATION", "false")
    monkeypatch.setattr(cache, "_last_sweep", 0.0)
    return tmp_path


def _age_marker(login, *, created=None, last_used=None):
    marker = login._marker
    if created is not None:
        with open(marker, "w", encoding="utf-8") as fh:
            json.dump({"created": created}, fh)
    used = last_used if last_used is not None else time.time()
    os.utime(marker, (used, used))


# ---------------------------------------------------------------------------
# The point of the module: a warm directory costs no login.
# ---------------------------------------------------------------------------

def test_second_command_reuses_the_login():
    env = _env()
    login = cache.attach(env, "vm list")
    logins = _Logins(env)

    assert login.ensure(logins) == (True, "")
    assert login.ensure(logins) == (True, "")

    assert logins.count == 1, "a warm directory must not log in again"


def test_concurrent_cold_requests_log_in_once():
    """Two requests arriving together must not both run `az login` into one dir."""
    env = _env()
    login = cache.attach(env, "vm list")
    logins = _Logins(env, delay=0.05)
    results = []

    def worker():
        results.append(cache.attach(_env(), "vm list").ensure(logins))

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == [(True, "")] * 12
    assert logins.count == 1
    assert os.path.exists(login._marker)


def test_failed_login_leaves_nothing_behind():
    env = _env()
    login = cache.attach(env, "vm list")

    ok, stderr = login.ensure(_Logins(env, returncode=1, stderr="AADSTS7000215: invalid secret"))

    assert ok is False
    assert "AADSTS7000215" in stderr
    assert not os.path.exists(login.config_dir), "a failed login must not look cached"


# ---------------------------------------------------------------------------
# Isolation: the directory is a function of the credentials and nothing else.
# ---------------------------------------------------------------------------

def test_org_members_sharing_credentials_share_one_directory():
    assert cache.attach(_env(), "vm list").config_dir == cache.attach(_env(), "aks list").config_dir


@pytest.mark.parametrize("other", [
    {"secret": "rotated"},          # reconnect / rotated secret
    {"client": "client-readonly"},  # read-only vs agent service principal
    {"tenant": "tenant-b"},         # another organisation
])
def test_different_credentials_never_share_a_directory(other):
    assert cache.attach(_env(), "vm list").config_dir != cache.attach(_env(**other), "vm list").config_dir


def test_directory_name_does_not_expose_the_credentials():
    name = os.path.basename(cache.attach(_env(secret="hunter2"), "vm list").config_dir)
    assert "hunter2" not in name
    assert "client-a" not in name
    assert "tenant-a" not in name


def test_directory_name_depends_on_the_server_key(monkeypatch):
    before = cache.attach(_env(), "vm list").config_dir
    monkeypatch.setenv("FLASK_SECRET_KEY", "another-key")
    assert cache.attach(_env(), "vm list").config_dir != before


def test_cache_and_login_are_private_to_this_user(isolated_cache):
    env = _env()
    login = cache.attach(env, "vm list")
    login.ensure(_Logins(env))

    root = os.path.join(str(isolated_cache), cache.CACHE_DIR_NAME)
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(login.config_dir).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(login._marker).st_mode) == 0o600


def test_untrusted_cache_root_disables_caching(isolated_cache):
    """A root someone else could read or plant must not be used at all."""
    root = os.path.join(str(isolated_cache), cache.CACHE_DIR_NAME)
    os.makedirs(root, mode=0o755)
    os.chmod(root, 0o755)
    env = _env()
    private = env["AZURE_CONFIG_DIR"]

    assert cache.attach(env, "vm list") is None
    assert env["AZURE_CONFIG_DIR"] == private
    assert os.path.isdir(private)


def test_symlinked_cache_root_disables_caching(isolated_cache):
    elsewhere = os.path.join(str(isolated_cache), "elsewhere")
    os.makedirs(elsewhere, mode=0o700)
    os.symlink(elsewhere, os.path.join(str(isolated_cache), cache.CACHE_DIR_NAME))

    assert cache.attach(_env(), "vm list") is None


# ---------------------------------------------------------------------------
# attach: when the shared directory applies and when the private one stays.
# ---------------------------------------------------------------------------

def test_attach_swaps_the_private_directory_for_the_cached_one():
    env = _env()
    private = env["AZURE_CONFIG_DIR"]

    login = cache.attach(env, "vm list")

    assert env["AZURE_CONFIG_DIR"] == login.config_dir
    assert cache.is_cached_dir(login.config_dir)
    assert not os.path.exists(private), "the unused private dir must not leak"


def test_disabled_cache_keeps_the_private_directory(monkeypatch):
    monkeypatch.setattr(cache, "_IDLE_SECONDS", 0)
    env = _env()
    private = env["AZURE_CONFIG_DIR"]

    assert cache.attach(env, "vm list") is None
    assert env["AZURE_CONFIG_DIR"] == private
    assert os.path.isdir(private)
    assert not cache.is_cached_dir(private)


def test_incomplete_credentials_are_never_cached():
    env = _env()
    env["AZURE_CLIENT_SECRET"] = ""
    assert cache.attach(env, "vm list") is None


@pytest.mark.parametrize("setting", ["true", None])
def test_pod_isolation_disables_caching(monkeypatch, setting):
    """`az` runs in a per-session terminal pod there; a marker on this filesystem
    would vouch for a login the pod never performed. Unset means on, as in terminal_run."""
    if setting is None:
        monkeypatch.delenv("ENABLE_POD_ISOLATION")
    else:
        monkeypatch.setenv("ENABLE_POD_ISOLATION", setting)
    env = _env()
    private = env["AZURE_CONFIG_DIR"]

    assert cache.attach(env, "vm list") is None
    assert env["AZURE_CONFIG_DIR"] == private
    assert os.path.isdir(private)


@pytest.mark.parametrize("command", [
    "login --service-principal -u x -p y --tenant z",
    "az login",
    "az --output json login",          # global flag value ahead of the group
    "--output json --query x --subscription y login --service-principal -u a -p b --tenant t",
    "--output json --query x --subscription y account set -s z",
    "group show --name login",         # a value that looks like a group is treated as one
    "logout",
    "account set --subscription S",
    "az account clear",
    "config set core.output=json",
    "configure --defaults group=rg",
    "extension add --name foo",
    "cloud set --name AzureUSGovernment",  # writes the active cloud into the dir
    # az resolves command names case-insensitively; the guard must too.
    "az LOGIN --service-principal -u x -p y --tenant z",
    "Logout",
    "account Set -s S",
    "AZ Account clear",
    'vm list --query "unterminated',   # unparseable
])
def test_commands_that_write_cli_state_never_touch_a_shared_directory(command):
    """`az login` against a shared dir would swap the identity for every other user."""
    env = _env()
    private = env["AZURE_CONFIG_DIR"]

    assert cache.uses_local_cli_state(command)
    assert cache.attach(env, command) is None
    assert env["AZURE_CONFIG_DIR"] == private


@pytest.mark.parametrize("command", [
    "vm list",
    "az aks show --name c --resource-group rg",
    "account show",
    "account list -o table",
    "account get-access-token",
    'graph query -q "Resources | count"',
    "kubectl get pods -n default",
    "monitor log-analytics query -w W --analytics-query Q",
])
def test_ordinary_commands_use_the_cache(command):
    assert not cache.uses_local_cli_state(command)
    assert cache.attach(_env(), command) is not None


# ---------------------------------------------------------------------------
# Lifetime.
# ---------------------------------------------------------------------------

def test_idle_login_is_not_reused():
    env = _env()
    login = cache.attach(env, "vm list")
    logins = _Logins(env)
    login.ensure(logins)

    _age_marker(login, last_used=time.time() - cache._IDLE_SECONDS - 5)
    login.ensure(logins)

    assert logins.count == 2


def test_use_keeps_a_login_alive():
    env = _env()
    login = cache.attach(env, "vm list")
    logins = _Logins(env)
    login.ensure(logins)
    _age_marker(login, last_used=time.time() - cache._IDLE_SECONDS + 60)

    login.ensure(logins)

    assert logins.count == 1
    assert time.time() - os.stat(login._marker).st_mtime < 5, "reuse must refresh the idle clock"


def test_login_is_refreshed_after_the_maximum_age():
    env = _env()
    login = cache.attach(env, "vm list")
    logins = _Logins(env)
    login.ensure(logins)

    _age_marker(login, created=time.time() - cache._MAX_AGE_SECONDS - 5)
    login.ensure(logins)

    assert logins.count == 2


def test_sweep_removes_idle_logins_and_keeps_live_ones(monkeypatch):
    live_env, idle_env = _env(secret="live"), _env(secret="idle")
    live, idle = cache.attach(live_env, "vm list"), cache.attach(idle_env, "vm list")
    live.ensure(_Logins(live_env))
    idle.ensure(_Logins(idle_env))
    _age_marker(idle, last_used=time.time() - cache._IDLE_SECONDS - 5)

    monkeypatch.setattr(cache, "_last_sweep", 0.0)
    cache.attach(_env(secret="third"), "vm list")  # any Azure command sweeps

    assert os.path.isdir(live.config_dir)
    assert not os.path.exists(idle.config_dir), "an idle login must not keep its secret on disk"


def test_sweep_leaves_a_login_in_progress_alone(monkeypatch):
    """A directory with no marker yet may be a login that is still running."""
    env = _env(secret="in-progress")
    login = cache.attach(env, "vm list")
    os.makedirs(login.config_dir, mode=0o700)

    monkeypatch.setattr(cache, "_last_sweep", 0.0)
    cache.attach(_env(secret="other"), "vm list")
    assert os.path.isdir(login.config_dir)

    old = time.time() - cache._ORPHAN_GRACE_SECONDS - 5
    os.utime(login.config_dir, (old, old))
    monkeypatch.setattr(cache, "_last_sweep", 0.0)
    cache.attach(_env(secret="other"), "vm list")
    assert not os.path.exists(login.config_dir), "a crashed login must eventually be removed"


# ---------------------------------------------------------------------------
# Recovering from a cached login that turned out to be unusable.
# ---------------------------------------------------------------------------

def test_login_lost_errors_trigger_a_relogin():
    login = cache.attach(_env(), "vm list")
    assert login.should_relogin("ERROR: Please run 'az login' to setup account.")
    assert login.should_relogin("Could not retrieve credential from local cache for service principal x.")


@pytest.mark.parametrize("stderr", [
    "",
    "(ResourceNotFound) The Resource 'Microsoft.Compute/virtualMachines/x' was not found.",
    "(AuthorizationFailed) The client does not have authorization to perform action",
    # A bad secret fails the re-login too, and unrelated AADSTS errors (consent,
    # unknown resource) must not wipe a login other requests are using.
    "AADSTS7000215: Invalid client secret provided.",
    "AADSTS500011: The resource principal named https://x was not found",
])
def test_ordinary_failures_do_not_trigger_a_relogin(stderr):
    env = _env()
    login = cache.attach(env, "vm list")
    login.ensure(_Logins(env))
    assert not login.should_relogin(stderr)


def test_unknown_subscription_only_triggers_a_relogin_when_the_list_is_stale():
    """az snapshots subscriptions at login; a fresh snapshot means it is truly absent."""
    stderr = "The subscription of 'S9' doesn't exist in cloud 'AzureCloud'."
    env = _env()
    login = cache.attach(env, "vm list")
    login.ensure(_Logins(env))

    assert not login.should_relogin(stderr), "a fresh login would relogin on every call"

    _age_marker(login, created=time.time() - cache._SUBSCRIPTION_LIST_MIN_AGE - 5)
    assert login.should_relogin(stderr)


def test_relogin_replaces_the_login():
    env = _env()
    login = cache.attach(env, "vm list")
    logins = _Logins(env)
    login.ensure(logins)
    started = time.time()
    time.sleep(0.01)

    assert login.relogin(logins, started) == (True, "")
    assert logins.count == 2


def test_burst_of_failures_costs_one_relogin():
    """Every command that failed on the same dead login must share its replacement."""
    env = _env()
    login = cache.attach(env, "vm list")
    logins = _Logins(env, delay=0.05)
    login.ensure(logins)
    started = time.time()
    time.sleep(0.01)

    threads = [threading.Thread(target=login.relogin, args=(logins, started)) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert logins.count == 2, "one initial login plus exactly one relogin"
