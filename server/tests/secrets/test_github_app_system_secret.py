"""Tests that GitHub App system secrets (private key + webhook secret) resolve
through whichever secrets backend is active — Vault OR AWS Secrets Manager —
instead of a hardcoded ``vault:`` reference.

Regression (DEV-1198): ``vault_keys`` previously hardcoded
``vault:kv/data/aurora/system/...`` refs and passed them to the active backend.
With ``SECRETS_BACKEND=aws_secrets_manager`` the AWS SM backend rejected the
``vault:`` prefix (``ValueError: bad prefix``), breaking GitHub App auth
entirely. These tests pin the backend-agnostic ``build_system_ref`` mapping and
the ``vault_keys`` read paths so the App key resolves on both backends.
"""

from unittest.mock import MagicMock

import pytest

from connectors.github_connector import vault_keys
from connectors.github_connector.vault_keys import GitHubAppConfigError
from utils.secrets.base import SecretsBackend
from utils.secrets.vault_backend import VaultSecretsBackend
from utils.secrets.aws_sm_backend import AWSSecretsManagerBackend

_PRIVATE_KEY_LOGICAL = "github-app/private-key"
_WEBHOOK_LOGICAL = "github-app/webhook-secret"
# Opaque stand-in for the key material the backend returns. The backend is
# mocked in these tests, so this is never parsed as a real PEM — kept
# intentionally non-PEM-shaped so secret scanners (gitleaks) don't flag it.
_FAKE_PEM = "test-github-app-private-key-material-0000"


@pytest.fixture(autouse=True)
def _clear_vault_keys_cache():
    """vault_keys caches secrets in module globals; reset around every test."""
    vault_keys.clear_cache()
    yield
    vault_keys.clear_cache()


def _mock_backend(*, available=True, ref="awssm:us-east-1:aurora/system/github-app/private-key", value=_FAKE_PEM):
    backend = MagicMock()
    backend.is_available.return_value = available
    backend.build_system_ref.return_value = ref
    backend.get_secret.return_value = value
    return backend


# ---------------------------------------------------------------------------
# build_system_ref mapping (per backend)
# ---------------------------------------------------------------------------


class TestBuildSystemRef:
    def test_vault_ref_matches_legacy_hardcoded_path(self, monkeypatch):
        """Vault ref is byte-identical to the old hardcoded constant — so
        existing Vault deployments see zero behavior change."""
        monkeypatch.setenv("VAULT_KV_MOUNT", "aurora")
        backend = VaultSecretsBackend()
        assert (
            backend.build_system_ref(_PRIVATE_KEY_LOGICAL)
            == "vault:kv/data/aurora/system/github-app/private-key"
        )
        assert (
            backend.build_system_ref(_WEBHOOK_LOGICAL)
            == "vault:kv/data/aurora/system/github-app/webhook-secret"
        )

    def test_vault_ref_honours_custom_mount(self, monkeypatch):
        monkeypatch.setenv("VAULT_KV_MOUNT", "custommount")
        backend = VaultSecretsBackend()
        assert backend.build_system_ref(_PRIVATE_KEY_LOGICAL) == (
            "vault:kv/data/custommount/system/github-app/private-key"
        )

    def test_aws_sm_ref_uses_system_prefix_and_region(self, monkeypatch):
        monkeypatch.setenv("AWS_SM_REGION", "us-east-1")
        backend = AWSSecretsManagerBackend()
        assert backend.build_system_ref(_PRIVATE_KEY_LOGICAL) == (
            "awssm:us-east-1:aurora/system/github-app/private-key"
        )

    def test_aws_sm_ref_roundtrips_through_parse_ref(self, monkeypatch):
        """A ref produced by build_system_ref must be parseable by the same
        backend (region matches, SecretId extracted verbatim) — this is exactly
        the path that used to fail with a hardcoded vault: ref."""
        monkeypatch.setenv("AWS_SM_REGION", "eu-west-2")
        backend = AWSSecretsManagerBackend()
        ref = backend.build_system_ref(_PRIVATE_KEY_LOGICAL)
        assert backend.can_handle_ref(ref)
        assert backend._parse_ref(ref) == "aurora/system/github-app/private-key"

    def test_base_default_raises_not_implemented(self):
        class _Bare(SecretsBackend):
            def store_secret(self, secret_name, secret_value, **kwargs):
                return ""

            def get_secret(self, secret_ref):
                return ""

            def delete_secret(self, secret_ref):
                return None

            def is_available(self):
                return True

        with pytest.raises(NotImplementedError):
            _Bare().build_system_ref(_PRIVATE_KEY_LOGICAL)


# ---------------------------------------------------------------------------
# get_app_private_key — backend read, no env fallback
# ---------------------------------------------------------------------------


class TestGetAppPrivateKey:
    def test_resolves_via_active_backend(self, monkeypatch):
        """AWS SM regression: builds the ref from the active backend and reads
        it — no longer raises on a vault: prefix."""
        backend = _mock_backend(value=_FAKE_PEM)
        monkeypatch.setattr(vault_keys, "get_secrets_backend", lambda: backend)

        assert vault_keys.get_app_private_key() == _FAKE_PEM
        backend.build_system_ref.assert_called_once_with("github-app/private-key")
        # The ref handed to get_secret must be exactly what build_system_ref
        # produced — unmodified — so the backend resolves the right secret.
        passed_ref = backend.get_secret.call_args.args[0]
        assert passed_ref == backend.build_system_ref.return_value

    def test_build_system_ref_error_wrapped_as_config_error(self, monkeypatch):
        """A backend whose build_system_ref raises (e.g. NotImplementedError)
        surfaces as GitHubAppConfigError, not the raw error type."""
        backend = _mock_backend()
        backend.build_system_ref.side_effect = NotImplementedError("no system secrets")
        monkeypatch.setattr(vault_keys, "get_secrets_backend", lambda: backend)

        with pytest.raises(GitHubAppConfigError):
            vault_keys.get_app_private_key()

    def test_no_env_fallback_for_private_key(self, monkeypatch):
        """Private key must come from the backend — GITHUB_APP_PRIVATE_KEY is
        intentionally NOT a fallback (scope: AWS SM read only)."""
        backend = _mock_backend(available=False)
        monkeypatch.setattr(vault_keys, "get_secrets_backend", lambda: backend)
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", _FAKE_PEM)  # must be ignored

        with pytest.raises(GitHubAppConfigError):
            vault_keys.get_app_private_key()

    def test_empty_backend_value_raises(self, monkeypatch):
        backend = _mock_backend(value="")
        monkeypatch.setattr(vault_keys, "get_secrets_backend", lambda: backend)
        with pytest.raises(GitHubAppConfigError):
            vault_keys.get_app_private_key()

    def test_result_is_cached(self, monkeypatch):
        backend = _mock_backend(value=_FAKE_PEM)
        monkeypatch.setattr(vault_keys, "get_secrets_backend", lambda: backend)
        vault_keys.get_app_private_key()
        vault_keys.get_app_private_key()
        backend.get_secret.assert_called_once()


# ---------------------------------------------------------------------------
# get_app_webhook_secret — backend read, env fallback preserved
# ---------------------------------------------------------------------------


class TestGetAppWebhookSecret:
    def test_resolves_via_active_backend(self, monkeypatch):
        backend = _mock_backend(
            ref="awssm:us-east-1:aurora/system/github-app/webhook-secret",
            value="whsec",
        )
        monkeypatch.setattr(vault_keys, "get_secrets_backend", lambda: backend)

        assert vault_keys.get_app_webhook_secret() == "whsec"
        backend.build_system_ref.assert_called_once_with("github-app/webhook-secret")

    def test_env_fallback_when_backend_unavailable(self, monkeypatch):
        """The pre-existing webhook env fallback still works when the backend
        has no value."""
        backend = _mock_backend(available=False)
        monkeypatch.setattr(vault_keys, "get_secrets_backend", lambda: backend)
        monkeypatch.setenv("GITHUB_APP_WEBHOOK_SECRET", "env-whsec")

        assert vault_keys.get_app_webhook_secret() == "env-whsec"

    def test_env_fallback_when_build_system_ref_unsupported(self, monkeypatch):
        """If the active backend can't build a system ref (raises mid-read), the
        webhook secret still falls back to the env var instead of propagating a
        non-GitHubAppConfigError and skipping the fallback."""
        backend = _mock_backend()  # available=True, so build_system_ref is reached
        backend.build_system_ref.side_effect = NotImplementedError("no system secrets")
        monkeypatch.setattr(vault_keys, "get_secrets_backend", lambda: backend)
        monkeypatch.setenv("GITHUB_APP_WEBHOOK_SECRET", "env-whsec")

        assert vault_keys.get_app_webhook_secret() == "env-whsec"

    def test_raises_when_neither_backend_nor_env(self, monkeypatch):
        backend = _mock_backend(available=False)
        monkeypatch.setattr(vault_keys, "get_secrets_backend", lambda: backend)
        for var in ("GITHUB_APP_WEBHOOK_SECRET", "GH_APP_WEBHOOK_SECRET", "GITHUB_WEBHOOK_SECRET"):
            monkeypatch.delenv(var, raising=False)

        with pytest.raises(GitHubAppConfigError):
            vault_keys.get_app_webhook_secret()
