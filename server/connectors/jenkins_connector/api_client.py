"""
Jenkins API client (read-only).

Provides methods for reading data from a Jenkins instance:
- Server info
- Jobs and builds
- Build console output
- Build queue
- Views
- Nodes (agents)
"""

import logging
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote as url_quote

import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)


class JenkinsClient:
    """
    Read-only Jenkins API client.

    All interactions use HTTP Basic Auth with a username + API token.
    """

    def __init__(self, base_url: str, username: str, api_token: str):
        self.base_url = base_url.rstrip("/")
        self.auth = HTTPBasicAuth(username, api_token)
        self.timeout = 30

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict] = None,
        accept: str = "application/json",
    ) -> Tuple[bool, Optional[Any], Optional[str]]:
        """
        Make an API request to Jenkins.

        Returns:
            Tuple of (success, response_data, error_message)
        """
        url = f"{self.base_url}{path}"

        try:
            response = requests.request(
                method=method,
                url=url,
                auth=self.auth,
                params=params,
                headers={"Accept": accept},
                timeout=self.timeout,
            )

            if response.status_code == 401:
                return False, None, "Unauthorized. Token may be expired or revoked."
            if response.status_code == 403:
                return False, None, "Forbidden. Insufficient permissions."
            if response.status_code == 404:
                return False, None, "Resource not found."

            if not response.ok:
                return False, None, f"Jenkins API error ({response.status_code})"

            if accept != "application/json":
                return True, response.text, None

            if not response.text:
                return True, None, None

            return True, response.json(), None

        except requests.exceptions.Timeout:
            return False, None, "Request timeout"
        except requests.exceptions.RequestException as e:
            logger.error("Jenkins API request failed: %s", e)
            return False, None, f"Connection error: {str(e)}"
        except Exception as e:
            logger.error("Unexpected error in Jenkins API request: %s", e)
            return False, None, str(e)

    # =========================================================================
    # Server Info
    # =========================================================================

    def get_server_info(self) -> Tuple[bool, Optional[Dict], Optional[str]]:
        """Get top-level Jenkins server information."""
        return self._request("GET", "/api/json")

    # =========================================================================
    # Jobs
    # =========================================================================

    def list_jobs(
        self, folder_path: Optional[str] = None
    ) -> Tuple[bool, List[Dict], Optional[str]]:
        """
        List jobs, optionally within a folder.

        Args:
            folder_path: Slash-separated folder path (e.g. "folder/subfolder").
                         None for root-level jobs.
        """
        tree = "jobs[name,url,color,fullName,description,buildable,inQueue,lastBuild[number,result,timestamp,duration]]"

        if folder_path:
            segments = "/".join(
                f"job/{url_quote(seg, safe='')}" for seg in folder_path.split("/") if seg
            )
            path = f"/{segments}/api/json"
        else:
            path = "/api/json"

        success, data, error = self._request("GET", path, params={"tree": tree})
        if not success:
            return False, [], error

        jobs = data.get("jobs", []) if data else []
        return True, jobs, None

    def get_job(self, job_path: str) -> Tuple[bool, Optional[Dict], Optional[str]]:
        """
        Get details for a specific job.

        Args:
            job_path: Slash-separated job path (e.g. "folder/job-name")
        """
        segments = "/".join(
            f"job/{url_quote(seg, safe='')}" for seg in job_path.split("/") if seg
        )
        return self._request("GET", f"/{segments}/api/json")

    # =========================================================================
    # Builds
    # =========================================================================

    def list_builds(
        self, job_path: str, limit: int = 20
    ) -> Tuple[bool, List[Dict], Optional[str]]:
        """
        List recent builds for a job.

        Args:
            job_path: Slash-separated job path
            limit: Max number of builds to return
        """
        segments = "/".join(
            f"job/{url_quote(seg, safe='')}" for seg in job_path.split("/") if seg
        )
        tree = (
            f"builds[number,url,result,timestamp,duration,displayName,building,description]"
            f"{{0,{limit}}}"
        )
        success, data, error = self._request(
            "GET", f"/{segments}/api/json", params={"tree": tree}
        )
        if not success:
            return False, [], error

        builds = data.get("builds", []) if data else []
        return True, builds, None

    def get_build(
        self, job_path: str, build_number: int
    ) -> Tuple[bool, Optional[Dict], Optional[str]]:
        """Get details for a specific build."""
        segments = "/".join(
            f"job/{url_quote(seg, safe='')}" for seg in job_path.split("/") if seg
        )
        return self._request("GET", f"/{segments}/{build_number}/api/json")

    def get_build_console(
        self, job_path: str, build_number: int
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """Get console output for a build (plain text)."""
        segments = "/".join(
            f"job/{url_quote(seg, safe='')}" for seg in job_path.split("/") if seg
        )
        return self._request(
            "GET",
            f"/{segments}/{build_number}/consoleText",
            accept="text/plain",
        )

    # =========================================================================
    # Queue
    # =========================================================================

    def get_queue(self) -> Tuple[bool, Optional[Dict], Optional[str]]:
        """Get the current build queue."""
        return self._request("GET", "/queue/api/json")

    # =========================================================================
    # Views
    # =========================================================================

    def list_views(self) -> Tuple[bool, List[Dict], Optional[str]]:
        """List all views."""
        tree = "views[name,url,description,jobs[name,url,color]]"
        success, data, error = self._request("GET", "/api/json", params={"tree": tree})
        if not success:
            return False, [], error

        views = data.get("views", []) if data else []
        return True, views, None

    # =========================================================================
    # Nodes
    # =========================================================================

    def list_nodes(self) -> Tuple[bool, List[Dict], Optional[str]]:
        """List all build agents / nodes."""
        success, data, error = self._request("GET", "/computer/api/json")
        if not success:
            return False, [], error

        computers = data.get("computer", []) if data else []

        formatted = []
        for node in computers:
            formatted.append(
                {
                    "displayName": node.get("displayName", ""),
                    "description": node.get("description", ""),
                    "offline": node.get("offline", False),
                    "temporarilyOffline": node.get("temporarilyOffline", False),
                    "idle": node.get("idle", True),
                    "numExecutors": node.get("numExecutors", 0),
                    "offlineCauseReason": node.get("offlineCauseReason", ""),
                    "monitorData": node.get("monitorData", {}),
                }
            )

        return True, formatted, None
