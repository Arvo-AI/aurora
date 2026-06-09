"""
CloudBees Feature Management client.

Queries the Feature Management (formerly Rollout) public API for
feature flag states and recent changes.
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

FM_BASE_URL = "https://x-api.rollout.io/public-api"
FM_TIMEOUT = 10.0
FM_RATE_LIMIT_DELAY = 1.0  # 1 req/sec rate limit


class CloudBeesFMClient:
    """Client for CloudBees Feature Management (Rollout) API."""

    def __init__(self, api_token: str, base_url: str = FM_BASE_URL):
        self.api_token = api_token
        self.base_url = base_url.rstrip("/")
        self.timeout = FM_TIMEOUT
        self._last_request_time: float = 0.0
        self._http_client: Optional[httpx.Client] = None

    def _get_http_client(self) -> httpx.Client:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.Client(
                headers={
                    "Authorization": f"Bearer {self.api_token}",
                    "Accept": "application/json",
                },
                timeout=httpx.Timeout(self.timeout),
            )
        return self._http_client

    def close(self):
        """Close the underlying HTTP client."""
        if self._http_client and not self._http_client.is_closed:
            self._http_client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def _rate_limit_wait(self):
        """Enforce rate limit of 1 request per second."""
        now = time.monotonic()
        elapsed = now - self._last_request_time
        if elapsed < FM_RATE_LIMIT_DELAY:
            time.sleep(FM_RATE_LIMIT_DELAY - elapsed)
        self._last_request_time = time.monotonic()

    def _request(
        self, method: str, path: str, params: Optional[Dict] = None
    ) -> Tuple[bool, Optional[Any], Optional[str]]:
        """Make an API request with rate limiting. Returns (success, data, error)."""
        self._rate_limit_wait()
        url = f"{self.base_url}{path}"
        client = self._get_http_client()

        try:
            response = client.request(method=method, url=url, params=params)

            if response.status_code == 401:
                return False, None, "Invalid API token. Check your Feature Management credentials."
            if response.status_code == 403:
                return False, None, "Forbidden. Insufficient permissions for Feature Management API."
            if response.status_code == 404:
                return False, None, "Resource not found."
            if response.status_code == 429:
                # Retry once after a short delay
                time.sleep(2.0)
                try:
                    response = client.request(method=method, url=url, params=params)
                except httpx.HTTPError:
                    return False, None, "Rate limit exceeded. Please try again later."
                self._last_request_time = time.monotonic()
                if response.status_code == 429:
                    return False, None, "Rate limit exceeded. Please try again later."
            if response.status_code >= 400:
                return False, None, f"Feature Management API error ({response.status_code})"

            if not response.text:
                return True, None, None
            try:
                return True, response.json(), None
            except ValueError:
                logger.warning("FM API returned non-JSON for %s %s", method, path)
                return False, None, "Unexpected response format from Feature Management API."

        except httpx.TimeoutException:
            return False, None, "Connection timeout reaching Feature Management API."
        except httpx.ConnectError:
            return False, None, "Cannot connect to Feature Management API."
        except httpx.HTTPError:
            logger.exception("FM API request failed")
            return False, None, "Feature Management API request failed."

    def validate_token(self) -> bool:
        """Validate the API token by listing applications. Returns True if valid."""
        success, _, _ = self._request("GET", "/applications")
        return success

    def list_applications(self) -> Tuple[bool, List[Dict], Optional[str]]:
        """List all applications."""
        success, data, error = self._request("GET", "/applications")
        if not success:
            return False, [], error

        apps = []
        if isinstance(data, list):
            apps = data
        elif isinstance(data, dict):
            apps = data.get("items") or data.get("applications") or []

        return True, apps, None

    def get_recent_flag_changes(
        self, app_id: str, since_hours: int = 24
    ) -> Tuple[bool, List[Dict], Optional[str]]:
        """Get flags that were modified within the given time window."""
        success, data, error = self._request(
            "GET", f"/applications/{quote(app_id, safe='')}/flags"
        )
        if not success:
            return False, [], error

        flags = []
        if isinstance(data, list):
            flags = data
        elif isinstance(data, dict):
            flags = data.get("items") or data.get("flags") or []

        cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
        recent_changes = [
            flag for flag in flags
            if _is_recently_modified(flag, cutoff)
        ]
        return True, recent_changes, None


def _is_recently_modified(flag: Dict, cutoff: datetime) -> bool:
    """Check if a flag was modified after the cutoff time."""
    updated_at = flag.get("updatedAt") or flag.get("updated_at") or flag.get("modifiedAt")
    if not updated_at:
        return False
    try:
        flag_time = _parse_timestamp(updated_at)
        return flag_time is not None and flag_time >= cutoff
    except (ValueError, TypeError, OSError):
        return False


def _parse_timestamp(value) -> Optional[datetime]:
    """Parse a timestamp value (ISO string or epoch number) into an aware datetime."""
    if isinstance(value, str):
        value = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    if isinstance(value, (int, float)):
        if value > 1e12:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        return datetime.fromtimestamp(value, tz=timezone.utc)
    return None
