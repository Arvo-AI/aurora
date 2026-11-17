"""Read-only access to public GitHub pull requests, without the GitHub App.

Powers the no-install PR preview: a new user pastes a public PR link and
sees Aurora's pre-merge risk review before installing anything. Reads go
through :class:`GitHubPRAdapter`'s own read methods (pagination, the 406
oversized-diff fallback) with the installation token removed. Requests are
anonymous, so GitHub allows 60 per hour per server IP (a preview uses about
four).

The reader never writes. Every write method of the adapter raises, so a
preview can never post a review or comment on someone else's repository.
"""

from __future__ import annotations

import re
from typing import Dict, Optional, Tuple

from services.change_gating.github_adapter import GitHubPRAdapter

# github.com/<owner>/<repo>/pull/<number>, optionally on the Files, Commits
# or Checks tab, with an optional trailing slash, query or fragment. Owner
# and repo follow GitHub's naming rules, so nothing else can reach the URL
# the reader builds.
_PR_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/"
    r"(?P<owner>[A-Za-z0-9][A-Za-z0-9-]{0,38})/"
    r"(?P<repo>[A-Za-z0-9._-]{1,100})/"
    r"pull/(?P<number>[1-9][0-9]{0,8})"
    r"(?:/(?:files|commits|checks))?/?(?:[?#]\S*)?$"
)


def is_public_preview(trigger_metadata: object) -> bool:
    """True for a preview run, which reviews a PR from someone else's repository.

    Such a run must not write org memory: the PR's author-controlled text
    would become knowledge that later investigations read.
    """
    return (
        isinstance(trigger_metadata, dict)
        and trigger_metadata.get("source") == "change_gating"
        and trigger_metadata.get("preview") is True
    )


def parse_pr_url(url: object) -> Optional[Tuple[str, int]]:
    """Return ``("owner/repo", number)`` for a github.com PR link, else None."""
    if not isinstance(url, str):
        return None
    match = _PR_URL_RE.match(url.strip())
    if not match or match.group("repo") in (".", ".."):
        return None
    return f"{match.group('owner')}/{match.group('repo')}", int(match.group("number"))


class PublicGitHubReader(GitHubPRAdapter):
    """:class:`GitHubPRAdapter` reads, sent without an installation token."""

    def __init__(self, repo_full_name: str):
        super().__init__(installation_id=0, repo_full_name=repo_full_name)

    def _headers(self, accept: str = "application/vnd.github+json") -> Dict[str, str]:
        return {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}

    def _read_only(self, *args, **kwargs):
        raise PermissionError("PublicGitHubReader is read-only")

    post_review = _read_only
    dismiss_review = _read_only
    update_review_body = _read_only
    post_issue_comment = _read_only
    delete_issue_comment = _read_only
    supersede_review = _read_only
