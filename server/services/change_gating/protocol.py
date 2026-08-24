"""Provider-neutral adapter contract for PR change gating.

``_run_investigation_core`` (``tasks/change_gating.py``) drives the whole
review lifecycle through this Protocol; everything provider-specific
(GitHub REST vs Bitbucket Cloud 2.0) lives behind it. Adding a provider
means writing one adapter + one thin Celery task wrapper — the core is
never rewritten (design doc ``bitbucket-incident-prevention.md``).

Normalization contract (what the core expects back)
---------------------------------------------------
- ``get_pull_request`` returns a GitHub-shaped PR dict:
  ``state`` is lowercase (``"open"``), ``draft`` is a bool,
  ``head.sha`` / ``base.ref`` / ``base.repo.default_branch`` are present,
  ``title`` / ``body`` / ``user.login`` / ``number`` are present.
  Adapters for providers with different shapes (Bitbucket ``OPEN``,
  ``source.commit.hash``) normalize inside the adapter.
- ``list_files`` returns GitHub ``list_files``-shaped dicts
  (``filename`` / ``status`` / ``additions`` / ``deletions`` and, when
  available, a unified ``patch``).
- Errors are raised as exceptions (a ``response`` attribute with
  ``status_code`` enables transient/permanent classification). Adapters
  over clients that return ``{"error": True}`` dicts MUST convert those
  to raised exceptions.
- Providers without a native compare API return ``None`` from
  ``get_compare`` / ``get_compare_diff`` — the core then falls back to a
  full-PR review (incremental review is an optimization, not a
  requirement).
- Reviews are GitHub-shaped dicts: ``id``, ``state`` (``APPROVED`` /
  ``COMMENTED`` / ``DISMISSED``), ``body``, ``user``. Providers without
  first-class reviews (Bitbucket) synthesize them from marker comments +
  approval state inside the adapter.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class PRAdapter(Protocol):
    """One PR-provider client scoped to a single repository."""

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_pull_request(self, pr_number: int) -> Dict[str, Any]:
        """Return the normalized PR dict (see module docstring)."""

    def get_diff(self, pr_number: int) -> Optional[str]:
        """Return the PR's unified diff, or None when unavailable."""

    def list_files(self, pr_number: int) -> List[Dict[str, Any]]:
        """Return the changed files as GitHub ``list_files``-shaped dicts."""

    def list_reviews(self, pr_number: int) -> List[Dict[str, Any]]:
        """Return all reviews on the PR in chronological order."""

    def list_review_comments(self, pr_number: int) -> List[Dict[str, Any]]:
        """Return all inline review comments on the PR."""

    def get_compare(self, base_sha: str, head_sha: str) -> Optional[Dict[str, Any]]:
        """Return compare JSON (``status`` + ``files``), or None when
        unsupported/unavailable — the core then runs a full-PR review."""

    def get_compare_diff(self, base_sha: str, head_sha: str) -> Optional[str]:
        """Return the incremental unified diff, or None (full-PR fallback)."""

    # ------------------------------------------------------------------
    # Aurora-identity helpers
    # ------------------------------------------------------------------

    def find_aurora_reviews(self, reviews: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Filter ``reviews`` down to Aurora's own, in chronological order.

        Must pair the body marker with a provider-side authorship check —
        a marker alone (which a human can paste) never qualifies.
        """

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def post_review(
        self,
        pr_number: int,
        *,
        commit_id: str,
        body: str,
        comments: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Post the verdict as a COMMENT review — adapters must never
        approve a PR (an approval would count toward required-approval
        merge checks). ``comments`` are inline findings (may be ignored
        by providers without inline support)."""

    def dismiss_review(self, pr_number: int, review_id: Any, message: str) -> Any:
        """Retract a legacy APPROVE posted before Aurora stopped approving
        (GitHub: dismissal; Bitbucket: unapprove)."""

    def supersede_review(
        self, pr_number: int, prior_review: Dict[str, Any], message: str
    ) -> None:
        """Mark a prior Aurora review as superseded by a newer one."""

    def post_issue_comment(self, pr_number: int, body: str) -> Dict[str, Any]:
        """Post a PR conversation comment; the returned dict carries ``id``."""

    def delete_issue_comment(self, comment_id: Any) -> None:
        """Delete a PR conversation comment (idempotent on already-gone)."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Release any pooled connections."""
