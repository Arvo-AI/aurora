"""Bitbucket Cloud PR adapter for change gating (Incident Prevention).

Implements ``services.change_gating.protocol.PRAdapter`` over the
Bitbucket Cloud 2.0 API so ``_run_investigation_core`` can drive the same
review flow it uses for GitHub. Provider quirks handled here (design doc
``bitbucket-incident-prevention.md``):

- **Normalization**: Bitbucket ``OPEN`` → ``open``; ``source.commit.hash``
  → ``head.sha``; destination branch → ``base.ref``;
  ``connected_repos.default_branch`` → ``base.repo.default_branch``.
- **Errors**: ``BitbucketAPIClient`` returns ``{"error": True}`` dicts —
  this adapter RAISES (:class:`BitbucketAPIError`, which carries a
  ``response.status_code``) so the core's retry/permanent classification
  works unchanged.
- **No compare API**: ``get_compare``/``get_compare_diff`` return ``None``
  so the core always falls back to a full-PR review (incremental reviews
  are a follow-up).
- **No first-class reviews**: prior Aurora reviews are synthesized from
  top-level marker comments.
- **Post review** = marker comment only (no inline comments in the POC).
  Aurora never approves PRs — there is no approve call in the posting
  path.
- **Dismiss / supersede** = prepend a note on the prior marker comment
  ONLY. The adapter never touches approvals in either direction:
  Bitbucket approval is account-level, so an unapprove could strip a
  manual approval the connected user made themselves. Legacy Aurora
  approvals (posted before Aurora stopped approving) must be removed
  by hand.
- **"Is this Aurora?"** = marker AND author UUID in
  ``{bot_uuid, token_owner_uuid}`` — a marker alone (which a human can
  paste) never qualifies.

Posting identity: the org's connected Bitbucket token by default. If a
deployer provisioned a dedicated bot account in the secrets backend
(system secret ``bitbucket-bot/credentials``, JSON ``{"email": ...,
"api_token": ...}``), reviews post as that account instead. Bot
credentials never reach agent tools and are never logged.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from connectors.bitbucket_connector.api_client import BitbucketAPIClient
from services.change_gating.markers import has_aurora_marker

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hidden-marker translation.
#
# The canonical Aurora markers (markers.py) are HTML comments — invisible on
# GitHub, but Bitbucket Cloud ESCAPES raw HTML in markdown, so "<!-- ... -->"
# renders as literal text in the PR comment. The Bitbucket-invisible
# equivalent is a markdown link-reference definition: a line of the form
#   [//]: # (payload)
# is consumed by the renderer and never displayed. This pair translates at
# the adapter's post/read boundary so markers.py stays provider-neutral:
# every outgoing body converts HTML-comment markers to the hidden form, and
# every body read back converts them to HTML-comment form before
# has_aurora_marker/decode_marker run.
#
# Both regexes are line-anchored AND restricted to the exact payload
# alphabet Aurora markers use (marker names, base64/hex, ":", spaces) —
# a genuinely inverse pair. The pair deliberately does NOT try to handle
# arbitrary HTML comments (multiline, parens, etc.): markers.py always
# emits its marker alone on one line with this alphabet, and anything
# else in a body is content that must be left untouched.
# ---------------------------------------------------------------------------

_MARKER_PAYLOAD = r"[A-Za-z0-9+/=:. _-]+"
_HTML_COMMENT_MARKER_RE = re.compile(
    rf"^<!-- ({_MARKER_PAYLOAD}) -->[ \t]*$", re.MULTILINE
)
_BB_HIDDEN_MARKER_RE = re.compile(
    rf"^\[//\]: # \(({_MARKER_PAYLOAD})\)[ \t]*$", re.MULTILINE
)


def hide_markers_for_bitbucket(body: str) -> str:
    """Rewrite HTML-comment markers into Bitbucket-invisible link labels."""
    return _HTML_COMMENT_MARKER_RE.sub(lambda m: f"[//]: # ({m.group(1)})", body or "")


def restore_markers_from_bitbucket(body: str) -> str:
    """Inverse of :func:`hide_markers_for_bitbucket` for read-back bodies.

    Bodies that already carry raw HTML-comment markers (posted before this
    translation existed) pass through unchanged, so both generations of
    prior reviews stay recognizable.
    """
    return _BB_HIDDEN_MARKER_RE.sub(lambda m: f"<!-- {m.group(1)} -->", body or "")

# System-secret logical name for the optional dedicated posting bot
# (same pattern as ``github-app/webhook-secret``):
#   vault kv put aurora/system/bitbucket-bot/credentials \
#     value='{"email": "bot@example.com", "api_token": "..."}'
_BOT_CREDENTIALS_LOGICAL_NAME = "bitbucket-bot/credentials"


class BitbucketAPIError(Exception):
    """Raised when the Bitbucket API returns an error dict.

    Carries a ``response`` attribute shaped like ``requests.Response``
    (``status_code`` / ``headers`` / ``text``) so
    ``tasks.change_gating._classify_provider_exc`` can classify it as
    transient (5xx / 429) or permanent (other 4xx) without knowing about
    Bitbucket.
    """

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)

        class _Resp:  # minimal requests.Response stand-in
            pass

        self.response = _Resp()
        self.response.status_code = status_code
        self.response.headers = {}
        self.response.text = message


def _checked(result: Any, phase: str) -> Any:
    """Convert a ``{"error": True}`` client result into a raised exception."""
    if isinstance(result, dict) and result.get("error") is True:
        status = result.get("status")
        message = str(result.get("message") or "Bitbucket API error")
        raise BitbucketAPIError(
            f"Bitbucket {phase} failed (status={status}): {message[:300]}",
            status_code=status if isinstance(status, int) else None,
        )
    return result


# ---------------------------------------------------------------------------
# Unified-diff file listing (Bitbucket has no PR file-list endpoint that
# carries per-file patches, so we derive GitHub ``list_files``-shaped dicts
# from the PR's unified diff).
# ---------------------------------------------------------------------------

_DIFF_GIT_PREFIX = "diff --git a/"


def _new_file_entry(diff_git_line: str) -> Dict[str, Any]:
    """Start a file dict from a ``diff --git a/<old> b/<new>`` line.

    The b/ path is taken via ``rsplit(" b/", 1)`` (no regex — the naive
    ``(.*?) b/(.*)`` pattern backtracks super-linearly on crafted names).
    """
    remainder = diff_git_line[len(_DIFF_GIT_PREFIX):]
    parts = remainder.rsplit(" b/", 1)
    filename = parts[1] if len(parts) == 2 else remainder
    return {"filename": filename, "status": "modified", "additions": 0, "deletions": 0}


def _apply_header_line(entry: Dict[str, Any], line: str) -> None:
    """Interpret a file-header-zone line (between ``diff --git`` and ``@@``)."""
    if line.startswith("--- /dev/null"):
        entry["status"] = "added"
    elif line.startswith("+++ /dev/null"):
        # A deleted file's only real path is the LEFT side.
        entry["status"] = "removed"
    elif line.startswith("+++ b/"):
        entry["filename"] = line[6:].split("\t")[0].strip()


def _apply_hunk_line(entry: Dict[str, Any], line: str) -> None:
    """Count adds/dels inside a hunk (``+++``/``---`` shapes never appear
    mid-hunk because the caller tracks the hunk zone)."""
    if line.startswith("+") and not line.startswith("+++"):
        entry["additions"] += 1
    elif line.startswith("-") and not line.startswith("---"):
        entry["deletions"] += 1


def _finish_file_entry(entry: Dict[str, Any], lines: List[str]) -> Dict[str, Any]:
    """Attach the patch slice: everything from the first @@ onward
    (GitHub's shape); header lines (index/---/+++) are not part of it."""
    hunk_start = next(
        (i for i, line in enumerate(lines) if line.startswith("@@")), None
    )
    if hunk_start is not None:
        entry["patch"] = "\n".join(lines[hunk_start:])
    return entry


def parse_files_from_diff(diff_text: Optional[str]) -> List[Dict[str, Any]]:
    """Split a unified diff into GitHub ``list_files``-shaped file dicts.

    Each dict carries ``filename`` (RIGHT-side path), ``status``
    (``added`` / ``removed`` / ``modified``), ``additions`` /
    ``deletions`` counts, and the file's own ``patch`` slice — exactly
    what ``build_per_file_diff`` and ``parse_diff_hunks`` consume.
    """
    files: List[Dict[str, Any]] = []
    if not diff_text:
        return files

    current: Optional[Dict[str, Any]] = None
    current_lines: List[str] = []
    in_hunk = False

    for line in diff_text.splitlines():
        if line.startswith(_DIFF_GIT_PREFIX):
            if current is not None:
                files.append(_finish_file_entry(current, current_lines))
            current = _new_file_entry(line)
            current_lines = []
            in_hunk = False
            continue
        if current is None:
            continue
        current_lines.append(line)
        if line.startswith("@@"):
            in_hunk = True
        elif in_hunk:
            _apply_hunk_line(current, line)
        else:
            _apply_header_line(current, line)

    if current is not None:
        files.append(_finish_file_entry(current, current_lines))
    return files


# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------


def _load_bot_client() -> Optional[BitbucketAPIClient]:
    """Build a client from the optional system-secret bot credentials.

    Returns None when no bot is provisioned (the default) or on any
    backend error — the caller then posts as the connected user.
    """
    try:
        from utils.secrets import get_secrets_backend

        backend = get_secrets_backend()
        if not backend.is_available():
            return None
        ref = backend.build_system_ref(_BOT_CREDENTIALS_LOGICAL_NAME)
        raw = backend.get_secret(ref)
        if not raw:
            return None
        creds = json.loads(raw)
        email = creds.get("email")
        api_token = creds.get("api_token") or creds.get("token")
        if not (email and api_token):
            logger.warning(
                "[ChangeGating:BB] bot credentials secret is present but missing "
                "email/api_token — falling back to the connected user"
            )
            return None
        logger.info("[ChangeGating:BB] using dedicated bot account for posting")
        return BitbucketAPIClient(api_token, auth_type="api_token", email=email)
    except Exception as exc:
        # Missing secret is the normal case; never log secret material.
        logger.debug(
            "[ChangeGating:BB] no bot credentials available (%s) — posting as "
            "the connected user", type(exc).__name__,
        )
        return None


def _lookup_default_branch(user_id: str, repo_full_name: str) -> Optional[str]:
    """Read ``connected_repos.default_branch`` for the enrolled repo."""
    from utils.auth.stateless_auth import set_rls_context
    from utils.db.connection_pool import db_pool

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                org_id = set_rls_context(cur, conn, user_id, log_prefix="[ChangeGating:BB]")
                if not org_id:
                    return None
                # Explicit org predicate as defense in depth alongside RLS
                # (same belt-and-suspenders as stateless_auth's user_tokens
                # reads). MAX() over the org's duplicate rows (UNIQUE is per
                # user): a row whose default_branch is NULL must not shadow
                # a sibling that carries the real value.
                cur.execute(
                    """SELECT MAX(default_branch) FROM connected_repos
                        WHERE provider = 'bitbucket' AND repo_full_name = %s
                          AND org_id = %s""",
                    (repo_full_name, org_id),
                )
                row = cur.fetchone()
                return row[0] if row and row[0] else None
    except Exception as exc:
        logger.warning(
            "[ChangeGating:BB] default_branch lookup failed for %s: %s",
            repo_full_name, type(exc).__name__,
        )
        return None


def build_bitbucket_adapter(user_id: str, repo_full_name: str) -> Optional["BitbucketPRAdapter"]:
    """Construct the adapter for one investigation, or None without creds.

    Reads always use the org's connected Bitbucket token (mirrors what the
    agent's own tools can see); posting prefers the dedicated bot account
    when one is provisioned.
    """
    from chat.backend.agent.tools.bitbucket.utils import get_bb_client_for_user

    read_client = get_bb_client_for_user(user_id)
    if read_client is None:
        return None
    post_client = _load_bot_client() or read_client
    default_branch = _lookup_default_branch(user_id, repo_full_name)
    return BitbucketPRAdapter(
        repo_full_name,
        read_client=read_client,
        post_client=post_client,
        default_branch=default_branch,
    )


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class BitbucketPRAdapter:
    """PRAdapter over Bitbucket Cloud, scoped to one ``workspace/repo_slug``."""

    def __init__(
        self,
        repo_full_name: str,
        *,
        read_client: BitbucketAPIClient,
        post_client: BitbucketAPIClient,
        default_branch: Optional[str] = None,
    ):
        self.repo_full_name = repo_full_name
        parts = repo_full_name.split("/", 1)
        self.workspace = parts[0]
        self.repo_slug = parts[1] if len(parts) > 1 else ""
        self._read = read_client
        self._post = post_client
        self._default_branch = default_branch
        # Author UUIDs allowed to count as "Aurora" — resolved lazily, once.
        self._aurora_uuids: Optional[set] = None
        # post_issue_comment(pr) → delete_issue_comment(comment_id) mapping
        # (Bitbucket's comment-delete endpoint is PR-scoped, GitHub's isn't).
        self._comment_pr: Dict[Any, int] = {}

    def close(self) -> None:
        """No pooled session to release (clients use module-level requests)."""

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    def _allowed_uuids(self) -> set:
        """UUIDs whose comments count as Aurora's own.

        The posting identity (bot or connected user) plus the read identity
        (connected user) — reviews posted before a bot was configured must
        still be recognized after one is added.
        """
        if self._aurora_uuids is not None:
            return self._aurora_uuids
        uuids = set()
        for client in {id(self._post): self._post, id(self._read): self._read}.values():
            try:
                me = client.get_current_user()
                if isinstance(me, dict) and not me.get("error"):
                    uuid = me.get("uuid")
                    if uuid:
                        uuids.add(uuid)
            except Exception as exc:
                logger.warning(
                    "[ChangeGating:BB] identity lookup failed: %s", type(exc).__name__
                )
        # Cache only a non-empty result: caching {} after a transient /user
        # failure would make find_aurora_reviews return [] for the whole
        # investigation, dropping prior-review context and the supersede
        # step (→ duplicate reviews on the PR).
        if uuids:
            self._aurora_uuids = uuids
        return uuids

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_pull_request(self, pr_number: int) -> Dict[str, Any]:
        """GET + normalize the PR into the GitHub shape the core expects."""
        raw = _checked(
            self._read.get_pull_request(self.workspace, self.repo_slug, pr_number),
            "get_pull_request",
        )
        source = raw.get("source") or {}
        destination = raw.get("destination") or {}
        author = raw.get("author") or {}
        state = str(raw.get("state") or "").lower()
        # GitHub reports merged/declined PRs as "closed"; the core only
        # distinguishes open vs not-open.
        if state in ("merged", "declined", "superseded"):
            state = "closed"
        default_branch = self._resolve_default_branch()
        return {
            "number": raw.get("id"),
            "state": state,
            "draft": bool(raw.get("draft")),
            "title": raw.get("title"),
            "body": raw.get("description"),
            "user": {"login": author.get("nickname") or author.get("display_name")},
            "head": {
                "sha": ((source.get("commit") or {}).get("hash")),
                "ref": ((source.get("branch") or {}).get("name")),
            },
            "base": {
                "ref": ((destination.get("branch") or {}).get("name")),
                "sha": ((destination.get("commit") or {}).get("hash")),
                "repo": {"default_branch": default_branch},
            },
        }

    def _resolve_default_branch(self) -> Optional[str]:
        """Default branch: connected_repos value, else the repo's mainbranch
        via the API (one extra call, cached for the adapter's lifetime)."""
        if not self._default_branch:
            repo_info = self._read.get_repository(self.workspace, self.repo_slug)
            if isinstance(repo_info, dict) and not repo_info.get("error"):
                self._default_branch = (repo_info.get("mainbranch") or {}).get("name")
        return self._default_branch

    def get_diff(self, pr_number: int) -> Optional[str]:
        """GET the PR's unified diff."""
        return _checked(
            self._read.get_pr_diff(self.workspace, self.repo_slug, pr_number),
            "get_diff",
        )

    def list_files(self, pr_number: int) -> List[Dict[str, Any]]:
        """Derive the changed-file list from the PR's unified diff."""
        diff = self.get_diff(pr_number)
        return parse_files_from_diff(diff)

    def list_reviews(self, pr_number: int) -> List[Dict[str, Any]]:
        """Synthesize GitHub-shaped reviews from top-level PR comments.

        Bitbucket has no PR Reviews API. Every live top-level (non-inline,
        non-reply) comment becomes a candidate review dict; markers and
        authorship are checked in :meth:`find_aurora_reviews`, mirroring
        the GitHub structure where list + filter are separate steps.
        """
        comments = _checked(
            self._read.list_pr_comments(self.workspace, self.repo_slug, pr_number),
            "list_reviews",
        )
        reviews: List[Dict[str, Any]] = []
        for comment in comments or []:
            if not isinstance(comment, dict):
                continue
            if comment.get("deleted"):
                continue
            if comment.get("inline") or comment.get("parent"):
                continue
            body = ((comment.get("content") or {}).get("raw")) or ""
            reviews.append(
                {
                    "id": comment.get("id"),
                    "state": "COMMENTED",  # Bitbucket has no review states; never APPROVED
                    # Read-back translation: hidden Bitbucket markers become
                    # HTML-comment markers so decode_marker/has_aurora_marker
                    # (provider-neutral) recognize them.
                    "body": restore_markers_from_bitbucket(body),
                    "user": comment.get("user") or {},
                    "_pr_number": pr_number,
                }
            )
        return reviews

    def find_aurora_reviews(self, reviews: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Filter to Aurora's own reviews: marker AND allowlisted author UUID.

        The marker alone is not enough — any workspace member could paste
        one into a comment to hijack the prior-review context or redirect
        the supersede step.
        """
        allowed = self._allowed_uuids()
        out: List[Dict[str, Any]] = []
        for review in reviews or []:
            if not isinstance(review, dict):
                continue
            if not has_aurora_marker(review.get("body")):
                continue
            user = review.get("user") or {}
            if not (isinstance(user, dict) and user.get("uuid") in allowed):
                continue
            out.append(review)
        return out

    def list_review_comments(self, pr_number: int) -> List[Dict[str, Any]]:
        """No inline comments in the POC — nothing to reconcile against.

        The parameter is required by the ``PRAdapter`` Protocol signature.
        """
        del pr_number
        return []

    def get_compare(self, base_sha: str, head_sha: str) -> Optional[Dict[str, Any]]:
        """No incremental reviews in the POC → full-PR fallback.

        Parameters are required by the ``PRAdapter`` Protocol signature.
        """
        del base_sha, head_sha
        return None

    def get_compare_diff(self, base_sha: str, head_sha: str) -> Optional[str]:
        """No incremental reviews in the POC → full-PR fallback.

        Parameters are required by the ``PRAdapter`` Protocol signature.
        """
        del base_sha, head_sha
        return None

    # ------------------------------------------------------------------
    # Writes (always via the posting client)
    # ------------------------------------------------------------------

    def post_review(
        self,
        pr_number: int,
        *,
        commit_id: str,
        body: str,
        comments: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Post the review as a marker comment — never an approval.

        ``commit_id`` and inline ``comments`` are required by the
        ``PRAdapter`` Protocol but ignored in the POC — Bitbucket comments
        are not commit-pinned and all findings live in the body table.
        """
        del commit_id, comments
        comment = _checked(
            self._post.add_pr_comment(
                self.workspace, self.repo_slug, pr_number,
                hide_markers_for_bitbucket(body),
            ),
            "post_review",
        )
        return {"id": comment.get("id")}

    def dismiss_review(self, pr_number: int, review_id: Any, message: str) -> Any:
        """Mark a stale review: best-effort note on the comment.

        Approvals are never touched — Bitbucket approval is account-level,
        so an unapprove could strip a manual approval the connected user
        made themselves.

        Currently unreachable in production (kept for ``PRAdapter``
        conformance): the core's only call site is the incremental dismiss
        loop, gated on ``state == "APPROVED"`` — this adapter has no
        incremental path and never surfaces APPROVED.
        """
        self._prepend_note(pr_number, review_id, message, body=None)
        return {"id": review_id}

    def supersede_review(
        self, pr_number: int, prior_review: Dict[str, Any], message: str
    ) -> None:
        """Mark a prior Aurora review superseded: prepend a note.

        Approvals are never touched (see :meth:`dismiss_review`).
        Idempotent: if the note is already there (a previous supersede whose
        follow-up failed), the body is left untouched.
        """
        self._prepend_note(
            pr_number, prior_review.get("id"), message, body=prior_review.get("body")
        )

    def _prepend_note(
        self, pr_number: int, comment_id: Any, message: str, body: Optional[str]
    ) -> None:
        """Prepend a bold note to a prior marker comment (best-effort)."""
        if comment_id is None:
            return
        note = f"**{message}**"
        try:
            if body is None:
                # dismiss path has no body in hand — find it.
                comments = self._read.list_pr_comments(
                    self.workspace, self.repo_slug, pr_number
                )
                for candidate in comments if isinstance(comments, list) else []:
                    if isinstance(candidate, dict) and candidate.get("id") == comment_id:
                        body = ((candidate.get("content") or {}).get("raw")) or ""
                        break
            if body is None:
                return
            if body.startswith(note):
                return
            # body may carry an HTML-comment marker (from list_reviews'
            # read-back translation) — re-hide it for the write.
            _checked(
                self._post.update_pr_comment(
                    self.workspace, self.repo_slug, pr_number, comment_id,
                    hide_markers_for_bitbucket(f"{note}\n\n{body}"),
                ),
                "prepend_note",
            )
        except Exception as exc:
            # The note is cosmetic; the new review carries the signal.
            logger.warning(
                "[ChangeGating:BB] supersede note failed for comment %s: %s",
                comment_id, type(exc).__name__,
            )

    def post_issue_comment(self, pr_number: int, body: str) -> Dict[str, Any]:
        """POST a PR conversation comment (progress indicator)."""
        comment = _checked(
            self._post.add_pr_comment(
                self.workspace, self.repo_slug, pr_number,
                hide_markers_for_bitbucket(body),
            ),
            "post_issue_comment",
        )
        comment_id = comment.get("id")
        if comment_id is not None:
            self._comment_pr[comment_id] = pr_number
        return {"id": comment_id}

    def delete_issue_comment(self, comment_id: Any) -> None:
        """DELETE a PR conversation comment. Idempotent on already-gone."""
        pr_number = self._comment_pr.pop(comment_id, None)
        if pr_number is None:
            logger.warning(
                "[ChangeGating:BB] no PR recorded for comment %s — skipping delete",
                comment_id,
            )
            return
        result = self._post.delete_pr_comment(
            self.workspace, self.repo_slug, pr_number, comment_id
        )
        if isinstance(result, dict) and result.get("error") and result.get("status") != 404:
            raise BitbucketAPIError(
                f"Bitbucket comment delete failed (status={result.get('status')})",
                status_code=result.get("status") if isinstance(result.get("status"), int) else None,
            )
