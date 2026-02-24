"""
Jenkins API client (read-only).

Provides methods for reading data from a Jenkins instance:
- Server info
- Jobs and builds
- Build console output
- Build queue
- Nodes (agents)
"""

import logging
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote as url_quote

import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)


class JenkinsClient:
    """Read-only Jenkins API client using HTTP Basic Auth."""

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
        """Make an API request. Returns (success, data, error)."""
        url = f"{self.base_url}{path}"
        try:
            response = requests.request(
                method=method, url=url, auth=self.auth,
                params=params, headers={"Accept": accept}, timeout=self.timeout,
            )

            if response.status_code == 401:
                return False, None, "Invalid credentials. Check your username and API token."
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
            return False, None, "Connection timeout. Verify the Jenkins URL is reachable."
        except requests.exceptions.ConnectionError:
            return False, None, "Cannot connect to Jenkins. Verify the URL and network access."
        except requests.exceptions.RequestException as e:
            logger.error("Jenkins API request failed: %s", e)
            # Return a generic, user-safe error message without exposing exception details.
            return False, None, "Cannot connect to Jenkins. Verify the URL and network access."

    @staticmethod
    def _job_segments(job_path: str) -> str:
        """Convert 'folder/job-name' to '/job/folder/job/job-name'."""
        return "/".join(
            f"job/{url_quote(seg, safe='')}" for seg in job_path.split("/") if seg
        )

    def get_server_info(self) -> Tuple[bool, Optional[Dict], Optional[str]]:
        """Get top-level Jenkins server information (excludes jobs for efficiency)."""
        tree = "mode,nodeDescription,numExecutors,useSecurity"
        return self._request("GET", "/api/json", params={"tree": tree})

    def list_jobs(self, folder_path: Optional[str] = None) -> Tuple[bool, List[Dict], Optional[str]]:
        """List jobs, optionally within a folder."""
        tree = "jobs[name,url,color,fullName,description,buildable,inQueue,lastBuild[number,result,timestamp,duration]]"
        path = f"/{self._job_segments(folder_path)}/api/json" if folder_path else "/api/json"
        success, data, error = self._request("GET", path, params={"tree": tree})
        if not success:
            return False, [], error
        return True, (data.get("jobs", []) if data else []), None

    def get_job(self, job_path: str) -> Tuple[bool, Optional[Dict], Optional[str]]:
        """Get details for a specific job."""
        return self._request("GET", f"/{self._job_segments(job_path)}/api/json")

    def list_builds(self, job_path: str, limit: int = 20) -> Tuple[bool, List[Dict], Optional[str]]:
        """List recent builds for a job."""
        tree = f"builds[number,url,result,timestamp,duration,displayName,building,description]{{0,{limit}}}"
        success, data, error = self._request(
            "GET", f"/{self._job_segments(job_path)}/api/json", params={"tree": tree}
        )
        if not success:
            return False, [], error
        return True, (data.get("builds", []) if data else []), None

    def get_build(self, job_path: str, build_number: int) -> Tuple[bool, Optional[Dict], Optional[str]]:
        """Get details for a specific build."""
        return self._request("GET", f"/{self._job_segments(job_path)}/{build_number}/api/json")

    MAX_CONSOLE_BYTES = 1_000_000  # 1 MB

    def get_build_console(self, job_path: str, build_number: int) -> Tuple[bool, Optional[str], Optional[str]]:
        """Get console output for a build (plain text), truncated to MAX_CONSOLE_BYTES."""
        success, text, error = self._request(
            "GET",
            f"/{self._job_segments(job_path)}/{build_number}/consoleText",
            accept="text/plain",
        )
        if success and text and len(text) > self.MAX_CONSOLE_BYTES:
            text = text[: self.MAX_CONSOLE_BYTES] + "\n\n--- Output truncated (exceeded 1 MB) ---\n"
        return success, text, error

    def get_queue(self) -> Tuple[bool, Optional[Dict], Optional[str]]:
        """Get the current build queue."""
        return self._request("GET", "/queue/api/json")

    def list_nodes(self) -> Tuple[bool, List[Dict], Optional[str]]:
        """List all build agents / nodes."""
        success, data, error = self._request("GET", "/computer/api/json")
        if not success:
            return False, [], error

        nodes = []
        for node in (data.get("computer", []) if data else []):
            nodes.append({
                "displayName": node.get("displayName", ""),
                "offline": node.get("offline", False),
                "idle": node.get("idle", True),
                "numExecutors": node.get("numExecutors", 0),
            })
        return True, nodes, None
