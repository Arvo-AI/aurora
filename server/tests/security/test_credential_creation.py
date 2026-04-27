"""Credential-creation tests (key generation, passwd manipulation).

Verify that the signature matcher and org denylist block SSH key
generation, user/password changes, credential file reads, and
authorized_keys writes.
"""

import os
import sys
import types
from unittest.mock import patch

import pytest

from utils.security.signature_match import check_signature

from .conftest import deny_blocks

# RFC 5737 documentation-range IP used in test payloads (not a real host).
_TEST_IP = "198.51.100.1"


# ---------------------------------------------------------------------------
# 1. SSH key generation
# ---------------------------------------------------------------------------

class TestSSHKeyGeneration:
    """Agent must never be allowed to generate SSH keys on the host."""

    @pytest.mark.parametrize("cmd", [
        "ssh-keygen -t rsa -b 4096 -f /tmp/key -N ''",
        "ssh-keygen -t ed25519 -f /home/user/.ssh/id_ed25519",
        "ssh-keygen -t ecdsa",
        "/usr/bin/ssh-keygen -t rsa",
        "ssh-keygen -f /tmp/backdoor_key -N '' -q",
    ])
    def test_ssh_keygen_blocked_by_denylist(self, cmd):
        assert deny_blocks(cmd), f"ssh-keygen not denied: {cmd!r}"

    @pytest.mark.parametrize("cmd", [
        "ssh-copy-id user@target-host",
        f"ssh-copy-id -i /tmp/key.pub root@{_TEST_IP}",
    ])
    def test_ssh_copy_id_blocked_by_denylist(self, cmd):
        assert deny_blocks(cmd), f"ssh-copy-id not denied: {cmd!r}"


# ---------------------------------------------------------------------------
# 2. Authorized-keys manipulation
# ---------------------------------------------------------------------------

class TestAuthorizedKeysManipulation:
    """Appending to authorized_keys must be caught by the signature matcher."""

    @pytest.mark.parametrize("cmd", [
        "echo 'ssh-rsa AAAA...' >> /root/.ssh/authorized_keys",
        "cat /tmp/key.pub >> ~/.ssh/authorized_keys",
        "tee -a /home/user/.ssh/authorized_keys < /tmp/pubkey",
    ])
    def test_authorized_keys_append_blocked(self, cmd):
        v = check_signature(cmd)
        assert v.matched, f"authorized_keys append not caught: {cmd!r}"
        assert v.rule_id in ("persist-authkeys", "cred-ssh")


# ---------------------------------------------------------------------------
# 3. User / password management
# ---------------------------------------------------------------------------

class TestUserPasswordManagement:
    """Commands that create users or change passwords must be denied."""

    @pytest.mark.parametrize("cmd", [
        "useradd backdoor",
        "useradd -m -s /bin/bash attacker",
        "adduser --disabled-password evil",
        "usermod -aG sudo evil",
        "passwd root",
        "passwd --stdin evil",
        "visudo",
    ])
    def test_user_management_blocked(self, cmd):
        assert deny_blocks(cmd), f"User management not denied: {cmd!r}"


# ---------------------------------------------------------------------------
# 4. Credential file access
# ---------------------------------------------------------------------------

class TestCredentialFileAccess:
    """Reading sensitive credential files must be caught by the signature matcher."""

    @pytest.mark.parametrize("cmd,expected_rule", [
        ("cat /etc/shadow", "cred-shadow"),
        ("cat ~/.aws/credentials", "cred-aws"),
        ("cat ~/.ssh/id_rsa", "cred-ssh"),
        ("cat ~/.ssh/id_ed25519", "cred-ssh"),
        ("cat /var/run/secrets/kubernetes.io/serviceaccount/token", "cred-k8s-sa"),
    ])
    def test_credential_file_read_blocked(self, cmd, expected_rule):
        v = check_signature(cmd)
        assert v.matched, f"Credential file read not caught: {cmd!r}"
        assert v.rule_id == expected_rule


# ---------------------------------------------------------------------------
# 5. Crontab persistence (credential-adjacent)
# ---------------------------------------------------------------------------

class TestCrontabPersistence:
    """Crontab edits that could install persistent credential harvesters."""

    @pytest.mark.parametrize("cmd", [
        "crontab -e",
        "crontab -i",
    ])
    def test_crontab_edit_blocked(self, cmd):
        v = check_signature(cmd)
        assert v.matched, f"Crontab modification not caught: {cmd!r}"
        assert v.rule_id == "persist-crontab"


# ---------------------------------------------------------------------------
# 6. Environment sanitization blocks credential leakage
# ---------------------------------------------------------------------------

class TestEnvSanitization:
    """terminal_exec_tool strips secrets from the child-process environment."""

    @staticmethod
    def _stub_heavy_deps():
        """Stub transitive imports so terminal_exec_tool can load in CI
        without Flask, werkzeug, psycopg2, etc."""
        stubs = {}
        for mod_name in (
            "utils.terminal.terminal_run",
            "chat.backend.agent.tools.cloud_exec_tool",
            "chat.backend.agent.tools.iac_tool",
        ):
            if mod_name not in sys.modules:
                stubs[mod_name] = types.ModuleType(mod_name)
        return stubs

    def test_safe_env_keys_excludes_secrets(self):
        stubs = self._stub_heavy_deps()
        with patch.dict(sys.modules, stubs):
            from chat.backend.agent.tools.terminal_exec_tool import _SAFE_ENV_KEYS

        dangerous_keys = {
            "VAULT_TOKEN", "DATABASE_URL", "SECRET_KEY",
            "AWS_SECRET_ACCESS_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
            "AZURE_CLIENT_SECRET", "REDIS_URL", "API_SECRET",
        }
        leaked = dangerous_keys & _SAFE_ENV_KEYS
        assert not leaked, f"Secret keys must not be in _SAFE_ENV_KEYS: {leaked}"

    def test_build_sanitized_env_omits_secrets(self, monkeypatch):
        stubs = self._stub_heavy_deps()
        with patch.dict(sys.modules, stubs):
            from chat.backend.agent.tools.terminal_exec_tool import _build_sanitized_env

        monkeypatch.setenv("VAULT_TOKEN", "test-secret-token")
        monkeypatch.setenv("DATABASE_URL", "postgres://secret@localhost/db")
        sanitized = _build_sanitized_env()
        assert "VAULT_TOKEN" not in sanitized
        assert "DATABASE_URL" not in sanitized
