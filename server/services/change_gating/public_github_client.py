"""Read-only GitHub client for public repositories (retro review).

The live PR review reaches GitHub through an installed GitHub App. The retro
review exists so someone can see Aurora's review on their repo *before*
installing anything, so this client needs no installation and no user
credential: it reads public data from the public REST API.

An optional server-wide token (``GITHUB_PUBLIC_READ_TOKEN``) only raises the
rate limit from 60 to 5000 requests/hour. It is never a user's credential,
it is never logged, and the client still only reads.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"

_TIMEOUT_SECONDS = 30
_PER_PAGE = 100
# A PR's file list can be up to 3000 entries; the retro review skips PRs that
# large anyway, so one page of 100 files is enough to decide.
_MAX_FILE_PAGES = 3


class GitHubRateLimited(Exception):
    """GitHub refused the request because the rate limit is exhausted."""


class GitHubRepoNotFound(Exception):
    """The repository does not exist or is not public."""


class PublicGitHubClient:
    """Thin read-only REST client scoped to one public repository."""

    def __init__(self, repo_full_name: str, token: Optional[str] = None):
        self.repo_full_name = repo_full_name
        self._token = token if token is not None else os.getenv("GITHUB_PUBLIC_READ_TOKEN") or None
        self._session = requests.Session()

    def close(self) -> None:
        self._session.close()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _headers(self, accept: str = "application/vnd.github+json") -> Dict[str, str]:
        headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None,
             accept: str = "application/vnd.github+json"):
        response = self._session.get(
            f"{GITHUB_API_BASE}{path}",
            headers=self._headers(accept),
            params=params,
            timeout=_TIMEOUT_SECONDS,
        )
        if response.status_code in (403, 429) and response.headers.get("X-RateLimit-Remaining") == "0":
            raise GitHubRateLimited(
                "GitHub API rate limit reached. Try again later, or set "
                "GITHUB_PUBLIC_READ_TOKEN on the server to raise the limit."
            )
        if response.status_code == 404:
            raise GitHubRepoNotFound(f"{self.repo_full_name} was not found or is not public")
        if response.status_code >= 400:
            # Log status + path only: never headers (token) or bodies.
            logger.error("[RetroReview] GitHub request failed: status=%s path=%s",
                         response.status_code, path)
            response.raise_for_status()
        return response

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_repo(self) -> Dict[str, Any]:
        """GET the repository; raises GitHubRepoNotFound for private/missing repos."""
        return self._get(f"/repos/{self.repo_full_name}").json()

    def search_merged_revert_prs(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Merged PRs whose title contains "Revert", newest first.

        The search API returns issue-shaped objects (number, title, body,
        html_url, closed_at), which is all the revert parser needs.
        """
        query = f'repo:{self.repo_full_name} is:pr is:merged in:title "Revert"'
        data = self._get(
            "/search/issues",
            params={"q": query, "sort": "created", "order": "desc",
                    "per_page": min(limit, _PER_PAGE)},
        ).json()
        return data.get("items") or []

    def list_recent_merged_prs(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Recently closed PRs that were merged (the control-set pool)."""
        prs = self._get(
            f"/repos/{self.repo_full_name}/pulls",
            params={"state": "closed", "sort": "updated", "direction": "desc",
                    "per_page": min(limit, _PER_PAGE)},
        ).json()
        return [pr for pr in prs if isinstance(pr, dict) and pr.get("merged_at")]

    def get_pull_request(self, pr_number: int) -> Dict[str, Any]:
        return self._get(f"/repos/{self.repo_full_name}/pulls/{pr_number}").json()

    def list_files(self, pr_number: int) -> List[Dict[str, Any]]:
        files: List[Dict[str, Any]] = []
        for page in range(1, _MAX_FILE_PAGES + 1):
            batch = self._get(
                f"/repos/{self.repo_full_name}/pulls/{pr_number}/files",
                params={"per_page": _PER_PAGE, "page": page},
            ).json()
            if not isinstance(batch, list) or not batch:
                break
            files.extend(batch)
            if len(batch) < _PER_PAGE:
                break
        return files

    def get_diff(self, pr_number: int) -> Optional[str]:
        """Unified diff, or None when GitHub refuses (406: too large)."""
        try:
            return self._get(
                f"/repos/{self.repo_full_name}/pulls/{pr_number}",
                accept="application/vnd.github.v3.diff",
            ).text
        except requests.HTTPError as exc:
            if getattr(exc.response, "status_code", None) == 406:
                return None
            raise
