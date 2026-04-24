"""
Bitbucket Cloud API client.
Wraps the Bitbucket 2.0 REST API with authentication and pagination support.
"""
import base64
import logging
from urllib.parse import quote, unquote, urlsplit

import requests

logger = logging.getLogger(__name__)

BITBUCKET_API_BASE = "https://api.bitbucket.org/2.0"
_BITBUCKET_ALLOWED_HOSTS = frozenset({"api.bitbucket.org", "bitbucket.org"})


def _validate_bitbucket_url(url: str) -> None:
    """Raise ValueError if url does not point to a known Bitbucket host over HTTPS,
    or if the path contains traversal segments (``..``)."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ValueError(f"URL scheme '{parts.scheme}' is not allowed; only HTTPS is permitted")
    if parts.hostname not in _BITBUCKET_ALLOWED_HOSTS:
        raise ValueError(f"URL host '{parts.hostname}' is not a known Bitbucket domain")
    # Reject path traversal. Decode each segment to a fixed point so that
    # doubly-encoded forms like ``%252e%252e`` (which downstream clients may
    # re-decode into ``..``) are also rejected.
    for segment in parts.path.split("/"):
        decoded = segment
        for _ in range(3):
            previous = decoded
            decoded = unquote(decoded)
            if decoded == previous:
                break
        if decoded == "..":
            raise ValueError("URL path contains a traversal segment")


def _sanitize_url(url: str) -> str:
    """Strip query params and credentials from a URL before logging."""
    parts = urlsplit(url)
    return parts._replace(query="", fragment="", netloc=parts.hostname or parts.netloc).geturl()


class BitbucketAPIClient:
    """Client for interacting with the Bitbucket Cloud 2.0 API."""

    REQUEST_TIMEOUT = 30  # seconds

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
        _validate_bitbucket_url(url)
        response = requests.get(url, headers=self._get_headers(), params=params, timeout=self.REQUEST_TIMEOUT)
        if response.status_code != 200:
            logger.error(f"Bitbucket GET {_sanitize_url(url)} failed: {response.status_code}")
            return self._handle_error(response)
        return response.json()

    def _get_raw(self, url, params=None):
        """GET that returns raw text (for diffs, logs). Returns string or error dict."""
        _validate_bitbucket_url(url)
        headers = self._get_headers()
        headers["Accept"] = "text/plain"
        response = requests.get(url, headers=headers, params=params, timeout=self.REQUEST_TIMEOUT)
        if response.status_code != 200:
            logger.error(f"Bitbucket GET (raw) {_sanitize_url(url)} failed: {response.status_code}")
            return self._handle_error(response)
        return response.text

    def _post(self, url, json_data=None, data=None, files=None):
        """POST with JSON or form/multipart data. Returns response JSON or error dict."""
        _validate_bitbucket_url(url)
        headers = self._get_headers()
        if json_data is not None:
            headers["Content-Type"] = "application/json"
        response = requests.post(url, headers=headers, json=json_data, data=data, files=files, timeout=self.REQUEST_TIMEOUT)
        if response.status_code not in (200, 201):
            logger.error(f"Bitbucket POST {_sanitize_url(url)} failed: {response.status_code}")
            return self._handle_error(response)
        try:
            return response.json()
        except Exception:
            return {"success": True, "status": response.status_code}

    def _put(self, url, json_data=None):
        """PUT with JSON data. Returns response JSON or error dict."""
        _validate_bitbucket_url(url)
        headers = self._get_headers()
        headers["Content-Type"] = "application/json"
        response = requests.put(url, headers=headers, json=json_data, timeout=self.REQUEST_TIMEOUT)
        if response.status_code != 200:
            logger.error(f"Bitbucket PUT {_sanitize_url(url)} failed: {response.status_code}")
            return self._handle_error(response)
        return response.json()

    def _delete(self, url):
        """DELETE. Returns status dict."""
        _validate_bitbucket_url(url)
        response = requests.delete(url, headers=self._get_headers(), timeout=self.REQUEST_TIMEOUT)
        if response.status_code not in (200, 204):
            logger.error(f"Bitbucket DELETE {_sanitize_url(url)} failed: {response.status_code}")
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
            try:
                _validate_bitbucket_url(url)
            except ValueError:
                logger.warning("Pagination rejected untrusted next URL: %s", _sanitize_url(url))
                return {
                    "error": True,
                    "status": None,
                    "message": "Pagination halted: next URL failed validation",
                }
            response = requests.get(url, headers=headers, params=params, timeout=self.REQUEST_TIMEOUT)
            if response.status_code != 200:
                logger.error(f"Bitbucket API error {response.status_code} at {_sanitize_url(url)}")
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
        result = self._paginated_get(f"{BITBUCKET_API_BASE}/user/workspaces")
        if isinstance(result, dict) and result.get("error"):
            return result
        return [entry["workspace"] for entry in result if isinstance(entry, dict) and entry.get("workspace")]

    def get_workspace(self, workspace):
        """Get a single workspace by slug."""
        return self._get(f"{BITBUCKET_API_BASE}/workspaces/{quote(workspace, safe='')}")

    def get_projects(self, workspace):
        """List projects in a workspace."""
        return self._paginated_get(
            f"{BITBUCKET_API_BASE}/workspaces/{quote(workspace, safe='')}/projects"
        )

    def get_repositories(self, workspace):
        """List repositories in a workspace."""
        return self._paginated_get(
            f"{BITBUCKET_API_BASE}/repositories/{quote(workspace, safe='')}"
        )

    def get_repository(self, workspace, repo_slug):
        """Get a single repository."""
        return self._get(
            f"{BITBUCKET_API_BASE}/repositories/"
            f"{quote(workspace, safe='')}/{quote(repo_slug, safe='')}"
        )

    # ------------------------------------------------------------------
    # File / Directory / Code Search
    # ------------------------------------------------------------------

    def _resolve_commit(self, workspace, repo_slug, ref):
        """Resolve a branch/tag name to a commit hash. Returns the ref unchanged if resolution fails."""
        if ref == "HEAD" or len(ref) >= 12 and ref.isalnum():
            return ref
        result = self._get(
            f"{BITBUCKET_API_BASE}/repositories/"
            f"{quote(workspace, safe='')}/{quote(repo_slug, safe='')}"
            f"/refs/branches/{quote(ref, safe='')}"
        )
        if isinstance(result, dict) and not result.get("error"):
            target_hash = result.get("target", {}).get("hash")
            if target_hash:
                return target_hash
        return ref

    def get_file_contents(self, workspace, repo_slug, path, commit="HEAD"):
        """Get the contents of a file at a specific commit/branch."""
        commit = self._resolve_commit(workspace, repo_slug, commit)
        url = (
            f"{BITBUCKET_API_BASE}/repositories/"
            f"{quote(workspace, safe='')}/{quote(repo_slug, safe='')}"
            f"/src/{commit}/{quote(path, safe='/')}"
        )
        _validate_bitbucket_url(url)
        headers = self._get_headers()
        response = requests.get(url, headers=headers, timeout=self.REQUEST_TIMEOUT)
        if response.status_code != 200:
            logger.error(f"Failed to get file {path}: {response.status_code}")
            return self._handle_error(response)
        content_type = response.headers.get("Content-Type", "")
        if "application/json" in content_type:
            return response.json()
        return {"content": response.text, "path": path, "commit": commit}

    def create_or_update_file(self, workspace, repo_slug, path, content, message, branch, author=None):
        """Create or update a file via multipart form POST to /src."""
        url = (
            f"{BITBUCKET_API_BASE}/repositories/"
            f"{quote(workspace, safe='')}/{quote(repo_slug, safe='')}/src"
        )
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
        url = (
            f"{BITBUCKET_API_BASE}/repositories/"
            f"{quote(workspace, safe='')}/{quote(repo_slug, safe='')}/src"
        )
        form_data = {
            "message": message,
            "branch": branch,
            "files": path,
        }
        return self._post(url, data=form_data)

    def get_directory_tree(self, workspace, repo_slug, path="", commit="HEAD"):
        """Get directory listing at a path."""
        commit = self._resolve_commit(workspace, repo_slug, commit)
        url = (
            f"{BITBUCKET_API_BASE}/repositories/"
            f"{quote(workspace, safe='')}/{quote(repo_slug, safe='')}"
            f"/src/{commit}/{quote(path, safe='/')}"
        )
        params = {"format": "meta"}
        return self._get(url, params=params)

    def search_code(self, workspace, query):
        """Search code across a workspace."""
        url = f"{BITBUCKET_API_BASE}/workspaces/{quote(workspace, safe='')}/search/code"
        return self._get(url, params={"search_query": query})

    # ------------------------------------------------------------------
    # Branches
    # ------------------------------------------------------------------

    def get_branches(self, workspace, repo_slug):
        """List branches for a repository."""
        return self._paginated_get(
            f"{BITBUCKET_API_BASE}/repositories/"
            f"{quote(workspace, safe='')}/{quote(repo_slug, safe='')}/refs/branches"
        )

    def create_branch(self, workspace, repo_slug, name, target_hash):
        """Create a new branch from a target commit hash."""
        url = (
            f"{BITBUCKET_API_BASE}/repositories/"
            f"{quote(workspace, safe='')}/{quote(repo_slug, safe='')}/refs/branches"
        )
        return self._post(url, json_data={
            "name": name,
            "target": {"hash": target_hash},
        })

    def delete_branch(self, workspace, repo_slug, name):
        """Delete a branch."""
        url = (
            f"{BITBUCKET_API_BASE}/repositories/"
            f"{quote(workspace, safe='')}/{quote(repo_slug, safe='')}"
            f"/refs/branches/{quote(name, safe='')}"
        )
        return self._delete(url)

    # ------------------------------------------------------------------
    # Commits / Diffs
    # ------------------------------------------------------------------

    def list_commits(self, workspace, repo_slug, branch=None, page_limit=5):
        """List commits, optionally filtered by branch."""
        base = (
            f"{BITBUCKET_API_BASE}/repositories/"
            f"{quote(workspace, safe='')}/{quote(repo_slug, safe='')}"
        )
        if branch:
            url = f"{base}/commits/{quote(branch, safe='')}"
        else:
            url = f"{base}/commits"
        return self._paginated_get(url, page_limit=page_limit)

    def get_commit(self, workspace, repo_slug, commit_hash):
        """Get a single commit by hash."""
        url = (
            f"{BITBUCKET_API_BASE}/repositories/"
            f"{quote(workspace, safe='')}/{quote(repo_slug, safe='')}"
            f"/commit/{quote(commit_hash, safe='')}"
        )
        return self._get(url)

    def get_diff(self, workspace, repo_slug, spec):
        """Get diff for a spec (commit hash, branch, or base..head range).

        For a ``base..head`` range, each side is URL-encoded individually so
        that refs containing reserved characters remain safe while the literal
        ``..`` separator is preserved as a single path segment.
        """
        if ".." in spec:
            parts = spec.split("..")
            if len(parts) != 2:
                raise ValueError("diff spec must contain exactly one '..' separator")
            base, head = parts
            encoded_spec = f"{quote(base, safe='')}..{quote(head, safe='')}"
        else:
            encoded_spec = quote(spec, safe='')
        url = (
            f"{BITBUCKET_API_BASE}/repositories/"
            f"{quote(workspace, safe='')}/{quote(repo_slug, safe='')}"
            f"/diff/{encoded_spec}"
        )
        return self._get_raw(url)

    # ------------------------------------------------------------------
    # Pull Requests
    # ------------------------------------------------------------------

    def _pr_base(self, workspace, repo_slug, pr_id=None):
        base = (
            f"{BITBUCKET_API_BASE}/repositories/"
            f"{quote(workspace, safe='')}/{quote(repo_slug, safe='')}/pullrequests"
        )
        if pr_id is not None:
            return f"{base}/{quote(str(pr_id), safe='')}"
        return base

    def get_pull_requests(self, workspace, repo_slug, state=None):
        """
        List pull requests for a repository.

        Args:
            state: Optional PR state filter (e.g. ``OPEN``, ``MERGED``, ``DECLINED``).
        """
        params = {"state": state} if state else None
        return self._paginated_get(self._pr_base(workspace, repo_slug), params=params)

    def get_pull_request(self, workspace, repo_slug, pr_id):
        """Get a single pull request."""
        return self._get(self._pr_base(workspace, repo_slug, pr_id))

    def create_pull_request(self, workspace, repo_slug, title, source_branch, dest_branch,
                            description="", close_source=False, reviewers=None):
        """Create a new pull request."""
        url = self._pr_base(workspace, repo_slug)
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
        return self._put(self._pr_base(workspace, repo_slug, pr_id), json_data=fields)

    def merge_pull_request(self, workspace, repo_slug, pr_id, merge_strategy="merge_commit",
                           close_source=True, message=None):
        """Merge a pull request."""
        url = f"{self._pr_base(workspace, repo_slug, pr_id)}/merge"
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
        return self._post(f"{self._pr_base(workspace, repo_slug, pr_id)}/approve")

    def unapprove_pull_request(self, workspace, repo_slug, pr_id):
        """Remove approval from a pull request."""
        return self._delete(f"{self._pr_base(workspace, repo_slug, pr_id)}/approve")

    def decline_pull_request(self, workspace, repo_slug, pr_id):
        """Decline a pull request."""
        return self._post(f"{self._pr_base(workspace, repo_slug, pr_id)}/decline")

    def list_pr_comments(self, workspace, repo_slug, pr_id):
        """List comments on a pull request."""
        return self._paginated_get(f"{self._pr_base(workspace, repo_slug, pr_id)}/comments")

    def add_pr_comment(self, workspace, repo_slug, pr_id, content):
        """Add a comment to a pull request."""
        return self._post(
            f"{self._pr_base(workspace, repo_slug, pr_id)}/comments",
            json_data={"content": {"raw": content}},
        )

    def get_pr_diff(self, workspace, repo_slug, pr_id):
        """Get the diff for a pull request."""
        return self._get_raw(f"{self._pr_base(workspace, repo_slug, pr_id)}/diff")

    def get_pr_activity(self, workspace, repo_slug, pr_id):
        """Get activity log for a pull request."""
        return self._paginated_get(f"{self._pr_base(workspace, repo_slug, pr_id)}/activity")

    # ------------------------------------------------------------------
    # Issues
    # ------------------------------------------------------------------

    def _issue_base(self, workspace, repo_slug, issue_id=None):
        base = (
            f"{BITBUCKET_API_BASE}/repositories/"
            f"{quote(workspace, safe='')}/{quote(repo_slug, safe='')}/issues"
        )
        if issue_id is not None:
            return f"{base}/{quote(str(issue_id), safe='')}"
        return base

    def get_issues(self, workspace, repo_slug):
        """List issues for a repository (requires issue tracker to be enabled)."""
        return self._paginated_get(self._issue_base(workspace, repo_slug))

    def get_issue(self, workspace, repo_slug, issue_id):
        """Get a single issue."""
        return self._get(self._issue_base(workspace, repo_slug, issue_id))

    def create_issue(self, workspace, repo_slug, title, content="", kind="bug", priority="major"):
        """Create a new issue."""
        payload = {
            "title": title,
            "content": {"raw": content},
            "kind": kind,
            "priority": priority,
        }
        return self._post(self._issue_base(workspace, repo_slug), json_data=payload)

    def update_issue(self, workspace, repo_slug, issue_id, **fields):
        """Update an issue's fields."""
        return self._put(self._issue_base(workspace, repo_slug, issue_id), json_data=fields)

    def list_issue_comments(self, workspace, repo_slug, issue_id):
        """List comments on an issue."""
        return self._paginated_get(
            f"{self._issue_base(workspace, repo_slug, issue_id)}/comments"
        )

    def add_issue_comment(self, workspace, repo_slug, issue_id, content):
        """Add a comment to an issue."""
        return self._post(
            f"{self._issue_base(workspace, repo_slug, issue_id)}/comments",
            json_data={"content": {"raw": content}},
        )

    # ------------------------------------------------------------------
    # Pipelines
    # ------------------------------------------------------------------

    def _pipeline_base(self, workspace, repo_slug, pipeline_uuid=None, step_uuid=None):
        base = (
            f"{BITBUCKET_API_BASE}/repositories/"
            f"{quote(workspace, safe='')}/{quote(repo_slug, safe='')}/pipelines/"
        )
        if pipeline_uuid is None:
            return base
        base = f"{base}{quote(pipeline_uuid, safe='')}"
        if step_uuid is not None:
            base = f"{base}/steps/{quote(step_uuid, safe='')}"
        return base

    def list_pipelines(self, workspace, repo_slug, sort="-created_on", page_limit=3):
        """List pipelines for a repository."""
        return self._paginated_get(
            self._pipeline_base(workspace, repo_slug),
            params={"sort": sort},
            page_limit=page_limit,
        )

    def get_pipeline(self, workspace, repo_slug, pipeline_uuid):
        """Get a single pipeline."""
        return self._get(self._pipeline_base(workspace, repo_slug, pipeline_uuid))

    def trigger_pipeline(self, workspace, repo_slug, target_branch, pattern=None, variables=None):
        """Trigger a new pipeline run."""
        url = self._pipeline_base(workspace, repo_slug)
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
        url = f"{self._pipeline_base(workspace, repo_slug, pipeline_uuid)}/stopPipeline"
        return self._post(url)

    def list_pipeline_steps(self, workspace, repo_slug, pipeline_uuid):
        """List steps in a pipeline."""
        url = f"{self._pipeline_base(workspace, repo_slug, pipeline_uuid)}/steps/"
        return self._paginated_get(url)

    def get_pipeline_step(self, workspace, repo_slug, pipeline_uuid, step_uuid):
        """Get a single pipeline step."""
        return self._get(self._pipeline_base(workspace, repo_slug, pipeline_uuid, step_uuid))

    def get_pipeline_step_log(self, workspace, repo_slug, pipeline_uuid, step_uuid):
        """Get log output for a pipeline step."""
        url = f"{self._pipeline_base(workspace, repo_slug, pipeline_uuid, step_uuid)}/log"
        return self._get_raw(url)
