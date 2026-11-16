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

import base64
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


def _generate_test_pem() -> str:
    """A throwaway RSA key made at test time, so no key material is committed."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode().strip()


_FAKE_PEM_TEXT = _generate_test_pem()


def _pem_block(label: str, body: str) -> str:
    """Build a PEM-shaped string at runtime so the source holds no literal key
    block for secret scanners (gitleaks) to match."""
    dashes = "-" * 5
    return f"{dashes}BEGIN {label}{dashes}\\n{body}\\n{dashes}END {label}{dashes}"


_ENV_VARS = (
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_APP_WEBHOOK_SECRET",
    "GH_APP_WEBHOOK_SECRET",
    "GITHUB_WEBHOOK_SECRET",
)


@pytest.fixture(autouse=True)
def _no_github_app_env(monkeypatch):
    """Start every test with none of the env vars set, whatever the host has."""
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


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
# get_app_private_key — env var if set, else the secrets backend
# ---------------------------------------------------------------------------


def _backend_must_not_be_called():
    raise AssertionError("secrets backend must not be consulted when the env var is set")


class TestGetAppPrivateKeyFromBackend:
    """GITHUB_APP_PRIVATE_KEY unset: the active secrets backend is used."""

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

    def test_backend_value_is_cached(self, monkeypatch):
        backend = _mock_backend(value=_FAKE_PEM)
        monkeypatch.setattr(vault_keys, "get_secrets_backend", lambda: backend)

        vault_keys.get_app_private_key()
        vault_keys.get_app_private_key()
        backend.get_secret.assert_called_once()

    def test_build_system_ref_error_wrapped_as_config_error(self, monkeypatch):
        """A backend whose build_system_ref raises (e.g. NotImplementedError)
        surfaces as GitHubAppConfigError, not the raw error type."""
        backend = _mock_backend()
        backend.build_system_ref.side_effect = NotImplementedError("no system secrets")
        monkeypatch.setattr(vault_keys, "get_secrets_backend", lambda: backend)

        with pytest.raises(GitHubAppConfigError):
            vault_keys.get_app_private_key()

    def test_backend_unavailable_raises_clear_error(self, monkeypatch):
        backend = _mock_backend(available=False)
        monkeypatch.setattr(vault_keys, "get_secrets_backend", lambda: backend)

        with pytest.raises(GitHubAppConfigError, match="unavailable") as info:
            vault_keys.get_app_private_key()
        assert "GITHUB_APP_PRIVATE_KEY" in str(info.value)
        assert "private_key" not in vault_keys._cache

    def test_backend_read_error_raises_clear_error(self, monkeypatch):
        backend = _mock_backend()
        backend.get_secret.side_effect = RuntimeError("boom")
        monkeypatch.setattr(vault_keys, "get_secrets_backend", lambda: backend)

        with pytest.raises(GitHubAppConfigError, match="Failed to read"):
            vault_keys.get_app_private_key()

    def test_invalid_backend_setting_raises_clear_error(self, monkeypatch):
        def _bad_backend():
            raise ValueError("unknown SECRETS_BACKEND")

        monkeypatch.setattr(vault_keys, "get_secrets_backend", _bad_backend)

        with pytest.raises(GitHubAppConfigError, match="misconfigured"):
            vault_keys.get_app_private_key()

    def test_permission_denied_names_the_path(self, monkeypatch):
        class Forbidden(Exception):
            pass

        ref = "vault:kv/data/aurora/system/github-app/private-key"
        backend = _mock_backend(ref=ref)
        backend.get_secret.side_effect = Forbidden("permission denied")
        monkeypatch.setattr(vault_keys, "get_secrets_backend", lambda: backend)

        with pytest.raises(GitHubAppConfigError, match="denied access") as info:
            vault_keys.get_app_private_key()
        assert ref in str(info.value)

    def test_empty_backend_value_raises(self, monkeypatch):
        backend = _mock_backend(value="")
        monkeypatch.setattr(vault_keys, "get_secrets_backend", lambda: backend)

        with pytest.raises(GitHubAppConfigError, match="empty"):
            vault_keys.get_app_private_key()

    def test_blank_env_var_counts_as_unset(self, monkeypatch):
        backend = _mock_backend(value=_FAKE_PEM)
        monkeypatch.setattr(vault_keys, "get_secrets_backend", lambda: backend)
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "   ")

        assert vault_keys.get_app_private_key() == _FAKE_PEM


class TestGetAppPrivateKeyFromEnv:
    """GITHUB_APP_PRIVATE_KEY set: it is used and the backend is never called."""

    @pytest.fixture(autouse=True)
    def _forbid_backend(self, monkeypatch):
        monkeypatch.setattr(vault_keys, "get_secrets_backend", _backend_must_not_be_called)

    def test_pem_text(self, monkeypatch):
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", _FAKE_PEM_TEXT)

        assert vault_keys.get_app_private_key() == _FAKE_PEM_TEXT

    def test_pem_with_escaped_newlines(self, monkeypatch):
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", _FAKE_PEM_TEXT.replace("\n", "\\n"))

        assert vault_keys.get_app_private_key() == _FAKE_PEM_TEXT

    def test_base64(self, monkeypatch):
        encoded = base64.b64encode(_FAKE_PEM_TEXT.encode()).decode()
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", encoded)

        assert vault_keys.get_app_private_key() == _FAKE_PEM_TEXT

    def test_base64_wrapped(self, monkeypatch):
        """Plain `base64 key.pem` wraps at 76 columns; the wrapped form decodes."""
        encoded = base64.encodebytes(_FAKE_PEM_TEXT.encode()).decode()
        assert "\n" in encoded.strip()
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", encoded)

        assert vault_keys.get_app_private_key() == _FAKE_PEM_TEXT

    def test_env_wins_over_a_working_backend(self, monkeypatch):
        """A key later written to the backend cannot override the env var."""
        backend = _mock_backend(value="stale-backend-key")
        monkeypatch.setattr(vault_keys, "get_secrets_backend", lambda: backend)
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", _FAKE_PEM_TEXT)

        assert vault_keys.get_app_private_key() == _FAKE_PEM_TEXT
        backend.is_available.assert_not_called()
        backend.get_secret.assert_not_called()

    def test_env_value_is_cached_and_source_logged_once(self, monkeypatch, caplog):
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", _FAKE_PEM_TEXT)
        normalize = MagicMock(side_effect=vault_keys._normalize_private_key)
        monkeypatch.setattr(vault_keys, "_normalize_private_key", normalize)

        with caplog.at_level("INFO", logger=vault_keys.logger.name):
            for _ in range(3):
                assert vault_keys.get_app_private_key() == _FAKE_PEM_TEXT

        normalize.assert_called_once()
        source_logs = [r for r in caplog.records if "loaded from" in r.getMessage()]
        assert len(source_logs) == 1
        assert source_logs[0].levelname == "INFO"
        assert "environment (GITHUB_APP_PRIVATE_KEY)" in source_logs[0].getMessage()
        assert all("PRIVATE KEY" not in r.getMessage() for r in caplog.records)
        assert all("WARNING" != r.levelname for r in caplog.records)

    def test_value_that_is_not_a_key_raises(self, monkeypatch):
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "not-a-key")

        with pytest.raises(GitHubAppConfigError, match="neither a PEM key nor base64"):
            vault_keys.get_app_private_key()

    @pytest.mark.parametrize(
        "value",
        [
            "-----BEGIN",
            _pem_block("PRIVATE KEY", "MIIEvQIBADANBgkq"),
            _pem_block("PUBLIC KEY", "MFkwEwYHKoZIzj0CAQ"),
        ],
        ids=["marker-only", "truncated-body", "not-a-private-key"],
    )
    def test_value_that_does_not_parse_raises(self, monkeypatch, value):
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", value)

        with pytest.raises(GitHubAppConfigError, match="valid unencrypted PEM private key"):
            vault_keys.get_app_private_key()
        assert "private_key" not in vault_keys._cache


# ---------------------------------------------------------------------------
# get_app_webhook_secret — env var if set, else the secrets backend
# ---------------------------------------------------------------------------


class TestGetAppWebhookSecret:
    def test_resolves_via_active_backend(self, monkeypatch):
        backend = _mock_backend(
            ref="awssm:us-east-1:aurora/system/github-app/webhook-secret",
            value="whsec",
        )
        monkeypatch.setattr(vault_keys, "get_secrets_backend", lambda: backend)

        assert vault_keys.get_app_webhook_secret() == "whsec"
        assert vault_keys.get_app_webhook_secret() == "whsec"
        backend.build_system_ref.assert_called_once_with("github-app/webhook-secret")
        backend.get_secret.assert_called_once()

    @pytest.mark.parametrize(
        "env_var", ["GITHUB_APP_WEBHOOK_SECRET", "GH_APP_WEBHOOK_SECRET", "GITHUB_WEBHOOK_SECRET"]
    )
    def test_env_var_set_never_calls_backend(self, monkeypatch, env_var):
        monkeypatch.setattr(vault_keys, "get_secrets_backend", _backend_must_not_be_called)
        monkeypatch.setenv(env_var, "env-whsec")

        assert vault_keys.get_app_webhook_secret() == "env-whsec"
        assert vault_keys.get_app_webhook_secret() == "env-whsec"

    def test_first_set_env_var_wins(self, monkeypatch):
        monkeypatch.setattr(vault_keys, "get_secrets_backend", _backend_must_not_be_called)
        monkeypatch.setenv("GITHUB_APP_WEBHOOK_SECRET", "primary")
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "legacy")

        assert vault_keys.get_app_webhook_secret() == "primary"

    def test_env_value_trailing_newline_is_stripped(self, monkeypatch):
        monkeypatch.setattr(vault_keys, "get_secrets_backend", _backend_must_not_be_called)
        monkeypatch.setenv("GITHUB_APP_WEBHOOK_SECRET", "env-whsec\n")

        assert vault_keys.get_app_webhook_secret() == "env-whsec"

    def test_backend_failure_with_no_env_raises_clear_error(self, monkeypatch):
        backend = _mock_backend(available=False)
        monkeypatch.setattr(vault_keys, "get_secrets_backend", lambda: backend)

        with pytest.raises(GitHubAppConfigError, match="unavailable") as info:
            vault_keys.get_app_webhook_secret()
        assert "GITHUB_APP_WEBHOOK_SECRET" in str(info.value)

    def test_build_system_ref_unsupported_raises_config_error(self, monkeypatch):
        backend = _mock_backend()
        backend.build_system_ref.side_effect = NotImplementedError("no system secrets")
        monkeypatch.setattr(vault_keys, "get_secrets_backend", lambda: backend)

        with pytest.raises(GitHubAppConfigError):
            vault_keys.get_app_webhook_secret()
