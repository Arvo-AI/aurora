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

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _handle_error(self, response):
        """Build a structured error dict from a failed API response."""
        try:
            error_body = response.json()
        except Exception:
            return {"error": True, "status": response.status_code, "message": response.text}

        error_info = error_body.get("error", {})
        if isinstance(error_info, str):
            return {"error": True, "status": response.status_code, "message": error_info}

        scope_detail = error_info.get("detail", {})
        required_scopes = scope_detail.get("required", []) if isinstance(scope_detail, dict) else []
        granted_scopes = scope_detail.get("granted", []) if isinstance(scope_detail, dict) else []

        result = {
            "error": True,
            "status": response.status_code,
            "message": error_info.get("message", response.text) if isinstance(error_info, dict) else str(error_info),
        }

        if required_scopes:
            missing = [s for s in required_scopes if s not in granted_scopes]
            result["missing_scopes"] = missing
            result["required"] = required_scopes
            result["granted"] = granted_scopes

        return result

    def _get(self, url, params=None):
        """Single-resource GET. Returns response JSON or error dict."""
        response = requests.get(url, headers=self._get_headers(), params=params)
        if response.status_code != 200:
            logger.error(f"Bitbucket GET {url} failed: {response.status_code}")
            return self._handle_error(response)
        return response.json()

    def _get_raw(self, url, params=None):
        """GET that returns raw text (for diffs, logs). Returns string or error dict."""
        headers = self._get_headers()
        headers["Accept"] = "text/plain"
        response = requests.get(url, headers=headers, params=params)
        if response.status_code != 200:
            logger.error(f"Bitbucket GET (raw) {url} failed: {response.status_code}")
            return self._handle_error(response)
        return response.text

    def _post(self, url, json_data=None, data=None, files=None):
        """POST with JSON or form/multipart data. Returns response JSON or error dict."""
        headers = self._get_headers()
        if json_data is not None:
            headers["Content-Type"] = "application/json"
        response = requests.post(url, headers=headers, json=json_data, data=data, files=files)
        if response.status_code not in (200, 201):
            logger.error(f"Bitbucket POST {url} failed: {response.status_code}")
            return self._handle_error(response)
        try:
            return response.json()
        except Exception:
            return {"success": True, "status": response.status_code}

    def _put(self, url, json_data=None):
        """PUT with JSON data. Returns response JSON or error dict."""
        headers = self._get_headers()
        headers["Content-Type"] = "application/json"
        response = requests.put(url, headers=headers, json=json_data)
        if response.status_code != 200:
            logger.error(f"Bitbucket PUT {url} failed: {response.status_code}")
            return self._handle_error(response)
        return response.json()

    def _delete(self, url):
        """DELETE. Returns status dict."""
        response = requests.delete(url, headers=self._get_headers())
        if response.status_code not in (200, 204):
            logger.error(f"Bitbucket DELETE {url} failed: {response.status_code}")
            return self._handle_error(response)
        return {"success": True, "status": response.status_code}

    def _paginated_get(self, url, params=None, page_limit=100):
        """
        Follow Bitbucket pagination (``next`` link) and return all ``values``.

        Args:
            url: Initial request URL.
            params: Optional query parameters for the first request.
            page_limit: Maximum number of pages to fetch.

        Returns:
            A list of all result values across pages.
        """
        all_values = []
        headers = self._get_headers()
        page_count = 0

        while url and page_count < page_limit:
            response = requests.get(url, headers=headers, params=params)
            if response.status_code != 200:
                logger.error(f"Bitbucket API error {response.status_code}: {response.text}")
                if not all_values:
                    return self._handle_error(response)
                logger.warning("Returning partial results due to mid-pagination error")
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
        return self._get(f"{BITBUCKET_API_BASE}/user")

    # ------------------------------------------------------------------
    # Workspaces / Projects / Repos
    # ------------------------------------------------------------------

    def get_workspaces(self):
        """List all workspaces the authenticated user has access to."""
        return self._paginated_get(f"{BITBUCKET_API_BASE}/workspaces")

    def get_workspace(self, workspace):
        """Get a single workspace by slug."""
        return self._get(f"{BITBUCKET_API_BASE}/workspaces/{workspace}")

    def get_projects(self, workspace):
        """List projects in a workspace."""
        return self._paginated_get(f"{BITBUCKET_API_BASE}/workspaces/{workspace}/projects")

    def get_repositories(self, workspace):
        """List repositories in a workspace."""
        return self._paginated_get(f"{BITBUCKET_API_BASE}/repositories/{workspace}")

    def get_repository(self, workspace, repo_slug):
        """Get a single repository."""
        return self._get(f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}")

    # ------------------------------------------------------------------
    # File / Directory / Code Search
    # ------------------------------------------------------------------

    def get_file_contents(self, workspace, repo_slug, path, commit="HEAD"):
        """Get the contents of a file at a specific commit/branch."""
        url = f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/src/{commit}/{path}"
        headers = self._get_headers()
        response = requests.get(url, headers=headers)
        if response.status_code != 200:
            logger.error(f"Failed to get file {path}: {response.status_code}")
            return self._handle_error(response)
        content_type = response.headers.get("Content-Type", "")
        if "application/json" in content_type:
            return response.json()
        return {"content": response.text, "path": path, "commit": commit}

    def create_or_update_file(self, workspace, repo_slug, path, content, message, branch, author=None):
        """Create or update a file via multipart form POST to /src."""
        url = f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/src"
        form_data = {
            "message": message,
            "branch": branch,
        }
        if author:
            form_data["author"] = author
        files = {path: (path, content)}
        return self._post(url, data=form_data, files=files)

    def delete_file(self, workspace, repo_slug, path, message, branch):
        """Delete a file via POST to /src with files param."""
        url = f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/src"
        form_data = {
            "message": message,
            "branch": branch,
            "files": path,
        }
        return self._post(url, data=form_data)

    def get_directory_tree(self, workspace, repo_slug, path="", commit="HEAD"):
        """Get directory listing at a path."""
        url = f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/src/{commit}/{path}"
        params = {"format": "meta"}
        return self._get(url, params=params)

    def search_code(self, workspace, query):
        """Search code across a workspace."""
        url = f"{BITBUCKET_API_BASE}/workspaces/{workspace}/search/code"
        return self._get(url, params={"search_query": query})

    # ------------------------------------------------------------------
    # Branches
    # ------------------------------------------------------------------

    def get_branches(self, workspace, repo_slug):
        """List branches for a repository."""
        return self._paginated_get(
            f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/refs/branches"
        )

    def create_branch(self, workspace, repo_slug, name, target_hash):
        """Create a new branch from a target commit hash."""
        url = f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/refs/branches"
        return self._post(url, json_data={
            "name": name,
            "target": {"hash": target_hash},
        })

    def delete_branch(self, workspace, repo_slug, name):
        """Delete a branch."""
        url = f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/refs/branches/{name}"
        return self._delete(url)

    # ------------------------------------------------------------------
    # Commits / Diffs
    # ------------------------------------------------------------------

    def list_commits(self, workspace, repo_slug, branch=None, page_limit=5):
        """List commits, optionally filtered by branch."""
        if branch:
            url = f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/commits/{branch}"
        else:
            url = f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/commits"
        return self._paginated_get(url, page_limit=page_limit)

    def get_commit(self, workspace, repo_slug, commit_hash):
        """Get a single commit by hash."""
        url = f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/commit/{commit_hash}"
        return self._get(url)

    def get_diff(self, workspace, repo_slug, spec):
        """Get diff for a spec (commit hash, branch, or base..head range)."""
        url = f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/diff/{spec}"
        return self._get_raw(url)

    # ------------------------------------------------------------------
    # Pull Requests
    # ------------------------------------------------------------------

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

    def get_pull_request(self, workspace, repo_slug, pr_id):
        """Get a single pull request."""
        url = f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/pullrequests/{pr_id}"
        return self._get(url)

    def create_pull_request(self, workspace, repo_slug, title, source_branch, dest_branch,
                            description="", close_source=False, reviewers=None):
        """Create a new pull request."""
        url = f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/pullrequests"
        payload = {
            "title": title,
            "source": {"branch": {"name": source_branch}},
            "destination": {"branch": {"name": dest_branch}},
            "description": description,
            "close_source_branch": close_source,
        }
        if reviewers:
            payload["reviewers"] = [{"uuid": r} if isinstance(r, str) else r for r in reviewers]
        return self._post(url, json_data=payload)

    def update_pull_request(self, workspace, repo_slug, pr_id, **fields):
        """Update a pull request's fields (title, description, etc.)."""
        url = f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/pullrequests/{pr_id}"
        return self._put(url, json_data=fields)

    def merge_pull_request(self, workspace, repo_slug, pr_id, merge_strategy="merge_commit",
                           close_source=True, message=None):
        """Merge a pull request."""
        url = f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/pullrequests/{pr_id}/merge"
        payload = {
            "type": "pullrequest",
            "merge_strategy": merge_strategy,
            "close_source_branch": close_source,
        }
        if message:
            payload["message"] = message
        return self._post(url, json_data=payload)

    def approve_pull_request(self, workspace, repo_slug, pr_id):
        """Approve a pull request."""
        url = f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/pullrequests/{pr_id}/approve"
        return self._post(url)

    def unapprove_pull_request(self, workspace, repo_slug, pr_id):
        """Remove approval from a pull request."""
        url = f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/pullrequests/{pr_id}/approve"
        return self._delete(url)

    def decline_pull_request(self, workspace, repo_slug, pr_id):
        """Decline a pull request."""
        url = f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/pullrequests/{pr_id}/decline"
        return self._post(url)

    def list_pr_comments(self, workspace, repo_slug, pr_id):
        """List comments on a pull request."""
        url = f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/pullrequests/{pr_id}/comments"
        return self._paginated_get(url)

    def add_pr_comment(self, workspace, repo_slug, pr_id, content):
        """Add a comment to a pull request."""
        url = f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/pullrequests/{pr_id}/comments"
        return self._post(url, json_data={"content": {"raw": content}})

    def get_pr_diff(self, workspace, repo_slug, pr_id):
        """Get the diff for a pull request."""
        url = f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/pullrequests/{pr_id}/diff"
        return self._get_raw(url)

    def get_pr_activity(self, workspace, repo_slug, pr_id):
        """Get activity log for a pull request."""
        url = f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/pullrequests/{pr_id}/activity"
        return self._paginated_get(url)

    # ------------------------------------------------------------------
    # Issues
    # ------------------------------------------------------------------

    def get_issues(self, workspace, repo_slug):
        """List issues for a repository (requires issue tracker to be enabled)."""
        return self._paginated_get(
            f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/issues"
        )

    def get_issue(self, workspace, repo_slug, issue_id):
        """Get a single issue."""
        url = f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/issues/{issue_id}"
        return self._get(url)

    def create_issue(self, workspace, repo_slug, title, content="", kind="bug", priority="major"):
        """Create a new issue."""
        url = f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/issues"
        payload = {
            "title": title,
            "content": {"raw": content},
            "kind": kind,
            "priority": priority,
        }
        return self._post(url, json_data=payload)

    def update_issue(self, workspace, repo_slug, issue_id, **fields):
        """Update an issue's fields."""
        url = f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/issues/{issue_id}"
        return self._put(url, json_data=fields)

    def list_issue_comments(self, workspace, repo_slug, issue_id):
        """List comments on an issue."""
        url = f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/issues/{issue_id}/comments"
        return self._paginated_get(url)

    def add_issue_comment(self, workspace, repo_slug, issue_id, content):
        """Add a comment to an issue."""
        url = f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/issues/{issue_id}/comments"
        return self._post(url, json_data={"content": {"raw": content}})

    # ------------------------------------------------------------------
    # Pipelines
    # ------------------------------------------------------------------

    def list_pipelines(self, workspace, repo_slug, sort="-created_on", page_limit=3):
        """List pipelines for a repository."""
        url = f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/pipelines/"
        return self._paginated_get(url, params={"sort": sort}, page_limit=page_limit)

    def get_pipeline(self, workspace, repo_slug, pipeline_uuid):
        """Get a single pipeline."""
        url = f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/pipelines/{pipeline_uuid}"
        return self._get(url)

    def trigger_pipeline(self, workspace, repo_slug, target_branch, pattern=None, variables=None):
        """Trigger a new pipeline run."""
        url = f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/pipelines/"
        target = {
            "type": "pipeline_ref_target",
            "ref_type": "branch",
            "ref_name": target_branch,
        }
        if pattern:
            target["selector"] = {"type": "custom", "pattern": pattern}
        payload = {"target": target}
        if variables:
            payload["variables"] = [
                {"key": k, "value": v} for k, v in variables.items()
            ]
        return self._post(url, json_data=payload)

    def stop_pipeline(self, workspace, repo_slug, pipeline_uuid):
        """Stop a running pipeline."""
        url = f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/pipelines/{pipeline_uuid}/stopPipeline"
        return self._post(url)

    def list_pipeline_steps(self, workspace, repo_slug, pipeline_uuid):
        """List steps in a pipeline."""
        url = f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/pipelines/{pipeline_uuid}/steps/"
        return self._paginated_get(url)

    def get_pipeline_step(self, workspace, repo_slug, pipeline_uuid, step_uuid):
        """Get a single pipeline step."""
        url = f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/pipelines/{pipeline_uuid}/steps/{step_uuid}"
        return self._get(url)

    def get_pipeline_step_log(self, workspace, repo_slug, pipeline_uuid, step_uuid):
        """Get log output for a pipeline step."""
        url = f"{BITBUCKET_API_BASE}/repositories/{workspace}/{repo_slug}/pipelines/{pipeline_uuid}/steps/{step_uuid}/log"
        return self._get_raw(url)
