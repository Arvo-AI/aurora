"""Environment hygiene for child processes that must not see server secrets."""

import ctypes
import ctypes.util
import logging
import os
import sys
import threading
from typing import Dict, MutableMapping

logger = logging.getLogger(__name__)

# Env vars that hold GitHub App secrets. With the private key a holder can mint
# installation tokens for every installation of the App.
GITHUB_APP_SECRET_ENV_VARS = (
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_APP_WEBHOOK_SECRET",
    "GH_APP_WEBHOOK_SECRET",
    "GITHUB_WEBHOOK_SECRET",
)

# Non-secret variables an MCP server needs to start, resolve binaries and reach
# the network. Everything else (VAULT_TOKEN, DB password, LLM keys, ...) is dropped.
_MCP_SAFE_ENV_KEYS = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "PWD", "HOSTNAME",
    "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    "TMPDIR", "TEMP", "TMP",
    "PYTHONPATH", "UV_CACHE_DIR",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
})
# npm registry and cache settings, needed by npx-based servers on air-gapped installs.
_MCP_SAFE_ENV_PREFIXES = ("NPM_CONFIG_", "npm_config_")

_PR_SET_DUMPABLE = 4
_dumpable_lock = threading.Lock()
_dumpable_cleared = False


def strip_github_app_secrets(env: MutableMapping[str, str]) -> MutableMapping[str, str]:
    """Remove the GitHub App secrets from ``env`` in place and return it."""
    for name in GITHUB_APP_SECRET_ENV_VARS:
        env.pop(name, None)
    return env


def mcp_base_env() -> Dict[str, str]:
    """Allowlisted copy of the server environment for an MCP server subprocess.

    Callers add the per-user credentials the server needs on top of this.
    """
    return {
        key: value
        for key, value in os.environ.items()
        if key in _MCP_SAFE_ENV_KEYS or key.startswith(_MCP_SAFE_ENV_PREFIXES)
    }


def deny_same_uid_proc_access() -> bool:
    """Mark this process non-dumpable so same-uid children cannot read it.

    A child running as the same uid can otherwise read ``/proc/<ppid>/environ``
    (or ptrace the parent) and recover every secret the parent was started
    with, whatever env the child itself was given. A non-dumpable process's
    ``/proc`` entries need CAP_SYS_PTRACE, which containers don't grant by
    default. The flag resets on ``execve`` in children, so it only covers this
    process. It also disables core dumps and non-root debugger attach.

    Linux only; returns False elsewhere or if the call fails.
    """
    global _dumpable_cleared
    if _dumpable_cleared:
        return True
    if not sys.platform.startswith("linux"):
        return False
    with _dumpable_lock:
        if _dumpable_cleared:
            return True
        try:
            libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
            if libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
                logger.warning("Could not mark process non-dumpable (errno %s)", ctypes.get_errno())
                return False
        except Exception as exc:
            logger.warning("Could not mark process non-dumpable: %s", type(exc).__name__)
            return False
        _dumpable_cleared = True
        logger.info("Process marked non-dumpable before starting MCP servers")
        return True
