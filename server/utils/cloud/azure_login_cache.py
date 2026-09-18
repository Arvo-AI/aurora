"""Reuse one `az login` per set of Azure credentials instead of one per command.

The `az` CLI cannot take a token from the environment the way `aws` and `gcloud`
can, so every Azure command used to pay for a full `az login` subprocess first.
This module keeps the logged-in AZURE_CONFIG_DIR and hands it to later commands.

Isolation model:
  * The directory name is derived from the credentials themselves (tenant, client
    id, client secret). A caller can only reach a directory by already holding the
    exact credentials stored in it, and those are resolved per request through the
    normal Vault/RBAC/org lookup. The cache never decides who may use credentials;
    it only skips repeating a login that was going to succeed anyway.
  * Org members sharing one connection share one login. Different orgs, the
    read-only vs agent service principals, and a rotated secret all land in
    different directories.
  * Commands that mutate the CLI's own state (`az login`, `az account set`, ...)
    never run against a cached directory; the caller keeps its private one.
  * Directories are 0700, expire after an idle window, and are force-refreshed
    after a maximum age. `az` stores the client secret in plaintext inside the
    directory and refuses to run without it, so the idle window bounds how long
    the secret rests on disk.

Each container has its own cache; nothing is shared across containers. With pod
isolation on, `az` runs in a per-session terminal pod whose filesystem this
module cannot see, so the cache is disabled there and every command logs in.
"""

import fcntl
import hashlib
import hmac
import json
import logging
import os
import re
import shlex
import shutil
import stat
import tempfile
import time
from contextlib import contextmanager
from typing import Callable, Optional, Tuple

logger = logging.getLogger(__name__)

CACHE_DIR_NAME = "aurora-az-login-cache"
_MARKER = ".aurora-login"
_DEFAULT_IDLE_SECONDS = 1800
_MAX_AGE_SECONDS = 8 * 3600
_SWEEP_INTERVAL_SECONDS = 300
# A directory with no marker is a login that crashed midway; leave a live one alone.
_ORPHAN_GRACE_SECONDS = 600
# `az` snapshots the subscription list at login. A "subscription doesn't exist"
# error only justifies a re-login when that snapshot is old enough to be stale,
# otherwise an inaccessible subscription would force a login on every call.
_SUBSCRIPTION_LIST_MIN_AGE = 300

_LOGIN_LOST = re.compile(
    r"Please run 'az login'|Could not retrieve credential from local cache", re.IGNORECASE
)
_SUBSCRIPTION_UNKNOWN = re.compile(
    r"doesn't exist in cloud|No subscriptions? found", re.IGNORECASE
)

# `az <group> [<sub>]` commands that write the CLI's own state. Run against a
# shared directory they would change or destroy the login for every other user.
# `cloud` (set/register/update) writes the active cloud into the directory's config.
_LOCAL_STATE_GROUPS = {"login", "logout", "config", "configure", "extension", "cache", "upgrade", "init", "interactive", "cloud"}
_LOCAL_STATE_ACCOUNT_SUBCOMMANDS = {"set", "clear"}

_last_sweep = 0.0


def idle_seconds() -> int:
    """Idle window in seconds. 0 disables the cache entirely."""
    raw = os.getenv("AZURE_LOGIN_CACHE_IDLE_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_IDLE_SECONDS
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("AZURE_LOGIN_CACHE_IDLE_SECONDS is not an integer; using %d", _DEFAULT_IDLE_SECONDS)
        return _DEFAULT_IDLE_SECONDS


def _root() -> Optional[str]:
    """Return the private cache root, or None when it cannot be trusted."""
    root = os.path.join(tempfile.gettempdir(), CACHE_DIR_NAME)
    try:
        os.makedirs(root, mode=0o700, exist_ok=True)
        st = os.lstat(root)
    except OSError as e:
        logger.warning("Azure login cache root unavailable: %s", e)
        return None
    # lstat, not stat: a symlink planted at this path must not redirect the cache.
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        logger.warning("Azure login cache root is not a private directory owned by this user; caching disabled")
        return None
    return root


def is_cached_dir(path: Optional[str]) -> bool:
    """True when path lives under the cache root and so must outlive the command."""
    if not path:
        return False
    root = os.path.join(tempfile.gettempdir(), CACHE_DIR_NAME)
    return os.path.realpath(path).startswith(os.path.realpath(root) + os.sep)


def uses_local_cli_state(command: str) -> bool:
    """True for `az` commands that write the CLI's own state rather than Azure's."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return True  # unparseable: keep it away from a shared directory
    # az resolves command names case-insensitively (`az Account Set` runs
    # `account set`), so the comparison must too.
    if tokens and tokens[0].lower() == "az":
        tokens = tokens[1:]
    words = [t.lower() for t in tokens if not t.startswith("-")]
    # Deliberately loose: any number of global flag values can sit before the
    # group (`az --output json --query x --subscription y login`), so every word is
    # checked. A false positive only costs one private login; a false negative
    # would let a command replace the identity in a shared directory.
    if any(w in _LOCAL_STATE_GROUPS for w in words):
        return True
    return any(
        a == "account" and b in _LOCAL_STATE_ACCOUNT_SUBCOMMANDS
        for a, b in zip(words, words[1:])
    )


def _pod_isolation_enabled() -> bool:
    """True when terminal_run executes commands in a per-session terminal pod.

    Same default as terminal_run: unset means on. The directory `az` logs into
    then lives in the pod, not on this filesystem, so a marker here would vouch
    for a login the pod never performed.
    """
    return os.getenv("ENABLE_POD_ISOLATION", "true") == "true"


def _key(env: dict) -> Optional[str]:
    parts = [env.get("AZURE_TENANT_ID"), env.get("AZURE_CLIENT_ID"), env.get("AZURE_CLIENT_SECRET")]
    if not all(parts):
        return None
    # Keyed so a directory listing is not an offline-checkable function of the secret.
    server_key = os.getenv("FLASK_SECRET_KEY") or CACHE_DIR_NAME
    message = "\0".join(str(p) for p in parts).encode("utf-8")
    return hmac.new(server_key.encode("utf-8"), message, hashlib.sha256).hexdigest()[:40]


@contextmanager
def _locked(lock_path: str, blocking: bool = True):
    """Exclusive flock. Works across threads and processes in one container.

    Lock files are never unlinked: removing one while another process holds or
    awaits it would let two holders lock different inodes for the same key.
    """
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class CachedLogin:
    """A reusable logged-in AZURE_CONFIG_DIR for one set of credentials."""

    def __init__(self, root: str, key: str, idle: int):
        self.config_dir = os.path.join(root, key)
        self._lock_path = os.path.join(root, key + ".lock")
        self._marker = os.path.join(self.config_dir, _MARKER)
        self._idle = idle

    def _created_at(self) -> Optional[float]:
        """Login time when the directory holds a live login, else None."""
        try:
            with open(self._marker, encoding="utf-8") as fh:
                created = float(json.load(fh)["created"])
            last_used = os.stat(self._marker).st_mtime
        except (OSError, ValueError, KeyError, TypeError):
            return None
        now = time.time()
        if now - created > _MAX_AGE_SECONDS or now - last_used > self._idle:
            return None
        return created

    def _touch(self) -> None:
        try:
            os.utime(self._marker)
        except OSError:
            pass  # swept since the check; the command's own failure triggers relogin

    def _login(self, run_login: Callable[[], object]) -> Tuple[bool, str]:
        """Wipe and log in. Caller must hold the lock."""
        shutil.rmtree(self.config_dir, ignore_errors=True)
        os.makedirs(self.config_dir, mode=0o700, exist_ok=True)
        started = time.perf_counter()
        result = run_login()
        logger.info("TIME: az login took %.2fs", time.perf_counter() - started)
        if result.returncode != 0:
            shutil.rmtree(self.config_dir, ignore_errors=True)
            return False, (result.stderr or "")
        fd = os.open(self._marker, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"created": time.time()}, fh)
        return True, ""

    def ensure(self, run_login: Callable[[], object]) -> Tuple[bool, str]:
        """Make sure the directory is logged in. Logs in only when it is cold.

        run_login must run the `az login` argv against this config_dir and return
        an object with returncode and stderr. Returns (ok, login_stderr).
        """
        if self._created_at() is not None:
            self._touch()
            logger.info("Azure login reused from cache")
            return True, ""
        with _locked(self._lock_path):
            # Another request may have logged in while this one waited.
            if self._created_at() is not None:
                self._touch()
                return True, ""
            return self._login(run_login)

    def should_relogin(self, stderr: str) -> bool:
        """True when a command's failure means the cached login itself is unusable."""
        if not stderr:
            return False
        if _LOGIN_LOST.search(stderr):
            return True
        if _SUBSCRIPTION_UNKNOWN.search(stderr):
            created = self._created_at()
            return created is None or time.time() - created > _SUBSCRIPTION_LIST_MIN_AGE
        return False

    def relogin(self, run_login: Callable[[], object], command_started_at: float) -> Tuple[bool, str]:
        """Replace a login that a command just proved unusable.

        command_started_at is time.time() from before the failed command. If some
        other request already re-logged in after that, its login is reused, so a
        burst of failures costs one login rather than one each.
        """
        with _locked(self._lock_path):
            created = self._created_at()
            if created is not None and created > command_started_at:
                return True, ""
            return self._login(run_login)


def attach(env: dict, command: str) -> Optional[CachedLogin]:
    """Point env at the cached login directory for its credentials, if one applies.

    env is the dict from setup_azure_environment_isolated and already holds a
    private, empty AZURE_CONFIG_DIR. When caching applies that private directory
    is removed and env is repointed at the shared one. Returns None when the
    caller should carry on with its private directory and a per-command login.
    """
    idle = idle_seconds()
    if idle <= 0 or _pod_isolation_enabled() or uses_local_cli_state(command):
        return None
    key = _key(env)
    root = _root() if key else None
    if not root:
        return None
    _sweep(root, idle)
    private_dir = env.get("AZURE_CONFIG_DIR")
    if private_dir and not is_cached_dir(private_dir):
        shutil.rmtree(private_dir, ignore_errors=True)
    login = CachedLogin(root, key, idle)
    env["AZURE_CONFIG_DIR"] = login.config_dir
    return login


def _sweep(root: str, idle: int) -> None:
    """Delete idle, over-age and orphaned directories. Throttled per process."""
    global _last_sweep
    now = time.time()
    if now - _last_sweep < _SWEEP_INTERVAL_SECONDS:
        return
    _last_sweep = now
    try:
        entries = [e for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return
    for entry in entries:
        login = CachedLogin(root, entry.name, idle)
        if login._created_at() is not None:
            continue
        with _locked(login._lock_path, blocking=False) as held:
            if not held:
                continue  # a login is in progress
            if login._created_at() is not None:
                continue
            try:
                age = now - entry.stat(follow_symlinks=False).st_mtime
            except OSError:
                continue
            if os.path.exists(login._marker) or age > _ORPHAN_GRACE_SECONDS:
                shutil.rmtree(entry.path, ignore_errors=True)
                logger.info("Removed expired Azure login cache directory")
