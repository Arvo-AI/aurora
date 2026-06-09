"""
CloudBees Operations Center (CJOC) client.

Discovers managed controllers and queries builds across them.
OC typically lives at `{base_url}/cjoc` or IS the base URL directly.
Uses Jenkins API format (HTTP Basic Auth with username + api_token).
"""

import logging
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from connectors.jenkins_connector.api_client import JenkinsClient

logger = logging.getLogger(__name__)

MAX_CONTROLLERS = 20
MAX_JOBS_PER_CONTROLLER = 50
MAX_BUILDS_PER_CONTROLLER = 5
DEFAULT_TIMEOUT = 15.0


class CloudBeesOCClient:
    """Client for CloudBees Operations Center (CJOC)."""

    def __init__(self, base_url: str, username: str, api_token: str, auth_mode: str = "basic"):
        self.base_url = base_url.rstrip("/")
        parsed = urlparse(self.base_url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"Invalid URL scheme: {parsed.scheme!r}. Only http and https are allowed.")
        self.username = username
        self.api_token = api_token
        self.auth_mode = auth_mode if auth_mode in ("basic", "bearer") else "basic"
        # Auto-detect bearer mode: if username is empty but token is present, use bearer
        if not self.username and self.api_token:
            self.auth_mode = "bearer"
        self.timeout = DEFAULT_TIMEOUT
        self._http_client: Optional[httpx.Client] = None

    def _get_http_client(self) -> httpx.Client:
        if self._http_client is None or self._http_client.is_closed:
            if self.auth_mode == "bearer":
                self._http_client = httpx.Client(
                    timeout=httpx.Timeout(self.timeout),
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Bearer {self.api_token}",
                    },
                )
            else:
                self._http_client = httpx.Client(
                    auth=(self.username, self.api_token),
                    timeout=httpx.Timeout(self.timeout),
                    headers={"Accept": "application/json"},
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

    def _validate_controller_url(self, controller_url: str) -> None:
        """Validate that a controller URL belongs to the same registrable domain as the OC URL."""
        oc_host = urlparse(self.base_url).hostname or ""
        ctrl_host = urlparse(controller_url).hostname or ""

        if not ctrl_host:
            raise ValueError(f"Invalid controller URL: {controller_url}")

        if ctrl_host == oc_host:
            return

        try:
            import tldextract
            oc_extracted = tldextract.extract(oc_host)
            ctrl_extracted = tldextract.extract(ctrl_host)
            oc_registered = oc_extracted.registered_domain
            ctrl_registered = ctrl_extracted.registered_domain
        except ImportError:
            oc_parts = oc_host.split(".")
            ctrl_parts = ctrl_host.split(".")
            oc_registered = ".".join(oc_parts[-2:]) if len(oc_parts) >= 2 else oc_host
            ctrl_registered = ".".join(ctrl_parts[-2:]) if len(ctrl_parts) >= 2 else ctrl_host

        if not oc_registered or not ctrl_registered or ctrl_registered != oc_registered:
            raise ValueError(
                f"Controller URL domain '{ctrl_host}' does not match "
                f"Operations Center domain '{oc_host}'"
            )

    def _request(
        self, method: str, path: str, params: Optional[Dict] = None
    ) -> Tuple[bool, Optional[Any], Optional[str]]:
        """Make an API request. Returns (success, data, error)."""
        url = f"{self.base_url}{path}"
        client = self._get_http_client()
        try:
            response = client.request(method=method, url=url, params=params)

            if response.status_code == 401:
                return False, None, "Invalid credentials. Check your username and API token."
            if response.status_code == 403:
                return False, None, "Forbidden. Insufficient permissions."
            if response.status_code == 404:
                return False, None, "Resource not found."
            if response.status_code >= 400:
                return False, None, f"Operations Center API error ({response.status_code})"

            if not response.text:
                return True, None, None
            try:
                return True, response.json(), None
            except ValueError:
                logger.warning("OC API returned non-JSON for %s %s", method, path)
                return False, None, "Unexpected response format from Operations Center."

        except httpx.TimeoutException:
            return False, None, "Connection timeout. Verify the Operations Center URL is reachable."
        except httpx.ConnectError:
            return False, None, "Cannot connect to Operations Center. Verify the URL and network access."
        except httpx.HTTPError:
            logger.exception("OC API request failed")
            return False, None, "Cannot connect to Operations Center. Verify the URL and network access."

    def get_server_info(self) -> Tuple[bool, Optional[Dict], Optional[str]]:
        """Validate OC connection by fetching server info."""
        # Try /cjoc path first, then root
        success, data, error = self._request(
            "GET", "/cjoc/api/json", params={"tree": "mode,nodeDescription,numExecutors,useSecurity"}
        )
        if success:
            return success, data, error

        # Fallback: OC might be at the root
        return self._request(
            "GET", "/api/json", params={"tree": "mode,nodeDescription,numExecutors,useSecurity"}
        )

    def discover_controllers(self) -> Tuple[bool, List[Dict], Optional[str]]:
        """Discover managed controllers from Operations Center."""
        success, data, error = self._fetch_controller_data()
        if not success:
            return False, [], error

        controllers = _parse_controller_response(data) if data else []
        return True, controllers, None

    def _fetch_controller_data(self) -> Tuple[bool, Optional[Any], Optional[str]]:
        """Try multiple OC endpoints to fetch controller data."""
        endpoints = [
            ("/cjoc/masterProvisioning/api/json", None),
            ("/cjoc/api/json", {"tree": "jobs[name,url,color,description]"}),
            ("/api/json", {"tree": "jobs[name,url,color,description]"}),
        ]
        for path, params in endpoints:
            success, data, error = self._request("GET", path, params=params)
            if success:
                return True, data, None
        return False, None, error

    def get_controller_client(self, controller_url: str) -> JenkinsClient:
        """Return a JenkinsClient configured for a specific managed controller."""
        self._validate_controller_url(controller_url)
        return JenkinsClient(
            base_url=controller_url.rstrip("/"),
            username=self.username,
            api_token=self.api_token,
        )

    def query_recent_builds_across_controllers(
        self, service: Optional[str] = None, time_window_hours: int = 24
    ) -> Tuple[bool, List[Dict], Optional[str]]:
        """Query recent builds across all discovered controllers."""
        import time as _time

        success, controllers, error = self.discover_controllers()
        if not success:
            return False, [], error

        if not controllers:
            return True, [], None

        cutoff_ms = int((_time.time() - time_window_hours * 3600) * 1000)

        all_builds: List[Dict] = []
        errors: List[str] = []

        for controller in controllers[:MAX_CONTROLLERS]:
            ctrl_builds, ctrl_error = self._query_single_controller(
                controller, service, cutoff_ms
            )
            all_builds.extend(ctrl_builds)
            if ctrl_error:
                errors.append(ctrl_error)

        all_builds.sort(key=lambda b: b.get("timestamp", 0), reverse=True)
        return True, all_builds, "; ".join(errors) if errors else None

    def _query_single_controller(
        self, controller: Dict, service: Optional[str], cutoff_ms: int
    ) -> Tuple[List[Dict], Optional[str]]:
        """Query builds from a single controller. Returns (builds, error_msg)."""
        controller_url = controller.get("url")
        if not controller_url:
            return [], None

        try:
            self._validate_controller_url(controller_url)
            client = self.get_controller_client(controller_url)
            ok, jobs, _ = client.list_jobs()
            if not ok:
                return [], f"{controller['name']}: Failed to query controller"

            if service:
                jobs = [j for j in jobs if service.lower() in (j.get("name", "") or "").lower()]

            builds = []
            for job in jobs[:MAX_JOBS_PER_CONTROLLER]:
                job_name = job.get("name") or job.get("fullName")
                if not job_name:
                    continue

                ok, job_builds, _ = client.list_builds(job_name, limit=MAX_BUILDS_PER_CONTROLLER)
                if not ok or not job_builds:
                    continue
                for build in job_builds:
                    if build.get("timestamp", 0) < cutoff_ms:
                        continue
                    build["_controller"] = controller["name"]
                    build["_job"] = job_name
                    builds.append(build)

            return builds, None

        except Exception as e:
            logger.warning("Failed to query controller %s: %s", controller.get("name"), e)
            return [], f"{controller.get('name', 'unknown')}: Failed to query controller"


def _parse_controller_response(data: Dict) -> List[Dict]:
    """Parse controller list from OC API response data."""
    masters = data.get("masters") or data.get("items") or []
    if masters:
        return [
            {
                "name": m.get("name") or m.get("displayName", "unknown"),
                "url": m.get("url") or m.get("homepageUrl", ""),
                "status": m.get("status") or m.get("state", "unknown"),
            }
            for m in masters[:MAX_CONTROLLERS]
        ]
    jobs = data.get("jobs", [])
    return [
        {
            "name": job.get("name", "unknown"),
            "url": job.get("url", ""),
            "status": _color_to_status(job.get("color", "")),
        }
        for job in jobs[:MAX_CONTROLLERS]
    ]


def _color_to_status(color: str) -> str:
    """Convert Jenkins color indicator to a human-readable status."""
    color = (color or "").lower().replace("_anime", "")
    mapping = {
        "blue": "online",
        "green": "online",
        "red": "failing",
        "yellow": "unstable",
        "grey": "offline",
        "disabled": "disabled",
        "notbuilt": "idle",
    }
    return mapping.get(color, "unknown")
