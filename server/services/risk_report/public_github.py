"""Read-only GitHub REST client for public repositories.

No GitHub App, no OAuth, no installation: everything here works against a
public repo unauthenticated. ``RISK_REPORT_GITHUB_TOKEN`` (any read-only
token) is optional and only raises the rate limit from 60 to 5,000 req/hour.

Returned shapes match what ``services.change_gating`` already consumes
(GitHub ``pulls`` / ``pulls/{n}/files`` JSON), so the reviewer is reused as is.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

_API = "https://api.github.com"
_TIMEOUT = 15
_REPO_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:github\.com/)?"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/"
    r"(?P<repo>[A-Za-z0-9._-]{1,100}?)(?:\.git)?/?$"
)


class PublicGitHubError(Exception):
    """Raised for repo-not-found, private repo, or rate limiting."""


def parse_repo(repo_url: str) -> Tuple[str, str]:
    """Accept ``owner/repo`` or a github.com URL; return ``(owner, repo)``."""
    match = _REPO_RE.match((repo_url or "").strip())
    if not match:
        raise ValueError("Expected a GitHub repo like owner/repo or https://github.com/owner/repo")
    return match.group("owner"), match.group("repo")


_PR_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:github\.com/)?"
    r"(?P<owner>[A-Za-z0-9][A-Za-z0-9-]{0,38})/(?P<repo>[A-Za-z0-9._-]{1,100}?)"
    r"(?:/pull/|#)(?P<number>\d+)(?:[/?#].*)?$"
)


def parse_pr(pr_url: str) -> Tuple[str, str, int]:
    """Accept ``https://github.com/owner/repo/pull/N`` or ``owner/repo#N``."""
    match = _PR_RE.match((pr_url or "").strip())
    if not match:
        raise ValueError("Expected a PR link like https://github.com/owner/repo/pull/123")
    return match.group("owner"), match.group("repo"), int(match.group("number"))


def parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class PublicGitHub:
    def __init__(self, owner: str, repo: str, token: Optional[str] = None, session=None):
        self.owner = owner
        self.repo = repo
        self.full_name = f"{owner}/{repo}"
        self._session = session or requests.Session()
        self._session.headers.update({
            "Accept": "application/vnd.github+json",
            "User-Agent": "aurora-risk-report",
        })
        token = token if token is not None else os.getenv("RISK_REPORT_GITHUB_TOKEN")
        if token:
            self._session.headers["Authorization"] = f"Bearer {token}"
        self.requests_made = 0

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        self.requests_made += 1
        resp = self._session.get(f"{_API}{path}", params=params, timeout=_TIMEOUT)
        if resp.status_code == 404:
            raise PublicGitHubError(f"{self.full_name} not found or not public")
        if resp.status_code in (403, 429) and resp.headers.get("X-RateLimit-Remaining") == "0":
            raise PublicGitHubError(
                "GitHub rate limit reached; set RISK_REPORT_GITHUB_TOKEN or retry later"
            )
        resp.raise_for_status()
        return resp.json()

    def get_repo(self) -> Dict[str, Any]:
        data = self._get(f"/repos/{self.full_name}")
        if data.get("private"):
            raise PublicGitHubError(f"{self.full_name} is private")
        return data

    def list_merged_prs(self, days: int, max_pages: int = 3) -> List[Dict[str, Any]]:
        """Merged PRs whose ``merged_at`` falls in the last ``days`` days, newest first."""
        since = datetime.now(timezone.utc) - timedelta(days=days)
        merged: List[Dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            batch = self._get(
                f"/repos/{self.full_name}/pulls",
                {"state": "closed", "sort": "updated", "direction": "desc",
                 "per_page": 100, "page": page},
            )
            if not batch:
                break
            for pr in batch:
                merged_at = parse_ts(pr.get("merged_at"))
                if merged_at and merged_at >= since:
                    merged.append(pr)
            # Sorted by last update: once a whole page predates the window, stop.
            oldest_update = parse_ts(batch[-1].get("updated_at"))
            if len(batch) < 100 or (oldest_update and oldest_update < since):
                break
        merged.sort(key=lambda p: p["merged_at"], reverse=True)
        return merged

    def search_merged_reverts(self, days: int, limit: int = 30) -> List[Dict[str, Any]]:
        """Merged PRs with "revert" in the title, via the search API.

        Listing every closed PR only reaches back a few days on a busy repo
        (bots open and close hundreds); search goes straight to the reverts.
        Search results are issue-shaped: ``pull_request.merged_at`` holds the
        merge time.
        """
        since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
        data = self._get("/search/issues", {
            "q": f"repo:{self.full_name} is:pr is:merged in:title revert merged:>={since}",
            "sort": "created", "order": "desc", "per_page": min(limit, 100),
        })
        items = data.get("items") or []
        for item in items:
            item["merged_at"] = (item.get("pull_request") or {}).get("merged_at")
        return items

    def search_reverts_of(self, pr_number: int) -> List[Dict[str, Any]]:
        """Merged PRs that mention ``revert`` and this PR's number (one search)."""
        data = self._get("/search/issues", {
            "q": f"repo:{self.full_name} is:pr is:merged revert {pr_number}",
            "per_page": 20,
        })
        items = data.get("items") or []
        for item in items:
            item["merged_at"] = (item.get("pull_request") or {}).get("merged_at")
        return items

    def get_pr(self, pr_number: int) -> Dict[str, Any]:
        return self._get(f"/repos/{self.full_name}/pulls/{pr_number}")

    def pr_for_commit(self, sha: str) -> Optional[int]:
        """The merged PR that introduced ``sha``, if any."""
        pulls = self._get(f"/repos/{self.full_name}/commits/{sha}/pulls")
        merged = [p for p in pulls or [] if p.get("merged_at")]
        return merged[0]["number"] if merged else None

    def list_pr_files(self, pr_number: int) -> List[Dict[str, Any]]:
        """Changed files with per-file ``patch``: the PR's own diff, which a
        later revert does not alter."""
        return self._get(
            f"/repos/{self.full_name}/pulls/{pr_number}/files", {"per_page": 100}
        )
