"""
Spinnaker Gate API client with dual auth support (token/basic + X.509).

Use :func:`get_spinnaker_client` to obtain a cached client instance.
Cache keeps a single ``SpinnakerClient`` per user_id alive for
``CLIENT_CACHE_TTL`` seconds so that the underlying ``requests.Session``
is reused across Flask requests and agent tool calls.
"""

import hashlib
import logging
import os
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)

SPINNAKER_TIMEOUT = 30
CLIENT_CACHE_TTL = 3600  # 1 hour

_client_cache: Dict[str, Tuple["SpinnakerClient", float, str]] = {}
_cache_lock = threading.Lock()
_user_locks: Dict[str, threading.Lock] = {}
_SWEEP_INTERVAL: float = CLIENT_CACHE_TTL / 2


class SpinnakerAPIError(Exception):
    """Custom error for Spinnaker API interactions."""


def _get_user_lock(user_id: str) -> threading.Lock:
    with _cache_lock:
        lock = _user_locks.get(user_id)
        if lock is None:
            lock = threading.Lock()
            _user_locks[user_id] = lock
        return lock


def _sweep_stale_locks() -> None:
    """Remove ``_user_locks`` entries whose user_id has no live cache entry.

    Must be called while holding ``_cache_lock``.
    """
    now = time.monotonic()
    last_sweep = getattr(_sweep_stale_locks, "_last_sweep", 0.0)
    if now - last_sweep < _SWEEP_INTERVAL:
        return
    _sweep_stale_locks._last_sweep = now
    stale = [uid for uid in _user_locks if uid not in _client_cache and not _user_locks[uid].locked()]
    for uid in stale:
        del _user_locks[uid]
    if stale:
        logger.debug("[SPINNAKER] Swept %d stale user locks", len(stale))


def get_spinnaker_client(
    user_id: str,
    base_url: str,
    auth_type: str = "token",
    username: Optional[str] = None,
    password: Optional[str] = None,
    cert_pem: Optional[str] = None,
    key_pem: Optional[str] = None,
    ca_bundle_pem: Optional[str] = None,
) -> "SpinnakerClient":
    """Return a cached :class:`SpinnakerClient` for *user_id*.

    A new client is created when no cached entry exists, the TTL has
    expired, or the credentials have changed.
    """
    user_lock = _get_user_lock(user_id)
    with user_lock:
        now = time.monotonic()
        with _cache_lock:
            entry = _client_cache.get(user_id)
        # Cache key from non-sensitive fields only — avoids hashing raw secrets
        # while still detecting config changes that should invalidate the client.
        cache_key_material = f"{base_url}:{auth_type}:{username}:{bool(cert_pem)}:{bool(key_pem)}:{bool(ca_bundle_pem)}"
        creds_hash = hashlib.sha256(cache_key_material.encode("utf-8")).hexdigest()
        if entry is not None:
            client, created_at, prev_hash = entry
            if prev_hash == creds_hash and (now - created_at) < CLIENT_CACHE_TTL:
                return client

        client = SpinnakerClient(
            base_url=base_url,
            auth_type=auth_type,
            username=username,
            password=password,
            cert_pem=cert_pem,
            key_pem=key_pem,
            ca_bundle_pem=ca_bundle_pem,
            user_id=user_id,
        )
        # Validate by fetching credentials
        try:
            client.get_credentials()
        except Exception:
            client.close()
            raise

        with _cache_lock:
            if entry is not None:
                try:
                    entry[0].close()
                except Exception:
                    logger.debug(
                        "[SPINNAKER] Failed to close session for user_id=%s (replacement)",
                        user_id,
                        exc_info=True,
                    )
            _client_cache[user_id] = (client, now, creds_hash)
            _sweep_stale_locks()
        return client


def get_spinnaker_client_for_user(user_id: str) -> Optional["SpinnakerClient"]:
    """Return a cached client for *user_id* using stored credentials.

    Convenience wrapper used by both routes and RCA tool to avoid
    duplicating the credential-unpacking logic.
    """
    from utils.auth.token_management import get_token_data

    creds = get_token_data(user_id, "spinnaker")
    if not creds:
        return None
    base_url = creds.get("base_url")
    if not base_url:
        return None
    try:
        return get_spinnaker_client(
            user_id=user_id,
            base_url=base_url,
            auth_type=creds.get("auth_type", "token"),
            username=creds.get("username"),
            password=creds.get("password"),
            cert_pem=creds.get("cert_pem"),
            key_pem=creds.get("key_pem"),
            ca_bundle_pem=creds.get("ca_bundle_pem"),
        )
    except Exception:
        logger.warning("[SPINNAKER] Failed to get client for user %s", user_id, exc_info=True)
        return None


def invalidate_spinnaker_client(user_id: str) -> None:
    """Remove *user_id*'s client from the cache (e.g. on disconnect)."""
    with _cache_lock:
        entry = _client_cache.pop(user_id, None)
        lock = _user_locks.get(user_id)
        if lock is not None and not lock.locked():
            _user_locks.pop(user_id, None)
    if entry is not None:
        try:
            entry[0].close()
        except Exception:
            logger.debug(
                "[SPINNAKER] Failed to close session for user_id=%s",
                user_id,
                exc_info=True,
            )


class SpinnakerClient:
    """HTTP client for Spinnaker Gate API.

    Supports dual auth:
    - Token/Basic: ``session.auth = HTTPBasicAuth(username, password)``
    - X.509: ``session.cert = (cert_path, key_path)``

    Prefer :func:`get_spinnaker_client` over constructing this directly.
    """

    def __init__(
        self,
        base_url: str,
        auth_type: str = "token",
        username: Optional[str] = None,
        password: Optional[str] = None,
        cert_pem: Optional[str] = None,
        key_pem: Optional[str] = None,
        ca_bundle_pem: Optional[str] = None,
        user_id: Optional[str] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.auth_type = auth_type
        self._user_id = user_id
        self._temp_files: List[str] = []
        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

        if auth_type == "x509":
            cert_path = self._write_temp(cert_pem, "cert.pem")
            key_path = self._write_temp(key_pem, "key.pem")
            self._session.cert = (cert_path, key_path)
            if ca_bundle_pem:
                ca_path = self._write_temp(ca_bundle_pem, "ca.pem")
                self._session.verify = ca_path
        else:
            if username and password:
                self._session.auth = HTTPBasicAuth(username, password)

    def _write_temp(self, content: Optional[str], suffix: str) -> str:
        """Write PEM content to a temp file and return the path."""
        if not content:
            raise SpinnakerAPIError(f"Missing PEM content for {suffix}")
        fd, path = tempfile.mkstemp(suffix=f"-spinnaker-{suffix}")
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, content.encode("utf-8"))
        finally:
            os.close(fd)
        self._temp_files.append(path)
        return path

    def close(self) -> None:
        """Close session and clean up temp files."""
        try:
            self._session.close()
        except Exception:
            logger.debug("[SPINNAKER] Failed to close session", exc_info=True)
        for path in self._temp_files:
            try:
                os.unlink(path)
            except OSError:
                logger.debug("[SPINNAKER] Failed to remove temp file %s", path)
        self._temp_files.clear()

    # ------------------------------------------------------------------
    # Internal request helper
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Send an authenticated request to Spinnaker Gate API."""
        kwargs.setdefault("timeout", SPINNAKER_TIMEOUT)
        url = f"{self.base_url}{path}"

        try:
            resp = self._session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            logger.error("[SPINNAKER] %s %s network error: %s", method, path, exc)
            raise SpinnakerAPIError("Unable to reach Spinnaker Gate API") from exc

        if resp.status_code == 401:
            if self._user_id:
                invalidate_spinnaker_client(self._user_id)
            raise SpinnakerAPIError("Invalid or expired credentials")

        if resp.status_code == 404:
            raise SpinnakerAPIError(f"Resource not found: {path}")

        if not resp.ok:
            logger.error(
                "[SPINNAKER] %s %s failed (HTTP %s)",
                method, path, resp.status_code,
            )
            raise SpinnakerAPIError(
                f"Spinnaker API request failed (HTTP {resp.status_code})"
            )

        if resp.status_code == 204 or not resp.text:
            return {}

        try:
            return resp.json()
        except (ValueError, requests.exceptions.JSONDecodeError):
            raise SpinnakerAPIError(f"Non-JSON response from {path}")

    def _get(self, path: str, **kwargs: Any) -> Any:
        return self._request("GET", path, **kwargs)

    def _post(self, path: str, **kwargs: Any) -> Any:
        return self._request("POST", path, **kwargs)

    # ------------------------------------------------------------------
    # Credentials / Status
    # ------------------------------------------------------------------

    def get_credentials(self) -> List[Dict[str, Any]]:
        """Validate connection by fetching cloud accounts."""
        return self._get("/credentials")

    # ------------------------------------------------------------------
    # Applications
    # ------------------------------------------------------------------

    def list_applications(self) -> List[Dict[str, Any]]:
        """List all Spinnaker applications."""
        return self._get("/applications")

    def get_application(self, app: str) -> Dict[str, Any]:
        """Get details for a specific application."""
        return self._get(f"/applications/{app}")

    # ------------------------------------------------------------------
    # Pipeline Executions
    # ------------------------------------------------------------------

    def list_pipeline_executions(
        self, app: str, limit: int = 25, statuses: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """List recent pipeline executions for an application."""
        params: Dict[str, Any] = {"limit": limit}
        if statuses:
            params["statuses"] = statuses
        return self._get(f"/applications/{app}/pipelines", params=params)

    def get_pipeline_execution(self, execution_id: str) -> Dict[str, Any]:
        """Get full details for a specific pipeline execution."""
        return self._get(f"/pipelines/{execution_id}")

    # ------------------------------------------------------------------
    # Pipeline Configs
    # ------------------------------------------------------------------

    def list_pipeline_configs(self, app: str) -> List[Dict[str, Any]]:
        """List pipeline definitions for an application."""
        return self._get(f"/applications/{app}/pipelineConfigs")

    # ------------------------------------------------------------------
    # Trigger Pipeline
    # ------------------------------------------------------------------

    def trigger_pipeline(
        self, app: str, pipeline_name: str, parameters: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """Trigger a named pipeline for an application."""
        body: Dict[str, Any] = {"type": "manual"}
        if parameters:
            body["parameters"] = parameters
        return self._post(f"/pipelines/{app}/{pipeline_name}", json=body)

    # ------------------------------------------------------------------
    # Clusters / Server Groups
    # ------------------------------------------------------------------

    def list_clusters(self, app: str) -> Dict[str, Any]:
        """List clusters for an application."""
        return self._get(f"/applications/{app}/clusters")

    def list_server_groups(self, app: str, account: str, cluster: str) -> List[Dict[str, Any]]:
        """List server groups for a cluster."""
        return self._get(f"/applications/{app}/clusters/{account}/{cluster}/serverGroups")

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    def list_tasks(self, app: str) -> List[Dict[str, Any]]:
        """List tasks for an application."""
        return self._get(f"/applications/{app}/tasks")

    def get_task(self, task_id: str) -> Dict[str, Any]:
        """Get details for a specific task."""
        return self._get(f"/tasks/{task_id}")
