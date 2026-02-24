"""
Bitbucket Cloud API client.
Wraps the Bitbucket 2.0 REST API with authentication and pagination support.
"""
import base64
import logging
import requests

logger = logging.getLogger(__name__)

BITBUCKET_API_BASE = "https://api.bitbucket.org/2.0"


class BitbucketAPIClient:
    """Client for interacting with the Bitbucket Cloud 2.0 API."""

    def __init__(self, access_token, auth_type="oauth", email=None):
        """
        Args:
            access_token: OAuth access token or API token.
            auth_type: ``"oauth"`` or ``"api_token"``.
            email: Required when *auth_type* is ``"api_token"`` (used for Basic Auth).
        """
        self.access_token = access_token
        self.auth_type = auth_type
        self.email = email

    def _get_headers(self):
        """Build the Authorization header based on auth_type."""
        if self.auth_type == "api_token":
            credentials = base64.b64encode(
                f"{self.email}:{self.access_token}".encode()
            ).decode()
            auth_value = f"Basic {credentials}"
        else:
            auth_value = f"Bearer {self.access_token}"

        return {"Authorization": auth_value, "Accept": "application/json"}

    def _paginated_get(self, url, params=None):
        """
        Follow Bitbucket pagination (``next`` link) and return all ``values``.

        Args:
            url: Initial request URL.
            params: Optional query parameters for the first request.

        Returns:
            A list of all result values across pages.
        """
        all_values = []
        headers = self._get_headers()
        page_count = 0
        max_pages = 100  # safety limit

        while url and page_count < max_pages:
            response = requests.get(url, headers=headers, params=params)
            if response.status_code != 200:
                logger.error(f"Bitbucket API error {response.status_code}: {response.text}")
                break

            data = response.json()
            all_values.extend(data.get("values", []))

            url = data.get("next")
            params = None  # params already encoded in the ``next`` URL
            page_count += 1

        return all_values

    # ------------------------------------------------------------------
    # User
    # ------------------------------------------------------------------

    def get_current_user(self):
        """Get the authenticated user's profile.

        Returns:
            User profile dict on success, or a dict with ``"error"`` key on failure.
        """
        response = requests.get(
            f"{BITBUCKET_API_BASE}/user",
            headers=self._get_headers(),
        )
        if response.status_code != 200:
            logger.error(f"Failed to get Bitbucket user: {response.status_code} {response.text}")
            return self._parse_user_error(response)
        return response.json()

    def _parse_user_error(self, response):
        """Build a structured error dict from a failed /user response.

        Bitbucket scope errors include ``error.detail.required`` and
        ``error.detail.granted`` lists, which we surface as ``missing_scopes``.
        """
        try:
            error_body = response.json()
        except Exception:
            return {"error": True, "status": response.status_code, "message": response.text}

        error_info = error_body.get("error", {})
        scope_detail = error_info.get("detail", {})
        required_scopes = scope_detail.get("required", [])
        granted_scopes = scope_detail.get("granted", [])

        result = {
            "error": True,
            "status": response.status_code,
            "message": error_info.get("message", response.text),
        }

        if required_scopes:
            missing = [s for s in required_scopes if s not in granted_scopes]
            result["missing_scopes"] = missing
            result["required"] = required_scopes
            result["granted"] = granted_scopes

        return result

    # ------------------------------------------------------------------
    # Workspaces / Projects / Repos
    # ------------------------------------------------------------------

    def get_workspaces(self):
        """List all workspaces the authenticated user has access to."""
        return self._paginated_get(f"{BITBUCKET_API_BASE}/workspaces")

    def get_projects(self, workspace):
        """List projects in a workspace."""
        return self._paginated_get(f"{BITBUCKET_API_BASE}/workspaces/{workspace}/projects")

    def get_repositories(self, workspace):
        """List repositories in a workspace."""
        return self._paginated_get(f"{BITBUCKET_API_BASE}/repositories/{workspace}")

    # ------------------------------------------------------------------
    # Branches / PRs / Issues
    # ------------------------------------------------------------------

    def get_branches(self, workspace, repo_slug):
        """List branches for a repository."""
        return self._paginated_get(
            f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/refs/branches"
        )

    def get_pull_requests(self, workspace, repo_slug, state=None):
        """
        List pull requests for a repository.

        Args:
            state: Optional PR state filter (e.g. ``OPEN``, ``MERGED``, ``DECLINED``).
        """
        params = {"state": state} if state else None
        return self._paginated_get(
            f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/pullrequests",
            params=params,
        )

    def get_issues(self, workspace, repo_slug):
        """List issues for a repository (requires issue tracker to be enabled)."""
        return self._paginated_get(
            f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/issues"
        )
