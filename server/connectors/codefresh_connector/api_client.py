"""
Codefresh API client (read-only).

Provides methods for reading data from a Codefresh instance:
- Pipelines and projects
- Builds (workflows)
- Build logs (progress)
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)


class CodefreshClient:
    """Read-only Codefresh API client using API key auth via Authorization header."""

    def __init__(self, base_url: str, api_token: str):
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.timeout = 30

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict] = None,
        accept: str = "application/json",
    ) -> Tuple[bool, Optional[Any], Optional[str]]:
        """Make an API request. Returns (success, data, error)."""
        url = f"{self.base_url}{path}"
        try:
            response = requests.request(
                method=method,
                url=url,
                params=params,
                headers={
                    "Authorization": self.api_token,
                    "Accept": accept,
                },
                timeout=self.timeout,
            )

            if response.status_code == 401:
                return False, None, "Invalid API key. Check your Codefresh API token."
            if response.status_code == 403:
                return False, None, "Forbidden. Insufficient permissions."
            if response.status_code == 404:
                return False, None, "Resource not found."
            if not response.ok:
                return False, None, f"Codefresh API error ({response.status_code})"

            if accept != "application/json":
                return True, response.text, None
            if not response.text:
                return True, None, None
            try:
                return True, response.json(), None
            except ValueError:
                logger.warning("Codefresh API returned non-JSON for %s %s", method, path)
                return False, None, "Unexpected response format from Codefresh."

        except requests.exceptions.Timeout:
            return False, None, "Connection timeout. Verify the Codefresh URL is reachable."
        except requests.exceptions.ConnectionError:
            return False, None, "Cannot connect to Codefresh. Verify the URL and network access."
        except requests.exceptions.RequestException as e:
            logger.error("Codefresh API request failed: %s", e)
            return False, None, "Cannot connect to Codefresh. Verify the URL and network access."

    # ------------------------------------------------------------------
    # Credential validation
    # ------------------------------------------------------------------

    def validate_credentials(self) -> Tuple[bool, Optional[Dict], Optional[str]]:
        """Validate API key with a lightweight pipelines call."""
        return self._request("GET", "/api/pipelines", params={"limit": "1"})

    # ------------------------------------------------------------------
    # Pipelines
    # ------------------------------------------------------------------

    def list_pipelines(
        self, project: Optional[str] = None, limit: int = 25
    ) -> Tuple[bool, Optional[Dict], Optional[str]]:
        """List pipelines, optionally filtered by project."""
        params: Dict[str, Any] = {"limit": str(limit)}
        if project:
            params["project"] = project
        return self._request("GET", "/api/pipelines", params=params)

    def get_pipeline(self, name: str) -> Tuple[bool, Optional[Dict], Optional[str]]:
        """Get a single pipeline by name (URL-encoded path)."""
        from urllib.parse import quote
        return self._request("GET", f"/api/pipelines/{quote(name, safe='')}")

    # ------------------------------------------------------------------
    # Builds (workflows)
    # ------------------------------------------------------------------

    def list_builds(
        self,
        pipeline_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 20,
    ) -> Tuple[bool, Optional[List], Optional[str]]:
        """List recent builds via the workflow endpoint."""
        params: Dict[str, Any] = {"limit": str(limit)}
        if pipeline_id:
            params["pipeline"] = pipeline_id
        if status:
            params["status"] = status
        return self._request("GET", "/api/workflow", params=params)

    def get_build(self, build_id: str) -> Tuple[bool, Optional[Dict], Optional[str]]:
        """Get details for a single build."""
        return self._request("GET", f"/api/builds/{build_id}")

    # ------------------------------------------------------------------
    # Build logs
    # ------------------------------------------------------------------

    MAX_LOG_BYTES = 1_000_000  # 1 MB

    def get_build_logs(self, build_id: str) -> Tuple[bool, Optional[Any], Optional[str]]:
        """Get build log / progress output."""
        success, data, error = self._request("GET", f"/api/progress/{build_id}")
        if success and isinstance(data, str) and len(data) > self.MAX_LOG_BYTES:
            data = data[: self.MAX_LOG_BYTES] + "\n\n--- Output truncated (exceeded 1 MB) ---\n"
        return success, data, error

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------

    def list_projects(self) -> Tuple[bool, Optional[List], Optional[str]]:
        """List all projects."""
        return self._request("GET", "/api/pipelines/projects/all")
