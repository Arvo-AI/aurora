"""
Shared HTTP client base for Prometheus-compatible APIs.

Handles authentication, retries, timeouts, and error mapping
for both Prometheus and Alertmanager clients.
"""

import base64
import logging
import time
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 5
DEFAULT_READ_TIMEOUT = 30
MAX_RETRIES = 2
RETRY_BACKOFF = 1.0


class APIError(Exception):
    """Base error for Prometheus/Alertmanager API failures."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


def build_auth_headers(
    auth_type: str,
    username: Optional[str] = None,
    password: Optional[str] = None,
    bearer_token: Optional[str] = None,
    custom_headers: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Build authentication headers from credential parameters."""
    headers: Dict[str, str] = {}

    auth_type = (auth_type or "none").lower().strip()

    if auth_type == "none":
        pass
    elif auth_type == "basic":
        if not username or not password:
            raise ValueError("Username and password are required for basic auth")
        encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {encoded}"
    elif auth_type == "bearer":
        if not bearer_token:
            raise ValueError("Bearer token is required for bearer auth")
        headers["Authorization"] = f"Bearer {bearer_token}"
    elif auth_type == "custom":
        if custom_headers:
            headers.update(custom_headers)
    else:
        raise ValueError(f"Unsupported auth_type: {auth_type}")

    return headers


def build_auth_headers_from_creds(creds: Dict[str, Any]) -> Dict[str, str]:
    """Build auth headers from a stored credentials dict."""
    return build_auth_headers(
        auth_type=creds.get("auth_type", "none"),
        username=creds.get("username"),
        password=creds.get("password"),
        bearer_token=creds.get("bearer_token"),
        custom_headers=creds.get("custom_headers"),
    )


class BaseHTTPClient:
    """Shared HTTP client with retry logic and error handling."""

    def __init__(
        self,
        base_url: str,
        auth_headers: Optional[Dict[str, str]] = None,
        verify_ssl: bool = True,
        read_timeout: int = DEFAULT_READ_TIMEOUT,
    ):
        if not base_url:
            raise ValueError("URL is required")

        self.base_url = base_url.rstrip("/")
        self._auth_headers = auth_headers or {}
        self.verify_ssl = verify_ssl
        self.read_timeout = read_timeout

    @property
    def headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/json",
            **self._auth_headers,
        }

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        json_body: Optional[Any] = None,
        retries: Optional[int] = None,
    ) -> Any:
        """Execute an HTTP request with retry logic and unified error handling."""
        url = f"{self.base_url}{path}"
        last_error: Optional[Exception] = None
        max_retries = retries if retries is not None else MAX_RETRIES

        for attempt in range(max_retries + 1):
            try:
                response = requests.request(
                    method=method,
                    url=url,
                    headers=self.headers,
                    params=params,
                    data=data,
                    json=json_body,
                    timeout=(CONNECT_TIMEOUT, self.read_timeout),
                    verify=self.verify_ssl,
                )

                # Auth failures — no retry
                if response.status_code == 401:
                    raise APIError("Authentication failed — check credentials", status_code=401)
                if response.status_code == 403:
                    raise APIError("Access forbidden — insufficient permissions", status_code=403)
                if response.status_code == 404:
                    raise APIError(f"Endpoint not found: {path}", status_code=404)

                # Retryable errors
                if response.status_code == 429:
                    if attempt < max_retries:
                        retry_after = int(response.headers.get("Retry-After", 5))
                        time.sleep(min(retry_after, 30))
                        continue
                    raise APIError("Rate limit exceeded", status_code=429)

                if response.status_code == 503:
                    if attempt < max_retries:
                        time.sleep(RETRY_BACKOFF * (2 ** attempt))
                        continue
                    raise APIError("Server unavailable", status_code=503)

                response.raise_for_status()

                if not response.content:
                    return None
                return response.json()

            except requests.ConnectionError as exc:
                last_error = exc
                if attempt < max_retries:
                    time.sleep(RETRY_BACKOFF * (2 ** attempt))
                    continue

            except requests.Timeout as exc:
                last_error = exc
                if attempt < max_retries:
                    time.sleep(RETRY_BACKOFF * (2 ** attempt))
                    continue

            except APIError:
                raise

            except requests.HTTPError as exc:
                raise APIError(
                    f"HTTP error: {exc}",
                    status_code=getattr(exc.response, "status_code", None),
                ) from exc

        raise APIError(f"Request failed after {max_retries + 1} attempts: {last_error}")
