"""GitHub Pull Request adapter for PR change gating.

Keeps all provider-specific GitHub API calls (fetch PR / diff / files,
post / dismiss / update reviews) behind one class so adding GitLab or
Bitbucket later means writing a new adapter, not rewriting the Celery
task (design doc section 11).

Security
--------
- Installation tokens are minted lazily per request via
  ``get_installation_token`` (which caches internally with a
  per-installation refresh lock) and are NEVER logged.
- Error paths log only the HTTP status and URL path; any response body
  excerpt included in logs is passed through ``redact_token`` first.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any, Dict, List, Optional

import requests

from utils.auth.github_app_token import get_installation_token
from utils.auth.log_redact import redact_token

logger = logging.getLogger(__name__)

# Matches the rest of the codebase (utils/auth/github_app_token.py,
# tools/github_rca_tool.py, routes/github/github_app.py): the GitHub API
# base is hardcoded — there is no env-overridable base URL pattern.
GITHUB_API_BASE = "https://api.github.com"

_TIMEOUT_SECONDS = 30
_PER_PAGE = 100
# Safety cap mirroring the bounded pagination loops elsewhere in the
# codebase (routes/github/github_user_repos.py): 30 pages x 100 = 3000
# items, GitHub's own ceiling for PR file listings.
_MAX_PAGES = 30

# Hidden HTML-comment marker appended to every Aurora review body so a
# later run can find its own prior review without a bot-user-id lookup.
# The payload is base64 (not raw JSON) because findings text could
# contain "--", which terminates HTML comments.
_MARKER_PREFIX = "aurora-change-gating"
_MARKER_VERSION = 1
# v1-strict: only payloads this code knows how to interpret.
_MARKER_RE = re.compile(rf"<!-- {_MARKER_PREFIX}:v{_MARKER_VERSION} ([A-Za-z0-9+/=]+) -->")
# Any-version: identifies a review as Aurora's even when the payload
# format is newer than this code (mixed-version fleet / rollback).
_MARKER_ANY_VERSION_RE = re.compile(rf"<!-- {_MARKER_PREFIX}:v\d+ [A-Za-z0-9+/=]+ -->")


def encode_marker(findings: List[Dict[str, Any]], head_sha: str) -> str:
    """Encode findings + head SHA into a hidden HTML-comment marker."""
    payload = {"v": _MARKER_VERSION, "head_sha": head_sha, "findings": findings}
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    return f"<!-- {_MARKER_PREFIX}:v{_MARKER_VERSION} {encoded} -->"


def has_aurora_marker(body: Optional[str]) -> bool:
    """True when the body carries an Aurora marker of ANY version."""
    return bool(body) and _MARKER_ANY_VERSION_RE.search(body) is not None


def decode_marker(body: Optional[str]) -> Optional[Dict[str, Any]]:
    """Extract and decode the Aurora v1 marker from a review body.

    Returns the decoded dict (keys ``head_sha``, ``findings``) or None on
    any failure — missing/newer-version marker, bad base64, bad JSON,
    non-dict payload.
    """
    if not body:
        return None
    match = _MARKER_RE.search(body)
    if not match:
        return None
    try:
        decoded = json.loads(base64.b64decode(match.group(1)).decode("utf-8"))
    except ValueError:
        # binascii.Error, UnicodeDecodeError and JSONDecodeError all subclass
        # ValueError, so this catches bad base64, bad UTF-8 and bad JSON alike.
        return None
    if not isinstance(decoded, dict):
        return None
    return decoded


def find_aurora_reviews(reviews: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return ALL of Aurora's own reviews, in chronological order.

    A review qualifies only when BOTH hold:

    - its body carries an Aurora marker (any version, so newer-format
      reviews are still recognized and superseded), AND
    - its author is a Bot account (``user.type == "Bot"``) — a human
      copy-pasting or crafting a marker into their own review must not
      be able to hijack the prior-review context (prompt-injection
      surface) or redirect the supersede step.
    """
    out: List[Dict[str, Any]] = []
    for review in reviews or []:
        if not isinstance(review, dict):
            continue
        if not has_aurora_marker(review.get("body")):
            continue
        user = review.get("user") or {}
        if isinstance(user, dict) and user.get("type") == "Bot":
            out.append(review)
    return out


def find_latest_aurora_review(reviews: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return Aurora's most recent review (``list_reviews`` is chronological)."""
    aurora = find_aurora_reviews(reviews)
    return aurora[-1] if aurora else None


class GitHubPRAdapter:
    """Thin GitHub REST client scoped to one installation + repository."""

    def __init__(self, installation_id: int, repo_full_name: str):
        self.installation_id = installation_id
        self.repo_full_name = repo_full_name
        # Keep-alive connection reuse across the 6-8 sequential calls per
        # investigation (same pattern as the connector clients elsewhere).
        self._session = requests.Session()

    def close(self):
        """Close the underlying HTTP session to release TCP connections."""
        self._session.close()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _headers(self, accept: str = "application/vnd.github+json") -> Dict[str, str]:
        """Build request headers, minting the installation token lazily.

        ``get_installation_token`` caches per installation, so per-call
        minting is cheap. The token is never logged.
        """
        token = get_installation_token(self.installation_id)
        return {
            "Authorization": f"Bearer {token}",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _url(self, path: str) -> str:
        return f"{GITHUB_API_BASE}/repos/{self.repo_full_name}{path}"

    def _raise_for_status(self, response, path: str) -> None:
        """Log status + URL path (never the token) and raise on 4xx/5xx."""
        if response.status_code >= 400:
            logger.error(
                "[ChangeGating] GitHub API request failed: status=%s path=%s",
                response.status_code,
                path,
            )
            response.raise_for_status()

    def _get_paginated(self, path: str) -> List[Dict[str, Any]]:
        """GET all pages of a list endpoint (per_page=100, capped)."""
        results: List[Dict[str, Any]] = []
        page = 1
        while True:
            response = self._session.get(
                self._url(path),
                headers=self._headers(),
                params={"per_page": _PER_PAGE, "page": page},
                timeout=_TIMEOUT_SECONDS,
            )
            self._raise_for_status(response, path)
            batch = response.json()
            if not isinstance(batch, list) or not batch:
                break
            results.extend(batch)
            if len(batch) < _PER_PAGE:
                break
            page += 1
            if page > _MAX_PAGES:
                logger.warning(
                    "[ChangeGating] pagination cap hit: path=%s pages=%s items=%s",
                    path, _MAX_PAGES, len(results),
                )
                break
        return results

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_pull_request(self, pr_number: int) -> Dict[str, Any]:
        """GET the PR object."""
        path = f"/pulls/{pr_number}"
        response = self._session.get(
            self._url(path), headers=self._headers(), timeout=_TIMEOUT_SECONDS
        )
        self._raise_for_status(response, path)
        return response.json()

    def _get_diff_text(self, path: str, swallow_statuses: tuple) -> Optional[str]:
        """GET a unified diff (diff media type) at ``path``.

        Returns None when the response status is in ``swallow_statuses`` —
        GitHub's way of saying the diff is unavailable (406 oversized, 404
        unknown ref). Callers log their own context. Shared by
        :meth:`get_diff` and :meth:`get_compare_diff`.
        """
        response = self._session.get(
            self._url(path),
            headers=self._headers(accept="application/vnd.github.v3.diff"),
            timeout=_TIMEOUT_SECONDS,
        )
        if response.status_code in swallow_statuses:
            return None
        self._raise_for_status(response, path)
        return response.text

    def get_diff(self, pr_number: int) -> Optional[str]:
        """GET the PR's unified diff (Accept: application/vnd.github.v3.diff).

        Returns None when GitHub answers 406 Not Acceptable — its response
        for PRs too large to serve as a diff (>20k lines / 300 files /
        1MB). Callers fall back to the changed-file summary in that case.
        """
        diff = self._get_diff_text(f"/pulls/{pr_number}", (406,))
        if diff is None:
            logger.info(
                "[ChangeGating] diff too large for the GitHub diff media type "
                "(406): repo=%s pr=%s — falling back to file summary",
                self.repo_full_name, pr_number,
            )
        return diff

    def list_files(self, pr_number: int) -> List[Dict[str, Any]]:
        """GET all changed files for the PR (paginated)."""
        return self._get_paginated(f"/pulls/{pr_number}/files")

    def list_reviews(self, pr_number: int) -> List[Dict[str, Any]]:
        """GET all reviews on the PR in chronological order (paginated)."""
        return self._get_paginated(f"/pulls/{pr_number}/reviews")

    def list_review_comments(self, pr_number: int) -> List[Dict[str, Any]]:
        """GET all inline review comments on the PR (paginated).

        Each comment carries ``pull_request_review_id`` linking it to the
        review it belongs to.
        """
        return self._get_paginated(f"/pulls/{pr_number}/comments")

    def get_compare_diff(self, base_sha: str, head_sha: str) -> Optional[str]:
        """GET the unified diff of what ``head_sha`` adds on top of ``base_sha``.

        Uses GitHub's three-dot compare (``base...head``). Returns None on 404
        (a sha GitHub can't find) or 406 (range too large); callers fall back
        to the full PR diff. Pair with :meth:`get_compare` to first confirm the
        range is a clean linear advance (``status == "ahead"``) before trusting
        the diff as a true incremental delta.
        """
        diff = self._get_diff_text(f"/compare/{base_sha}...{head_sha}", (404, 406))
        if diff is None:
            logger.info(
                "[ChangeGating] compare diff unavailable (404/406): repo=%s "
                "%s..%s — falling back to full diff",
                self.repo_full_name, base_sha[:7], head_sha[:7],
            )
        return diff

    def get_compare(self, base_sha: str, head_sha: str) -> Optional[Dict[str, Any]]:
        """GET the compare JSON for ``base_sha...head_sha``.

        Carries ``status`` (``ahead`` / ``behind`` / ``diverged`` /
        ``identical``) and the changed-file list (``files``: same shape as
        :meth:`list_files`). ``status == "ahead"`` is the only case where head
        is a clean linear advance over base, i.e. a genuine incremental delta;
        the others mean a force-push / out-of-order / no-op push and the caller
        must fall back to a full-PR review. Returns None when the compare is
        unavailable (404/406).
        """
        path = f"/compare/{base_sha}...{head_sha}"
        response = self._session.get(
            self._url(path), headers=self._headers(), timeout=_TIMEOUT_SECONDS,
        )
        if response.status_code in (404, 406):
            return None
        self._raise_for_status(response, path)
        data = response.json()
        return data if isinstance(data, dict) else None

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def post_review(
        self,
        pr_number: int,
        *,
        commit_id: str,
        event: str,
        body: str,
        comments: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """POST a PR review (APPROVE or COMMENT) with optional inline comments.

        Each comment is ``{"path", "line", "side": "RIGHT", "body"}``.
        GitHub 422s when any inline comment falls outside the diff hunks;
        in that case we retry ONCE with no inline comments — the findings
        remain visible in the top-level review body table.
        """
        path = f"/pulls/{pr_number}/reviews"
        payload = {
            "commit_id": commit_id,
            "event": event,
            "body": body,
            "comments": comments,
        }
        response = self._session.post(
            self._url(path),
            headers=self._headers(),
            json=payload,
            timeout=_TIMEOUT_SECONDS,
        )
        if response.status_code == 422 and comments:
            excerpt = redact_token(response.text or "")[:300]
            logger.warning(
                "[ChangeGating] post_review got 422 with %d inline comments; "
                "retrying once without inline comments. status=%s response=%s",
                len(comments),
                response.status_code,
                excerpt,
            )
            payload["comments"] = []
            response = self._session.post(
                self._url(path),
                headers=self._headers(),
                json=payload,
                timeout=_TIMEOUT_SECONDS,
            )
        self._raise_for_status(response, path)
        return response.json()

    def dismiss_review(self, pr_number: int, review_id: int, message: str) -> Dict[str, Any]:
        """PUT a dismissal for a prior review.

        GitHub only allows dismissing reviews in APPROVED (or
        CHANGES_REQUESTED) state — COMMENT reviews cannot be dismissed;
        use :meth:`update_review_body` to supersede those instead.
        """
        path = f"/pulls/{pr_number}/reviews/{review_id}/dismissals"
        response = self._session.put(
            self._url(path),
            headers=self._headers(),
            json={"message": message},
            timeout=_TIMEOUT_SECONDS,
        )
        self._raise_for_status(response, path)
        return response.json()

    def update_review_body(self, pr_number: int, review_id: int, body: str) -> Dict[str, Any]:
        """PUT a replacement body onto an existing review."""
        path = f"/pulls/{pr_number}/reviews/{review_id}"
        response = self._session.put(
            self._url(path),
            headers=self._headers(),
            json={"body": body},
            timeout=_TIMEOUT_SECONDS,
        )
        self._raise_for_status(response, path)
        return response.json()

    def post_issue_comment(self, pr_number: int, body: str) -> Dict[str, Any]:
        """POST a PR conversation comment (a GitHub *issue* comment).

        Used for the transient "Aurora is reviewing…" progress indicator.
        Returns the created comment dict (carries ``id``).
        """
        path = f"/issues/{pr_number}/comments"
        response = self._session.post(
            self._url(path),
            headers=self._headers(),
            json={"body": body},
            timeout=_TIMEOUT_SECONDS,
        )
        self._raise_for_status(response, path)
        return response.json()

    def delete_issue_comment(self, comment_id: int) -> None:
        """DELETE a PR conversation comment by id (204, no body).

        Idempotent: a 404 (comment already gone — e.g. deleted by a human)
        is treated as success, not an error.
        """
        path = f"/issues/comments/{comment_id}"
        response = self._session.delete(
            self._url(path),
            headers=self._headers(),
            timeout=_TIMEOUT_SECONDS,
        )
        if response.status_code == 404:
            return
        self._raise_for_status(response, path)

    def supersede_review(
        self, pr_number: int, prior_review: Dict[str, Any], message: str
    ) -> None:
        """Mark a prior Aurora review as superseded.

        Encapsulates the GitHub-specific state quirk (design doc section
        11 keeps provider details out of the task):

        - APPROVED reviews are dismissable -> dismiss with ``message``.
        - COMMENTED reviews cannot be dismissed -> prepend a bold
          ``message`` note to the body instead. Idempotent: if the note
          is already there (a previous supersede whose follow-up post
          failed), the body is left untouched.
        - Any other state (e.g. DISMISSED) needs no supersede.
        """
        state = prior_review.get("state")
        review_id = prior_review.get("id")
        if state == "APPROVED":
            self.dismiss_review(pr_number, review_id, message)
        elif state == "COMMENTED":
            note = f"**{message}**"
            body = prior_review.get("body") or ""
            if body.startswith(note):
                return
            self.update_review_body(pr_number, review_id, f"{note}\n\n{body}")
