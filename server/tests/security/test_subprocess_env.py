import subprocess
import sys

import pytest

from utils.security import subprocess_env
from utils.security.subprocess_env import (
    GITHUB_APP_SECRET_ENV_VARS,
    mcp_base_env,
    strip_github_app_secrets,
)


def test_strip_github_app_secrets_removes_every_name():
    env = {name: "secret" for name in GITHUB_APP_SECRET_ENV_VARS}
    env["PATH"] = "/usr/bin"
    assert strip_github_app_secrets(env) == {"PATH": "/usr/bin"}


def test_mcp_base_env_keeps_only_allowlisted_keys(monkeypatch):
    monkeypatch.setattr(subprocess_env.os, "environ", {
        "PATH": "/usr/bin",
        "HOME": "/home/app",
        "HTTPS_PROXY": "http://proxy:3128",
        "NPM_CONFIG_REGISTRY": "https://npm.internal",
        "GITHUB_APP_PRIVATE_KEY": "pem",
        "VAULT_TOKEN": "hvs.x",
        "POSTGRES_PASSWORD": "pw",
        "OPENAI_API_KEY": "sk",
        "AWS_SECRET_ACCESS_KEY": "aws",
    })
    assert mcp_base_env() == {
        "PATH": "/usr/bin",
        "HOME": "/home/app",
        "HTTPS_PROXY": "http://proxy:3128",
        "NPM_CONFIG_REGISTRY": "https://npm.internal",
    }


def test_deny_same_uid_proc_access_is_noop_off_linux(monkeypatch):
    monkeypatch.setattr(subprocess_env, "_dumpable_cleared", False)
    monkeypatch.setattr(subprocess_env.sys, "platform", "darwin")
    assert subprocess_env.deny_same_uid_proc_access() is False


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="prctl is Linux only")
def test_child_cannot_read_parent_environ_after_deny():
    # Run in a fresh interpreter: the flag is process-wide and can't be undone.
    script = """
import os, subprocess, sys
os.environ["MARKER_SECRET"] = "x"
from utils.security.subprocess_env import deny_same_uid_proc_access
assert deny_same_uid_proc_access()
child = "open('/proc/%d/environ','rb').read()" % os.getpid()
r = subprocess.run([sys.executable, "-c", child], capture_output=True, env={"PATH": os.environ["PATH"]})
print("DENIED" if r.returncode != 0 and b"PermissionError" in r.stderr else "READABLE")
"""
    with open("/proc/self/status") as status:
        cap_eff = next(int(line.split()[1], 16) for line in status if line.startswith("CapEff:"))
    if cap_eff & (1 << 19):  # CAP_SYS_PTRACE bypasses the non-dumpable check
        pytest.skip("CAP_SYS_PTRACE is held")
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                         cwd=subprocess_env.__file__.rsplit("/utils/", 1)[0])
    assert out.stdout.strip() == "DENIED", out.stderr
