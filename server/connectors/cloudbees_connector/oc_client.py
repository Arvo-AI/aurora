"""
CloudBees Operations Center (CJOC) client.

Discovers managed controllers and queries builds across them.
OC typically lives at `{base_url}/cjoc` or IS the base URL directly.
Uses Jenkins API format (HTTP Basic Auth with username + api_token).
"""

import logging
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from connectors.jenkins_connector.api_client import JenkinsClient

logger = logging.getLogger(__name__)

MAX_CONTROLLERS = 20
MAX_BUILDS_PER_CONTROLLER = 5
DEFAULT_TIMEOUT = 15.0


class CloudBeesOCClient:
    """Client for CloudBees Operations Center (CJOC)."""

    def __init__(self, base_url: str, username: str, api_token: str):
        self.base_url = base_url.rstrip("/")
        parsed = urlparse(self.base_url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"Invalid URL scheme: {parsed.scheme!r}. Only http and https are allowed.")
        self.username = username
        self.api_token = api_token
        self.timeout = DEFAULT_TIMEOUT
        self._http_client: Optional[httpx.Client] = None

    def _get_http_client(self) -> httpx.Client:
        if self._http_client is None or self._http_client.is_closed:
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
        """Validate that a controller URL belongs to the same domain as the OC base URL."""
        oc_host = urlparse(self.base_url).hostname
        ctrl_host = urlparse(controller_url).hostname
        if not ctrl_host or (ctrl_host != oc_host and not ctrl_host.endswith(f".{oc_host}")):
            raise ValueError(f"Controller URL {controller_url} does not match OC domain {oc_host}")

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
        except httpx.HTTPError as e:
            logger.error("OC API request failed: %s", e)
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
        """Discover managed controllers from Operations Center.

        Returns list of {name, url, status} dicts.
        """
        # Primary endpoint: masterProvisioning API
        success, data, error = self._request(
            "GET", "/cjoc/masterProvisioning/api/json"
        )

        if not success:
            # Fallback: try the root-level view which may list controllers
            success, data, error = self._request(
                "GET",
                "/cjoc/api/json",
                params={"tree": "jobs[name,url,color,description]"},
            )
            if not success:
                # Final fallback: root without /cjoc
                success, data, error = self._request(
                    "GET",
                    "/api/json",
                    params={"tree": "jobs[name,url,color,description]"},
                )

        if not success:
            return False, [], error

        controllers = []
        if data:
            # masterProvisioning response has 'masters' list
            masters = data.get("masters") or data.get("items") or []
            if masters:
                for master in masters[:MAX_CONTROLLERS]:
                    controllers.append({
                        "name": master.get("name") or master.get("displayName", "unknown"),
                        "url": master.get("url") or master.get("homepageUrl", ""),
                        "status": master.get("status") or master.get("state", "unknown"),
                    })
            else:
                # Fallback: treat top-level jobs as controllers
                jobs = data.get("jobs", [])
                for job in jobs[:MAX_CONTROLLERS]:
                    # In OC, managed controllers appear as top-level items
                    controllers.append({
                        "name": job.get("name", "unknown"),
                        "url": job.get("url", ""),
                        "status": _color_to_status(job.get("color", "")),
                    })

        return True, controllers, None

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
        """Query recent builds across all discovered controllers.

        Caps at MAX_CONTROLLERS controllers and MAX_BUILDS_PER_CONTROLLER builds each
        to avoid timeout.
        """
        success, controllers, error = self.discover_controllers()
        if not success:
            return False, [], error

        if not controllers:
            return True, [], None

        all_builds: List[Dict] = []
        errors: List[str] = []

        for controller in controllers[:MAX_CONTROLLERS]:
            controller_url = controller.get("url")
            if not controller_url:
                continue

            try:
                self._validate_controller_url(controller_url)
                client = self.get_controller_client(controller_url)
                ok, jobs, err = client.list_jobs()
                if not ok:
                    errors.append(f"{controller['name']}: Failed to query controller")
                    continue

                # Filter by service name if provided
                if service:
                    jobs = [j for j in jobs if service.lower() in (j.get("name", "") or "").lower()]

                for job in jobs[:5]:  # Limit jobs per controller
                    job_name = job.get("name") or job.get("fullName")
                    if not job_name:
                        continue

                    ok, builds, err = client.list_builds(job_name, limit=MAX_BUILDS_PER_CONTROLLER)
                    if ok and builds:
                        for build in builds:
                            build["_controller"] = controller["name"]
                            build["_job"] = job_name
                        all_builds.extend(builds)

            except Exception as e:
                logger.warning(
                    "Failed to query controller %s: %s", controller.get("name"), e
                )
                errors.append(f"{controller.get('name', 'unknown')}: Failed to query controller")

        # Sort by timestamp descending
        all_builds.sort(key=lambda b: b.get("timestamp", 0), reverse=True)

        return True, all_builds, "; ".join(errors) if errors else None


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
