"""Pins the Vault backend's initialization retry behaviour.

The backend is a process-wide singleton, so whatever it concludes about Vault's
availability sticks for every later lookup. It used to latch ``_available =
False`` on the *first* attempt, which meant a pod that happened to touch Vault
while it was unreachable, sealed, or holding an expired token kept failing every
credential lookup until it was restarted — long after Vault itself recovered.
Worse, the failure surfaced to users as a cloud-provider authentication error,
sending operators after credentials that were never the problem.

These tests fix the contract: success latches, failure retries.
"""

import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from utils.secrets.vault_backend import VaultSecretsBackend


@pytest.fixture
def vault_env(monkeypatch):
    monkeypatch.setenv("VAULT_TOKEN", "test-token")
    monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")


def _client(authenticated: bool) -> MagicMock:
    client = MagicMock()
    client.is_authenticated.return_value = authenticated
    client.sys.list_mounted_secrets_engines.return_value = {"aurora/": {}}
    return client


class TestAvailabilityIsRetried:
    def test_unauthenticated_vault_is_retried_and_recovers(self, vault_env):
        """The actual bug: a sealed/expired-token Vault must not disable the
        backend for the life of the process."""
        backend = VaultSecretsBackend()
        backend.RETRY_INTERVAL_SECONDS = 0

        with patch("hvac.Client", return_value=_client(False)):
            assert backend.is_available() is False

        # Vault gets unsealed / the token is rotated — no restart involved.
        with patch("hvac.Client", return_value=_client(True)):
            assert backend.is_available() is True, (
                "backend stayed unavailable after Vault recovered"
            )

    def test_connection_error_is_retried_and_recovers(self, vault_env):
        """An exception during init must be retried, not latched."""
        backend = VaultSecretsBackend()
        backend.RETRY_INTERVAL_SECONDS = 0

        with patch("hvac.Client", side_effect=ConnectionError("vault down")):
            assert backend.is_available() is False

        with patch("hvac.Client", return_value=_client(True)):
            assert backend.is_available() is True

    def test_missing_token_is_retried_once_provided(self, vault_env, monkeypatch):
        """VAULT_TOKEN is read per attempt, so a later-mounted secret is picked up."""
        monkeypatch.delenv("VAULT_TOKEN", raising=False)
        backend = VaultSecretsBackend()
        backend.RETRY_INTERVAL_SECONDS = 0

        assert backend.is_available() is False

        monkeypatch.setenv("VAULT_TOKEN", "now-present")
        with patch("hvac.Client", return_value=_client(True)):
            assert backend.is_available() is True

    def test_failures_are_rate_limited(self, vault_env):
        """Retrying is not the same as reconnecting on every lookup: a real outage
        must not turn each credential read into a fresh connection attempt."""
        backend = VaultSecretsBackend()
        backend.RETRY_INTERVAL_SECONDS = 300

        with patch("hvac.Client", return_value=_client(False)) as ctor:
            for _ in range(10):
                assert backend.is_available() is False
            assert ctor.call_count == 1, (
                f"expected 1 attempt within the backoff window, got {ctor.call_count}"
            )

    def test_backoff_window_expiry_allows_another_attempt(self, vault_env):
        backend = VaultSecretsBackend()
        backend.RETRY_INTERVAL_SECONDS = 300

        with patch("hvac.Client", return_value=_client(False)):
            assert backend.is_available() is False

        # Pretend the backoff window has elapsed.
        backend._last_failure_at = time.monotonic() - 301

        with patch("hvac.Client", return_value=_client(True)):
            assert backend.is_available() is True

    def test_concurrent_callers_make_a_single_attempt(self, vault_env):
        """The auth probe is a network call under a lock, so a thundering herd of
        lookups must collapse into one attempt rather than one per thread."""
        backend = VaultSecretsBackend()
        backend.RETRY_INTERVAL_SECONDS = 300
        results = []

        def slow_client(*a, **kw):
            time.sleep(0.02)
            return _client(False)

        with patch("hvac.Client", side_effect=slow_client) as ctor:
            threads = [
                threading.Thread(target=lambda: results.append(backend.is_available()))
                for _ in range(8)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert results == [False] * 8
        assert ctor.call_count == 1, (
            f"expected the herd to collapse to 1 attempt, got {ctor.call_count}"
        )


class TestSuccessLatches:
    def test_successful_init_is_not_repeated(self, vault_env):
        """Once connected, the client is reused — no re-auth per lookup."""
        backend = VaultSecretsBackend()

        with patch("hvac.Client", return_value=_client(True)) as ctor:
            for _ in range(5):
                assert backend.is_available() is True
            assert ctor.call_count == 1

    def test_get_secret_raises_while_unavailable(self, vault_env):
        """The guard still fails closed rather than returning an empty secret."""
        backend = VaultSecretsBackend()
        backend.RETRY_INTERVAL_SECONDS = 0

        with patch("hvac.Client", return_value=_client(False)):
            with pytest.raises(RuntimeError, match="not available"):
                backend.get_secret("vault:kv/data/aurora/users/x")


class TestErrorAttribution:
    """A down secrets backend must not be reported as a cloud auth failure.

    This is what made the original incident hard to diagnose: users saw "Failed to
    authenticate with Azure" while the real cause was an unreachable Vault, so the
    investigation went looking at Azure service-principal credentials. Every
    provider reads credentials through the same backend, so the attribution has to
    hold for all of them, not just the one that happened to be reported.
    """

    def _backend(self, available: bool):
        backend = MagicMock()
        backend.is_available.return_value = available
        return patch("utils.secrets.get_secrets_backend", return_value=backend)

    def test_names_vault_when_backend_is_down(self):
        from utils.secrets import credential_error_message

        with self._backend(available=False):
            msg = credential_error_message("Failed to authenticate with Azure")

        assert "Vault" in msg
        assert "not a problem with your cloud credentials" in msg

    def test_keeps_provider_message_when_backend_is_healthy(self):
        """A real credential problem must still read as one."""
        from utils.secrets import credential_error_message

        with self._backend(available=True):
            assert credential_error_message("boom") == "boom"

    def test_diagnostic_failure_does_not_mask_the_original_error(self):
        """The availability probe must never replace the error it annotates."""
        from utils.secrets import credential_error_message

        with patch("utils.secrets.get_secrets_backend", side_effect=RuntimeError("nope")):
            assert credential_error_message("boom") == "boom"

    @pytest.mark.parametrize(
        "fallback",
        [
            "Failed to authenticate with Azure",
            "Failed to setup AWS environment with workspace authentication",
            "Failed to setup GCP environment with service_account authentication",
            "Failed to setup OVH environment. Please connect your OVH account first.",
            "Failed to setup Scaleway environment. Please connect your Scaleway account first.",
            "Failed to setup Tailscale environment. Please connect your Tailscale account first.",
            "Failed to setup Fly.io environment. Please connect your Fly.io account first.",
            "Failed to setup Terraform environment",
            "Failed to assume role",
        ],
    )
    def test_every_provider_message_is_replaced_when_backend_is_down(self, fallback):
        """The original fix only covered Azure; the outage hits every provider."""
        from utils.secrets import (
            SECRETS_BACKEND_UNAVAILABLE_MESSAGE,
            credential_error_message,
        )

        with self._backend(available=False):
            assert credential_error_message(fallback) == SECRETS_BACKEND_UNAVAILABLE_MESSAGE


class TestReconnectPromptSuppression:
    """During a backend outage the connector is fine, so "reconnect your account"
    is wrong advice — it sends users to re-add credentials that never broke."""

    def _payload(self, available: bool, requires_connection: bool):
        from chat.backend.agent.tools.cloud_exec_tool import _credential_setup_failure

        backend = MagicMock()
        backend.is_available.return_value = available
        with patch("utils.secrets.get_secrets_backend", return_value=backend):
            return json.loads(
                _credential_setup_failure(
                    "Failed to setup OVH environment. Please connect your OVH account first.",
                    "server list",
                    requires_connection=requires_connection,
                )
            )

    def test_reconnect_prompt_dropped_when_backend_is_down(self):
        payload = self._payload(available=False, requires_connection=True)
        assert "requires_connection" not in payload
        assert "Vault" in payload["error"]

    def test_reconnect_prompt_kept_when_backend_is_healthy(self):
        payload = self._payload(available=True, requires_connection=True)
        assert payload["requires_connection"] is True

    def test_final_command_is_always_reported(self):
        for available in (True, False):
            payload = self._payload(available=available, requires_connection=True)
            assert payload["final_command"] == "server list"
