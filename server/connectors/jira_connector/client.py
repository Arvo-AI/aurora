"""Jira REST API client supporting Cloud (v3) and Data Center (v2)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

JIRA_OAUTH_API_BASE = "https://api.atlassian.com/ex/jira"


def build_jira_oauth_api_base(cloud_id: str) -> str:
    """Cloud REST API v3 base via Atlassian gateway."""
    return f"{JIRA_OAUTH_API_BASE}/{cloud_id}/rest/api/3"


def build_jira_dc_api_base(base_url: str) -> str:
    """Data Center REST API v2 base."""
    base_url = base_url.rstrip("/")
    return f"{base_url}/rest/api/2"


class JiraClient:
    """Jira API client for Cloud (OAuth, REST v3) and Data Center (PAT, REST v2)."""

    def __init__(
        self,
        base_url: str,
        access_token: str,
        auth_type: str = "oauth",
        timeout: int = 30,
        cloud_id: Optional[str] = None,
    ):
        self.base_url = base_url.rstrip("/") if base_url else ""
        self.cloud_id = cloud_id
        self.auth_type = auth_type
        self.access_token = access_token
        self.timeout = timeout

        if auth_type == "oauth" and cloud_id:
            self.api_base = build_jira_oauth_api_base(cloud_id)
        else:
            self.api_base = build_jira_dc_api_base(self.base_url)

        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        if auth_type not in {"oauth", "pat"}:
            logger.warning("Unknown Jira auth_type=%s; defaulting to Bearer token.", auth_type)

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"{self.api_base}{path}"
        try:
            response = requests.request(
                method,
                url,
                headers=self.headers,
                params=params,
                json=json_body,
                timeout=self.timeout,
            )
            response.raise_for_status()
            if response.status_code == 204:
                return {}
            return response.json()
        except requests.RequestException as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if getattr(exc, "request", None) is not None:
                exc.request.headers = {
                    k: ("***" if k.lower() == "authorization" else v)
                    for k, v in (exc.request.headers or {}).items()
                }
            logger.error("Jira API request failed: %s %s (%s) status=%s", method, path, type(exc).__name__, status_code)
            raise

    # ------------------------------------------------------------------
    # Authentication / validation
    # ------------------------------------------------------------------

    def get_myself(self) -> Dict[str, Any]:
        """Validate credentials by fetching the current user."""
        return self._request("GET", "/myself")

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    _DEFAULT_SEARCH_FIELDS = [
        "summary", "status", "assignee", "priority",
        "created", "updated", "labels", "issuetype", "project",
    ]

    def search_issues(
        self,
        jql: str,
        fields: Optional[List[str]] = None,
        max_results: int = 20,
        start_at: int = 0,
    ) -> Dict[str, Any]:
        """Search issues via JQL.

        Uses /search/jql (Atlassian migrated from /search — CHANGE-2046, March 2026).
        Falls back to /search for Data Center instances that may not have the new endpoint.
        """
        search_fields = fields or self._DEFAULT_SEARCH_FIELDS

        new_body: Dict[str, Any] = {
            "jql": jql,
            "maxResults": max_results,
            "fields": search_fields,
        }

        try:
            result = self._request("POST", "/search/jql", json_body=new_body)
            if "total" not in result:
                result["total"] = len(result.get("issues", []))
            return result
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 405):
                logger.info("Jira /search/jql not available, falling back to /search")
                legacy_body: Dict[str, Any] = {
                    "jql": jql,
                    "maxResults": max_results,
                    "startAt": start_at,
                }
                if fields:
                    legacy_body["fields"] = fields
                return self._request("POST", "/search", json_body=legacy_body)
            raise

    # ------------------------------------------------------------------
    # Issue CRUD
    # ------------------------------------------------------------------

    def get_issue(
        self,
        issue_key: str,
        fields: Optional[List[str]] = None,
        expand: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get a single issue by key or ID."""
        params: Dict[str, Any] = {}
        if fields:
            params["fields"] = ",".join(fields)
        if expand:
            params["expand"] = expand
        return self._request("GET", f"/issue/{issue_key}", params=params)

    def create_issue(
        self,
        project_key: str,
        summary: str,
        issue_type: str = "Task",
        description_adf: Optional[Dict[str, Any]] = None,
        labels: Optional[List[str]] = None,
        parent_key: Optional[str] = None,
        extra_fields: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create a new issue. *description_adf* must be in ADF format for Cloud."""
        fields: Dict[str, Any] = {
            "project": {"key": project_key},
            "summary": summary,
            "issuetype": {"name": issue_type},
        }
        if description_adf:
            fields["description"] = description_adf
        if labels:
            fields["labels"] = labels
        if parent_key:
            fields["parent"] = {"key": parent_key}
        if extra_fields:
            fields.update(extra_fields)

        return self._request("POST", "/issue", json_body={"fields": fields})

    def update_issue(
        self,
        issue_key: str,
        fields: Optional[Dict[str, Any]] = None,
        update: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Update issue fields."""
        body: Dict[str, Any] = {}
        if fields:
            body["fields"] = fields
        if update:
            body["update"] = update
        return self._request("PUT", f"/issue/{issue_key}", json_body=body)

    def create_subtask(
        self,
        parent_key: str,
        project_key: str,
        summary: str,
        description_adf: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create a subtask linked to a parent issue."""
        return self.create_issue(
            project_key=project_key,
            summary=summary,
            issue_type="Sub-task",
            description_adf=description_adf,
            parent_key=parent_key,
        )

    # ------------------------------------------------------------------
    # Comments
    # ------------------------------------------------------------------

    def add_comment(
        self,
        issue_key: str,
        body_adf: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Add a comment to an issue. *body_adf* must be in ADF format for Cloud."""
        return self._request(
            "POST",
            f"/issue/{issue_key}/comment",
            json_body={"body": body_adf},
        )

    # ------------------------------------------------------------------
    # Issue links
    # ------------------------------------------------------------------

    def link_issues(
        self,
        inward_key: str,
        outward_key: str,
        link_type: str = "Relates",
    ) -> Dict[str, Any]:
        """Create a link between two issues."""
        return self._request(
            "POST",
            "/issueLink",
            json_body={
                "type": {"name": link_type},
                "inwardIssue": {"key": inward_key},
                "outwardIssue": {"key": outward_key},
            },
        )

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    def get_projects(self, max_results: int = 50) -> List[Dict[str, Any]]:
        """List accessible projects."""
        result = self._request("GET", "/project/search", params={"maxResults": max_results})
        return result.get("values", [])

    def get_issue_types(self, project_key: str) -> List[Dict[str, Any]]:
        """List issue types for a project."""
        result = self._request("GET", f"/project/{project_key}")
        return result.get("issueTypes", [])
