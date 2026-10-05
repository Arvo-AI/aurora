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
    """Minimum env for the backend to attempt a connection at all."""
    monkeypatch.setenv("VAULT_TOKEN", "test-token")
    monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")


def _client(authenticated: bool) -> MagicMock:
    """A stand-in hvac client whose auth probe returns ``authenticated``."""
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

    def test_missing_token_is_config_blocked_without_retry_storm(self, vault_env, monkeypatch):
        """Unset VAULT_TOKEN is a deploy-time config gap, not a transient outage."""
        monkeypatch.delenv("VAULT_TOKEN", raising=False)
        backend = VaultSecretsBackend()
        backend.RETRY_INTERVAL_SECONDS = 0

        with patch("hvac.Client", return_value=_client(True)) as ctor:
            assert backend.is_available() is False
            assert backend._config_blocked is True
            for _ in range(5):
                assert backend.is_available() is False
            assert ctor.call_count == 0, "config gap must not reconnect on every lookup"

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
        """Backoff suppresses retries, it does not end them."""
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
            """Slow enough that the other threads pile up on the init lock."""
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
            # Probe client plus the stored operations client.
            assert ctor.call_count == 2

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
        """Patch in a backend reporting the given availability."""
        backend = MagicMock()
        backend.is_available.return_value = available
        return patch("utils.secrets.get_secrets_backend", return_value=backend)

    def test_names_vault_when_backend_is_down(self):
        """The whole point: the message must point at Vault, not the provider."""
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


class TestAvailabilityIsRevalidatedAfterOperations:
    """Init-time success must not be mistaken for *current* health.

    Vault can seal, or VAULT_TOKEN can expire, after a successful init. The
    availability latch is the input to the outage-vs-credentials attribution, so a
    stale ``True`` puts us straight back to blaming the provider.
    """

    _REF = "vault:kv/data/aurora/users/x"

    def _connected(self, backend, client):
        """Drive the backend to a latched-available state on ``client``."""
        with patch("hvac.Client", return_value=client):
            assert backend.is_available() is True

    def test_read_failure_re_probes_and_reports_the_outage(self, vault_env):
        """A read that fails on a sealed Vault must re-open the availability check."""
        backend = VaultSecretsBackend()
        client = _client(True)
        self._connected(backend, client)

        # Vault seals after init: the read fails while the latch still says available.
        client.secrets.kv.v2.read_secret_version.side_effect = RuntimeError("Vault is sealed")
        with pytest.raises(RuntimeError, match="sealed"):
            backend.get_secret(self._REF)

        with patch("hvac.Client", return_value=_client(False)) as ctor:
            assert backend.is_available() is False
            assert ctor.call_count == 1, "availability was never re-probed"

    def test_expired_token_after_init_is_attributed_to_the_backend(self, vault_env):
        """End-to-end: the cached latch used to report an outage as a provider
        credential error, which is the whole misdiagnosis this PR exists to fix."""
        from utils.secrets import (
            SECRETS_BACKEND_UNAVAILABLE_MESSAGE,
            credential_error_message,
        )

        backend = VaultSecretsBackend()
        client = _client(True)
        self._connected(backend, client)

        Unauthorized = type("Unauthorized", (Exception,), {})
        client.secrets.kv.v2.read_secret_version.side_effect = Unauthorized("token expired")
        with pytest.raises(Unauthorized):
            backend.get_secret(self._REF)

        with patch("hvac.Client", return_value=_client(False)), patch(
            "utils.secrets.get_secrets_backend", return_value=backend
        ):
            assert (
                credential_error_message("Failed to authenticate with Azure")
                == SECRETS_BACKEND_UNAVAILABLE_MESSAGE
            )

    @pytest.mark.parametrize(
        "exc",
        [
            type("InvalidPath", (Exception,), {})("nope"),
            Exception("secret not found at that path"),
            Exception("no versions of this secret exist"),
        ],
        ids=["InvalidPath", "not-found", "no-versions"],
    )
    def test_missing_secret_leaves_the_latch_intact(self, vault_env, exc):
        """A 404 is about one secret, so re-probing on it would be pure overhead."""
        backend = VaultSecretsBackend()
        client = _client(True)

        with patch("hvac.Client", return_value=client) as ctor:
            assert backend.is_available() is True
            client.secrets.kv.v2.read_secret_version.side_effect = exc

            with pytest.raises(type(exc)) as raised:
                backend.get_secret(self._REF)
            assert raised.value is exc

            assert backend.is_available() is True
            assert ctor.call_count == 2, "a missing secret must not trigger a re-probe"

    def test_malformed_reference_leaves_the_latch_intact(self, vault_env):
        """A bad ref is a caller bug, so it must not cost a re-probe either."""
        backend = VaultSecretsBackend()

        with patch("hvac.Client", return_value=_client(True)) as ctor:
            assert backend.is_available() is True
            with pytest.raises(ValueError, match="Invalid Vault secret reference"):
                backend.get_secret("not-a-vault-ref")

            assert backend.is_available() is True
            assert ctor.call_count == 2

    def test_forbidden_on_one_path_does_not_declare_an_outage(self, vault_env):
        """A per-path ACL denial must not force a re-probe or flip the outage latch."""
        from utils.secrets import credential_error_message

        backend = VaultSecretsBackend()
        client = _client(True)
        Forbidden = type("Forbidden", (Exception,), {})

        with patch("hvac.Client", return_value=client) as ctor:
            assert backend.is_available() is True
            client.secrets.kv.v2.read_secret_version.side_effect = Forbidden("permission denied")
            with pytest.raises(Forbidden, match="permission denied"):
                backend.get_secret(self._REF)

            with patch("utils.secrets.get_secrets_backend", return_value=backend):
                assert credential_error_message("boom") == "boom"
            assert backend.is_available() is True
            assert ctor.call_count == 2

    @pytest.mark.parametrize("operation", ["store", "delete"])
    def test_write_failures_also_re_open_the_availability_check(self, vault_env, operation):
        """Reads are not the only signal — a failed write says as much about health."""
        backend = VaultSecretsBackend()
        client = _client(True)
        self._connected(backend, client)

        if operation == "store":
            client.secrets.kv.v2.create_or_update_secret.side_effect = RuntimeError("sealed")
            with pytest.raises(RuntimeError):
                backend.store_secret("x", "y")
        else:
            client.secrets.kv.v2.delete_metadata_and_all_versions.side_effect = RuntimeError("sealed")
            with pytest.raises(RuntimeError):
                backend.delete_secret(self._REF)

        assert backend._initialized is False, (
            f"{operation} failure did not re-open the availability check"
        )

    def test_a_single_failure_does_not_reconnect_on_every_later_read(self, vault_env):
        """Re-validating must not undo the thundering-herd guard: the failed re-probe
        starts a backoff window and later reads fail fast on the guard."""
        backend = VaultSecretsBackend()
        backend.RETRY_INTERVAL_SECONDS = 300
        client = _client(True)
        self._connected(backend, client)

        client.secrets.kv.v2.read_secret_version.side_effect = RuntimeError("sealed")
        with pytest.raises(RuntimeError):
            backend.get_secret(self._REF)

        with patch("hvac.Client", return_value=_client(False)) as ctor:
            for _ in range(10):
                with pytest.raises(RuntimeError, match="not available"):
                    backend.get_secret(self._REF)
            assert ctor.call_count == 1, (
                f"expected 1 re-probe inside the backoff window, got {ctor.call_count}"
            )

    def test_stale_failure_cannot_unlatch_a_reconnected_client(self, vault_env):
        """A read that started before the outage can land after recovery. Clearing
        the latch then would cost the fresh client a pointless re-probe, so the
        invalidation only applies to the client that actually failed."""
        backend = VaultSecretsBackend()
        old_client = _client(True)
        self._connected(backend, old_client)

        # Vault recovers and another caller publishes a replacement client.
        new_client = _client(True)
        backend._client = new_client

        backend._invalidate_after_operation_failure(RuntimeError("sealed"), old_client)

        assert backend._initialized is True, "the replacement client was unlatched"
        assert backend._available is True

    def test_current_client_failure_still_unlatches(self, vault_env):
        """The guard must not swallow the case it exists to allow."""
        backend = VaultSecretsBackend()
        client = _client(True)
        self._connected(backend, client)

        backend._invalidate_after_operation_failure(RuntimeError("sealed"), client)

        assert backend._initialized is False
        assert backend._available is False

    def test_in_flight_read_on_a_replaced_client_leaves_the_latch_alone(self, vault_env):
        """End-to-end form of the race: the failing read is issued against the
        client it captured, and its failure doesn't disturb the new one."""
        backend = VaultSecretsBackend()
        old_client = _client(True)
        self._connected(backend, old_client)

        new_client = _client(True)
        released = threading.Event()

        def slow_failing_read(*a, **kw):
            """Blocks until the test has swapped the client, then fails."""
            released.wait(1)
            raise RuntimeError("Vault is sealed")

        old_client.secrets.kv.v2.read_secret_version.side_effect = slow_failing_read

        errors = []

        def read():
            """Runs the doomed read off-thread so the swap can race it."""
            try:
                backend.get_secret(self._REF)
            except Exception as exc:  # noqa: BLE001 - asserted on below
                errors.append(exc)

        reader = threading.Thread(target=read)
        reader.start()
        # Swap in the recovered client while the read is still in flight.
        backend._client = new_client
        released.set()
        reader.join(2)

        assert errors, "the in-flight read did not fail"
        assert "sealed" in str(errors[0])
        assert backend._initialized is True, "stale failure unlatched the new client"
        assert backend._available is True
        new_client.secrets.kv.v2.read_secret_version.assert_not_called()


class TestBackendIsNamedInTheDiagnostic:
    """Vault is the default, not the only backend. Telling an AWS Secrets Manager
    operator to check VAULT_ADDR sends them to a service they aren't running."""

    def _message(self, monkeypatch, **env) -> str:
        """Build a fresh backend from ``env`` and return the message it produces."""
        from utils.secrets import credential_error_message, reset_backend

        for key, value in env.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)

        reset_backend()
        try:
            return credential_error_message("Failed to authenticate with Azure")
        finally:
            reset_backend()

    def test_aws_secrets_manager_gets_aws_guidance(self, monkeypatch):
        """An operator not running Vault must not be sent to check VAULT_ADDR."""
        from utils.secrets import AWS_SM_UNAVAILABLE_MESSAGE

        msg = self._message(
            monkeypatch, SECRETS_BACKEND="aws_secrets_manager", AWS_SM_REGION=None
        )

        assert msg == AWS_SM_UNAVAILABLE_MESSAGE
        assert "AWS Secrets Manager" in msg
        assert "VAULT_ADDR" not in msg

    def test_vault_still_gets_vault_guidance(self, monkeypatch):
        """Selecting on backend type must not regress the default case."""
        from utils.secrets import SECRETS_BACKEND_UNAVAILABLE_MESSAGE

        msg = self._message(monkeypatch, SECRETS_BACKEND="vault", VAULT_TOKEN=None)

        assert msg == SECRETS_BACKEND_UNAVAILABLE_MESSAGE
        assert "Vault" in msg
        assert "AWS_SM_REGION" not in msg

    def test_both_messages_disclaim_the_provider_credentials(self):
        """Whichever backend is down, the point is that the connector is not at fault."""
        from utils.secrets import (
            AWS_SM_UNAVAILABLE_MESSAGE,
            SECRETS_BACKEND_UNAVAILABLE_MESSAGE,
        )

        for msg in (SECRETS_BACKEND_UNAVAILABLE_MESSAGE, AWS_SM_UNAVAILABLE_MESSAGE):
            assert "not a problem with your cloud credentials" in msg


class TestReconnectPromptSuppression:
    """During a backend outage the connector is fine, so "reconnect your account"
    is wrong advice — it sends users to re-add credentials that never broke."""

    def _payload(self, available: bool, requires_connection: bool):
        """The failure payload cloud_exec would return under the given conditions."""
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
        """No reconnect flow during an outage — the connector never broke."""
        payload = self._payload(available=False, requires_connection=True)
        assert "requires_connection" not in payload
        assert "Vault" in payload["error"]

    def test_reconnect_prompt_kept_when_backend_is_healthy(self):
        """A genuinely disconnected account must still be prompted to reconnect."""
        payload = self._payload(available=True, requires_connection=True)
        assert payload["requires_connection"] is True

    def test_final_command_is_always_reported(self):
        """Suppressing the prompt must not drop the rest of the payload."""
        for available in (True, False):
            payload = self._payload(available=available, requires_connection=True)
            assert payload["final_command"] == "server list"
