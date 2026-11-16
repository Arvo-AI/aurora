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
    monkeypatch.setattr(subprocess_env, "_dumpable_cleared", subprocess_env.threading.Event())
    monkeypatch.setattr(subprocess_env.sys, "platform", "darwin")
    assert subprocess_env.deny_same_uid_proc_access() is False


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="prctl is Linux only")
def test_child_cannot_read_parent_environ_after_deny():
    # Run in a fresh interpreter: the flag is process-wide and can't be undone.
    script = """
import os, subprocess, sys
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


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="prctl is Linux only")
def test_forked_workers_inherit_the_flag():
    # A celery/gunicorn master sets the flag, then forks workers that run shell
    # commands. Neither the worker nor the master may be readable from that shell.
    script = """
import os, subprocess, sys
from utils.security.subprocess_env import deny_same_uid_proc_access
assert deny_same_uid_proc_access()
master = os.getpid()
pid = os.fork()
if pid == 0:
    # Each read is checked on its own: a PermissionError for both, nothing else.
    probe = (
        "import sys\\n"
        "try:\\n"
        "    open('/proc/%s/environ', 'rb').read()\\n"
        "    print('READABLE')\\n"
        "except PermissionError:\\n"
        "    print('DENIED')\\n"
    )
    results = []
    for target in (master, os.getpid()):
        out = subprocess.run([sys.executable, "-c", probe % target], capture_output=True, text=True,
                             env={"PATH": os.environ["PATH"]})
        results.append(out.stdout.strip() or "ERROR:" + out.stderr.strip()[-200:])
    sys.stdout.write(",".join(results)); sys.stdout.flush()
    os._exit(0)
os.waitpid(pid, 0)
"""
    with open("/proc/self/status") as status:
        cap_eff = next(int(line.split()[1], 16) for line in status if line.startswith("CapEff:"))
    if cap_eff & (1 << 19):
        pytest.skip("CAP_SYS_PTRACE is held")
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                         cwd=subprocess_env.__file__.rsplit("/utils/", 1)[0])
    assert out.stdout.strip() == "DENIED,DENIED", out.stderr


def test_celery_master_sets_the_flag_before_forking():
    from unittest.mock import Mock

    import celery_config

    signal = getattr(celery_config, "worker_init", None)
    # tests/conftest.py stubs celery with a MagicMock when it isn't installed.
    if signal is None or isinstance(signal, Mock):
        pytest.skip("celery is stubbed in this environment")
    receivers = [r[1]() for r in celery_config.worker_init.receivers]
    assert celery_config._deny_proc_access in receivers


def test_mcp_base_env_drops_npm_credentials(monkeypatch):
    monkeypatch.setattr(subprocess_env.os, "environ", {
        "NPM_CONFIG_REGISTRY": "https://npm.internal",
        "npm_config_cache": "/tmp/npm",
        "NPM_CONFIG__AUTH": "secret",
        "NPM_CONFIG__AUTHTOKEN": "secret",
        "npm_config__authtoken": "secret",
        "GITHUB_READ_ONLY": "1",
        "AWS_ENDPOINT_URL_S3": "https://s3.internal",
    })
    assert mcp_base_env() == {
        "NPM_CONFIG_REGISTRY": "https://npm.internal",
        "npm_config_cache": "/tmp/npm",
        "GITHUB_READ_ONLY": "1",
        "AWS_ENDPOINT_URL_S3": "https://s3.internal",
    }
