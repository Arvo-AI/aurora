"""GitHub adapter for the change-intercept pipeline.

Wraps the existing Aurora GitHub App infrastructure rather than
duplicating it:

    - ``utils.auth.github_webhook.verify_webhook_signature`` handles
      HMAC-SHA256 over ``X-Hub-Signature-256``.
    - ``utils.auth.github_app_token.get_installation_token`` mints +
      caches per-installation tokens with the existing thundering-herd
      protections.
    - ``connectors.github_connector.vault_keys.get_app_webhook_secret``
      sources the webhook secret from Vault with env fallback.

What this module owns:

    - Translating ``pull_request`` / ``issue_comment`` /
      ``pull_request_review_comment`` payloads into the vendor-neutral
      :class:`NormalizedChangeEvent` shape.
    - The one-shot REST round-trip to fetch the unified diff +
      changed-files list + commit messages (and comments for followups)
      at webhook time so the investigator never needs to call back to
      GitHub.
    - Classifying comment events as replies to Aurora (threaded reply
      to a Bot comment, ``@<slug>`` mention, or an explicit
      ``re-review`` / ``recheck`` command).

Part 1a phase boundaries:

    - Part 1 (this commit): ``verify_signature``, ``parse``,
      ``fetch_snapshot``, ``is_reply_to_us`` are implemented; the
      Celery dispatcher persists every event to ``change_events`` but
      does not run any investigation.
    - Part 3: ``post_verdict`` and ``dismiss_prior`` get wired to
      ``POST /pulls/{n}/reviews`` and
      ``PUT /pulls/{n}/reviews/{id}/dismissals`` respectively.

Security invariants enforced here:

    - Tokens are never logged. ``get_installation_token`` already
      guarantees this, but every HTTP call additionally redacts any
      ``ghs_...`` substring from error text via
      ``utils.auth.log_redact.redact_token``.
    - Outbound calls always pass an explicit ``Authorization`` header
      built from a per-invocation token; we never mutate process env.
    - The webhook payload ``sender`` is checked against the App's own
      bot login before treating a comment as a reply to Aurora —
      prevents feedback loops where Aurora's own comments would
      re-trigger an investigation.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import requests

from connectors.github_connector.vault_keys import (
    GitHubAppConfigError,
    get_app_webhook_secret,
)
from utils.auth.github_app_token import (
    GitHubAppInstallationNotFound,
    GitHubAppInstallationSuspended,
    GitHubAppTokenError,
    get_installation_token,
)
from utils.auth.github_webhook import (
    SIGNATURE_HEADER,
    GitHubWebhookError,
    verify_webhook_signature,
)
from utils.auth.log_redact import redact_token

from .base import (
    RE_REVIEW_COMMANDS,
    ChangeAdapter,
    ChangeSnapshot,
    NormalizedChangeEvent,
    PostedVerdict,
    ReplyMatch,
)

logger = logging.getLogger(__name__)


# ─── Constants ───────────────────────────────────────────────────────


# Aligned with the existing ``_GITHUB_TIMEOUT_SECONDS`` in
# ``github_app_token.py`` for stack-wide consistency.
_GITHUB_TIMEOUT_SECONDS = 20

_API_BASE = "https://api.github.com"
_API_VERSION_HEADER = "2022-11-28"

# ``pull_request`` actions that flow into the investigation pipeline.
# Per the resolved open question (#2), ``synchronize`` deliberately does
# NOT trigger a new investigation — the dispatcher persists the event
# row for audit but skips the LLM call. The set below is the actions
# that produce a ``NormalizedChangeEvent`` of ``kind='code_change'``;
# the dispatcher's investigation-trigger decision lives on top.
_PR_ACCEPTED_ACTIONS: frozenset[str] = frozenset(
    {"opened", "reopened", "ready_for_review", "synchronize"}
)

# Actions we explicitly ignore (no review needed). ``closed`` /
# ``converted_to_draft`` / etc. We return ``None`` from ``parse`` so
# the dispatcher acknowledges the webhook without persisting.
_PR_IGNORED_ACTIONS: frozenset[str] = frozenset(
    {
        "closed",
        "converted_to_draft",
        "edited",  # title/body edits don't change risk
        "assigned",
        "unassigned",
        "review_requested",
        "review_request_removed",
        "labeled",
        "unlabeled",
        "auto_merge_enabled",
        "auto_merge_disabled",
        "locked",
        "unlocked",
        "milestoned",
        "demilestoned",
    }
)

# Branch names that imply a production target. Cheap heuristic used to
# populate ``target_env`` for analytics. NOT used to gate review per
# the resolved open question — every PR is reviewed regardless of env.
_PROD_BRANCH_RE = re.compile(
    r"^(main|master|prod|production)(/.*)?$|^(release|prod)[-/]",
    re.IGNORECASE,
)


def _resolve_app_slug() -> str:
    """Read the App slug from ``NEXT_PUBLIC_GITHUB_APP_SLUG``.

    Returns lowercase. Empty string if unset (the @-mention check then
    degrades to "no mentions match", which is safer than a false-match
    on a sender named e.g. ``-bot``).
    """
    return (os.getenv("NEXT_PUBLIC_GITHUB_APP_SLUG") or "").strip().lower()


def _bot_login_for(slug: str) -> str:
    """Return the GitHub login a Bot user with this slug would have.

    GitHub adds the ``[bot]`` suffix to every App user's login. The
    self-filter compares ``sender.login`` against this string.
    """
    return f"{slug}[bot]" if slug else ""


def _safe_get(payload: dict[str, Any], *keys: Any) -> Any:
    """Walk a nested dict / list; return ``None`` on any missing step.

    Mirrors the helper in ``tasks.github_webhook_tasks`` so the adapter
    behaves identically to the existing webhook code when fields are
    missing.
    """
    cur: Any = payload
    for key in keys:
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list) and isinstance(key, int):
            try:
                cur = cur[key]
            except IndexError:
                return None
        else:
            return None
    return cur


def _infer_target_env(ref: str | None, base_ref: str | None) -> str | None:
    """Return ``"prod"`` / ``"non-prod"`` / ``None``.

    Cheap heuristic on the BASE branch name (the merge target — the
    head branch is the engineer's feature ref). ``None`` when neither
    branch is set (e.g. for comment events).
    """
    if not base_ref and not ref:
        return None
    candidate = base_ref or ref or ""
    return "prod" if _PROD_BRANCH_RE.match(candidate) else "non-prod"


def _redact(text: str) -> str:
    """Local alias for ``redact_token`` to keep call sites short."""
    return redact_token(text) if text else text


# ─── REST helpers ────────────────────────────────────────────────────


class GitHubFetchError(Exception):
    """Raised when an outbound GitHub REST call fails or returns non-2xx.

    The dispatcher converts this to a Celery retry — the snapshot is
    required before we can persist the ``change_events`` row, so a
    transient GitHub failure should not silently drop the webhook.
    """


def _auth_headers(installation_id: int, accept: str) -> dict[str, str]:
    """Build the auth + accept headers for one outbound REST call.

    Each call gets its own header dict — no shared state, no env
    mutation. The token is minted via the shared cache so consecutive
    calls within a single ``fetch_snapshot`` round-trip reuse the
    same token without hitting the install-token endpoint.
    """
    token = get_installation_token(installation_id)
    return {
        "Authorization": f"Bearer {token}",
        "Accept": accept,
        "X-GitHub-Api-Version": _API_VERSION_HEADER,
    }


def _get(
    url: str,
    installation_id: int,
    *,
    accept: str = "application/vnd.github+json",
    params: dict[str, Any] | None = None,
) -> requests.Response:
    """Issue a GET with the App's installation token.

    Raises :class:`GitHubFetchError` on network failure or non-2xx.
    The token is never logged; the error message redacts any
    token-shaped substring from the response body before truncating.
    """
    try:
        headers = _auth_headers(installation_id, accept)
    except (GitHubAppInstallationNotFound, GitHubAppInstallationSuspended):
        # Surface these typed exceptions so the dispatcher can mark the
        # installation row appropriately. They are NOT wrapped in
        # ``GitHubFetchError`` because their semantics (mark deleted /
        # mark suspended) are different from a transient fetch failure.
        raise
    except GitHubAppTokenError as exc:
        raise GitHubFetchError(
            f"Failed to obtain installation token for installation_id={installation_id}: "
            f"{type(exc).__name__}: {_redact(str(exc))}"
        ) from exc

    try:
        response = requests.get(
            url, headers=headers, params=params, timeout=_GITHUB_TIMEOUT_SECONDS
        )
    except requests.RequestException as exc:
        raise GitHubFetchError(
            f"GitHub GET failed for url={url}: "
            f"{type(exc).__name__}: {_redact(str(exc))}"
        ) from exc

    if response.status_code >= 300:
        raise GitHubFetchError(
            f"GitHub GET returned non-2xx for url={url} "
            f"(status={response.status_code}): {_redact(response.text or '')[:200]}"
        )
    return response


def _paginated_get(
    url: str,
    installation_id: int,
    *,
    accept: str = "application/vnd.github+json",
    params: dict[str, Any] | None = None,
    max_pages: int = 10,
) -> list[Any]:
    """Walk Link-header pagination, accumulating array items.

    Caps at ``max_pages * per_page`` items to avoid runaway responses
    on pathological PRs (e.g. a 10,000-file refactor — we just take
    the first 1,000 files). The validator anchors findings against
    whatever made it into the snapshot; oversize PRs degrade
    gracefully rather than blowing up the worker.
    """
    items: list[Any] = []
    current_params = dict(params or {})
    current_params.setdefault("per_page", 100)
    current_url: str | None = url
    pages = 0

    while current_url and pages < max_pages:
        response = _get(
            current_url, installation_id, accept=accept, params=current_params
        )
        try:
            page = response.json()
        except ValueError as exc:
            raise GitHubFetchError(
                f"GitHub paginated GET returned non-JSON body for url={current_url}: {exc}"
            ) from exc

        if not isinstance(page, list):
            # First page should be an array; bail if not.
            raise GitHubFetchError(
                f"GitHub paginated GET expected JSON array; got {type(page).__name__}"
            )

        items.extend(page)
        pages += 1

        # ``Link: <...>; rel="next"`` is GitHub's pagination marker.
        link_header = response.headers.get("Link", "")
        next_url = _parse_next_link(link_header)
        current_url = next_url
        # After the first page, params are baked into ``next_url``.
        current_params = {}

    return items


# Compiled in ``_parse_next_link`` for clarity over performance —
# pagination runs once per snapshot, not in a hot loop.
def _parse_next_link(link_header: str) -> str | None:
    """Return the ``rel="next"`` URL from a GitHub Link header.

    Returns ``None`` when the header is missing or there is no next
    page. RFC 5988 link-header format, but GitHub uses a fixed
    ``rel="next"`` shape so we parse with a tight regex rather than a
    full RFC parser.
    """
    if not link_header:
        return None
    for chunk in link_header.split(","):
        chunk = chunk.strip()
        m = re.match(r'<([^>]+)>;\s*rel="next"', chunk)
        if m:
            return m.group(1)
    return None


# ─── The adapter ─────────────────────────────────────────────────────


class GitHubChangeAdapter:
    """:class:`ChangeAdapter` implementation for the Aurora GitHub App.

    Stateless — every method takes its inputs as arguments. The
    instance is cached by the registry only so multi-call traffic
    avoids re-running ``__init__`` (which today is a no-op but may
    pre-warm config in the future).
    """

    vendor: str = "github"

    # ─── Signature verification ──────────────────────────────────────

    def verify_signature(
        self,
        raw_body: bytes,
        headers: dict[str, str],
    ) -> bool:
        """Delegate to the existing ``verify_webhook_signature`` helper.

        The HTTP endpoint at ``routes.github.github_webhook`` already
        validates signatures before the dispatcher fires, so this
        method exists mostly for future use (a second ingest endpoint,
        or unit tests). For the same reason it does NOT raise on a
        malformed header — it returns ``False`` so the caller can
        decide how to respond.
        """
        signature = headers.get(SIGNATURE_HEADER) or headers.get(
            SIGNATURE_HEADER.lower()
        )
        if not signature:
            return False
        try:
            secret = get_app_webhook_secret()
        except GitHubAppConfigError as exc:
            logger.error(
                "github_adapter_event=verify_signature_failed reason=secret_unavailable "
                "error_class=%s",
                type(exc).__name__,
            )
            return False
        try:
            return verify_webhook_signature(raw_body, signature, secret)
        except GitHubWebhookError:
            return False

    # ─── Parsing ─────────────────────────────────────────────────────

    def parse(
        self,
        event_type: str,
        payload: dict[str, Any],
        org_id: str,
    ) -> NormalizedChangeEvent | None:
        """Translate a webhook payload into a ``NormalizedChangeEvent``.

        - ``pull_request`` → ``code_change`` event when action is in
          :data:`_PR_ACCEPTED_ACTIONS`; ``None`` otherwise.
        - ``issue_comment`` / ``pull_request_review_comment`` →
          ``code_change_followup`` event when classified as a reply
          to Aurora; ``None`` otherwise.
        - Any other event type → ``None``.
        """
        action = payload.get("action") if isinstance(payload.get("action"), str) else None
        installation_id = _safe_get(payload, "installation", "id")
        if not isinstance(installation_id, int):
            installation_id = None

        if event_type == "pull_request":
            return self._parse_pull_request(payload, action, installation_id, org_id)
        if event_type in ("issue_comment", "pull_request_review_comment"):
            return self._parse_comment(
                event_type, payload, action, installation_id, org_id
            )
        return None

    def _parse_pull_request(
        self,
        payload: dict[str, Any],
        action: str | None,
        installation_id: int | None,
        org_id: str,
    ) -> NormalizedChangeEvent | None:
        if action not in _PR_ACCEPTED_ACTIONS:
            return None

        pr_number = _safe_get(payload, "pull_request", "number")
        repo_full_name = _safe_get(payload, "repository", "full_name")
        if not isinstance(pr_number, int) or not isinstance(repo_full_name, str):
            logger.warning(
                "github_adapter_event=parse_pull_request status=skipped "
                "reason=missing_pr_or_repo action=%s",
                action,
            )
            return None

        # ``ready_for_review`` carries ``draft=False`` on the new state;
        # ``opened`` on a draft carries ``draft=True``. Per the
        # re-investigation policy we don't review drafts on open — we
        # wait for ``ready_for_review`` to fire when the PR transitions.
        is_draft = bool(_safe_get(payload, "pull_request", "draft"))
        if action == "opened" and is_draft:
            return None

        ref = _safe_get(payload, "pull_request", "head", "ref")
        base_ref = _safe_get(payload, "pull_request", "base", "ref")
        commit_sha = _safe_get(payload, "pull_request", "head", "sha")
        actor = _safe_get(payload, "pull_request", "user", "login")

        external_id = str(pr_number)
        dedup_key = f"github:{repo_full_name}:{pr_number}"

        return NormalizedChangeEvent(
            vendor=self.vendor,
            kind="code_change",
            org_id=org_id,
            installation_id=installation_id,
            external_id=external_id,
            dedup_key=dedup_key,
            repo=repo_full_name,
            ref=ref if isinstance(ref, str) else None,
            base_ref=base_ref if isinstance(base_ref, str) else None,
            commit_sha=commit_sha if isinstance(commit_sha, str) else None,
            actor=actor if isinstance(actor, str) else None,
            target_env=_infer_target_env(
                ref if isinstance(ref, str) else None,
                base_ref if isinstance(base_ref, str) else None,
            ),
            action=action or "unknown",
            raw_payload=payload,
        )

    def _parse_comment(
        self,
        event_type: str,
        payload: dict[str, Any],
        action: str | None,
        installation_id: int | None,
        org_id: str,
    ) -> NormalizedChangeEvent | None:
        # Only ``created`` matters — edits / deletions on comments
        # are noise for our purposes.
        if action != "created":
            return None

        match = self._classify_reply(event_type, payload)
        if match is None:
            return None

        pr_number_str = match.parent_pr_external_id
        repo_full_name = match.repo
        actor = match.replier
        comment_body = match.comment_body
        comment_id = match.comment_id

        # Pull head SHA off the issue's PR sub-object if present (only
        # for ``issue_comment`` events). ``pull_request_review_comment``
        # carries the SHA on the comment block directly.
        if event_type == "pull_request_review_comment":
            commit_sha = _safe_get(payload, "comment", "commit_id")
        else:
            commit_sha = _safe_get(payload, "issue", "pull_request", "url")  # placeholder
            commit_sha = None  # PR head SHA not present in issue_comment payload

        external_id = f"comment:{comment_id}"
        dedup_key = f"github:{repo_full_name}:{pr_number_str}"

        return NormalizedChangeEvent(
            vendor=self.vendor,
            kind="code_change_followup",
            org_id=org_id,
            installation_id=installation_id,
            external_id=external_id,
            dedup_key=dedup_key,
            repo=repo_full_name,
            ref=None,
            base_ref=None,
            commit_sha=commit_sha if isinstance(commit_sha, str) else None,
            actor=actor or None,
            target_env=None,
            action="reply",
            raw_payload=payload,
            follow_up_comment=comment_body,
            parent_external_id=pr_number_str,
        )

    # ─── Reply classification ────────────────────────────────────────

    def is_reply_to_us(
        self,
        event_type: str,
        payload: dict[str, Any],
    ) -> ReplyMatch | None:
        """Return a :class:`ReplyMatch` iff this comment is addressed to
        Aurora; ``None`` otherwise.

        The classifier is liberal in Part 1 (favours capturing
        followups for audit) but always applies the bot self-filter
        to prevent feedback loops.
        """
        return self._classify_reply(event_type, payload)

    def _classify_reply(
        self,
        event_type: str,
        payload: dict[str, Any],
    ) -> ReplyMatch | None:
        # 1. Self-filter: never re-trigger on our own comments.
        sender_login = _safe_get(payload, "sender", "login") or ""
        slug = _resolve_app_slug()
        our_bot = _bot_login_for(slug)
        if our_bot and sender_login.lower() == our_bot.lower():
            return None

        # 2. ``issue_comment`` requires the issue to have a
        #    ``pull_request`` block — Issues that aren't PRs are not
        #    in scope for the PR-review gate.
        if event_type == "issue_comment":
            if not _safe_get(payload, "issue", "pull_request"):
                return None

        repo_full_name = _safe_get(payload, "repository", "full_name")
        if not isinstance(repo_full_name, str):
            return None

        pr_number: int | None
        if event_type == "issue_comment":
            pr_number = _safe_get(payload, "issue", "number")
        else:
            pr_number = _safe_get(payload, "pull_request", "number")
        if not isinstance(pr_number, int):
            return None

        comment_id = _safe_get(payload, "comment", "id")
        if not isinstance(comment_id, int):
            return None

        body = _safe_get(payload, "comment", "body") or ""
        if not isinstance(body, str):
            return None

        in_reply_to = _safe_get(payload, "comment", "in_reply_to_id")

        # 3a. Threaded reply on an inline / review comment — always
        #     treat as a match (the dispatcher confirms by joining on
        #     ``change_investigations.inline_comment_ids``).
        if event_type == "pull_request_review_comment" and isinstance(
            in_reply_to, int
        ):
            return ReplyMatch(
                repo=repo_full_name,
                parent_pr_external_id=str(pr_number),
                comment_id=str(comment_id),
                comment_body=body,
                replier=sender_login or "unknown",
                match_kind="threaded",
            )

        # 3b. @-mention of the App's slug in the comment body.
        # The trailing negative lookahead rejects ``@aurora-testing-tool``
        # when the App slug is ``aurora`` — GitHub usernames can contain
        # hyphens, so a plain ``\b`` boundary would let those through
        # because ``-`` is a non-word character.
        if slug and re.search(
            rf"(?<![A-Za-z0-9_])@{re.escape(slug)}(?![A-Za-z0-9-])",
            body,
            re.IGNORECASE,
        ):
            # Distinguish a plain mention from an explicit re-review
            # command so the dispatcher (Part 3) can pick the right
            # follow-up prompt variant.
            body_lower = body.lower()
            is_recheck = any(cmd in body_lower for cmd in RE_REVIEW_COMMANDS)
            return ReplyMatch(
                repo=repo_full_name,
                parent_pr_external_id=str(pr_number),
                comment_id=str(comment_id),
                comment_body=body,
                replier=sender_login or "unknown",
                match_kind="re_review" if is_recheck else "mention",
            )

        return None

    # ─── Snapshot fetch ──────────────────────────────────────────────

    def fetch_snapshot(
        self,
        event: NormalizedChangeEvent,
    ) -> ChangeSnapshot:
        """One-shot fetch of diff + files + commits (+ comments on
        followups) using the installation token.

        For Part 1 the snapshot is persisted to ``change_events`` and
        the dispatcher stops there — no investigation, no review post.
        Part 2 hands the snapshot to the investigator unchanged.

        Returns a :class:`ChangeSnapshot` with as many fields populated
        as the GitHub API surfaced. Empty fields are acceptable on
        degenerate PRs (e.g. opened with no commits); the investigator
        treats an empty diff as an automatic approve.
        """
        if event.installation_id is None or event.repo is None:
            # No way to authenticate or address the repo — return an
            # empty snapshot. The dispatcher persists the raw payload
            # anyway for audit.
            logger.warning(
                "github_adapter_event=fetch_snapshot status=skipped "
                "reason=missing_install_or_repo external_id=%s",
                event.external_id,
            )
            return ChangeSnapshot(body="", diff="")

        # For followup events ``external_id`` is ``comment:<id>``; we
        # need the parent PR number to address the REST endpoints.
        pr_number = event.parent_external_id or event.external_id
        if not pr_number.isdigit():
            logger.warning(
                "github_adapter_event=fetch_snapshot status=skipped "
                "reason=non_numeric_pr external_id=%s parent=%s",
                event.external_id,
                event.parent_external_id,
            )
            return ChangeSnapshot(body="", diff="")

        owner_repo = event.repo
        installation_id = event.installation_id

        body = self._fetch_pr_body(owner_repo, pr_number, installation_id)
        diff = self._fetch_pr_diff(owner_repo, pr_number, installation_id)
        files = self._fetch_pr_files(owner_repo, pr_number, installation_id)
        commits = self._fetch_pr_commits(owner_repo, pr_number, installation_id)
        # Comments are only useful for followups (the prior verdict and
        # the engineer's reply). For initial events we skip the extra
        # API call.
        if event.kind == "code_change_followup":
            comments = self._fetch_pr_comments(owner_repo, pr_number, installation_id)
        else:
            comments = []

        return ChangeSnapshot(
            body=body,
            diff=diff,
            files=files,
            commits=commits,
            comments=comments,
        )

    def _fetch_pr_body(
        self,
        owner_repo: str,
        pr_number: str,
        installation_id: int,
    ) -> str:
        """GET PR metadata and return the body string."""
        url = f"{_API_BASE}/repos/{owner_repo}/pulls/{pr_number}"
        try:
            response = _get(url, installation_id)
        except GitHubFetchError as exc:
            logger.warning(
                "github_adapter_event=fetch_pr_body status=failed "
                "owner_repo=%s pr=%s error_class=%s",
                owner_repo,
                pr_number,
                type(exc).__name__,
            )
            return ""
        try:
            payload = response.json()
        except ValueError:
            return ""
        body = payload.get("body") if isinstance(payload, dict) else None
        return body if isinstance(body, str) else ""

    def _fetch_pr_diff(
        self,
        owner_repo: str,
        pr_number: str,
        installation_id: int,
    ) -> str:
        """GET unified diff via the diff accept header."""
        url = f"{_API_BASE}/repos/{owner_repo}/pulls/{pr_number}"
        try:
            response = _get(
                url, installation_id, accept="application/vnd.github.diff"
            )
        except GitHubFetchError as exc:
            logger.warning(
                "github_adapter_event=fetch_pr_diff status=failed "
                "owner_repo=%s pr=%s error_class=%s",
                owner_repo,
                pr_number,
                type(exc).__name__,
            )
            return ""
        return response.text or ""

    def _fetch_pr_files(
        self,
        owner_repo: str,
        pr_number: str,
        installation_id: int,
    ) -> list[dict[str, Any]]:
        """GET the changed-files list with per-file stats.

        Returns a normalised list of ``{path, status, additions,
        deletions}`` — the rest of GitHub's per-file fields (``patch``,
        ``sha``, etc.) are stripped to keep the JSONB column compact.
        The validator reconstructs hunks from the unified diff, not
        from the per-file patches.
        """
        url = f"{_API_BASE}/repos/{owner_repo}/pulls/{pr_number}/files"
        try:
            items = _paginated_get(url, installation_id)
        except GitHubFetchError as exc:
            logger.warning(
                "github_adapter_event=fetch_pr_files status=failed "
                "owner_repo=%s pr=%s error_class=%s",
                owner_repo,
                pr_number,
                type(exc).__name__,
            )
            return []
        files: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            files.append(
                {
                    "path": item.get("filename"),
                    "status": item.get("status"),
                    "additions": item.get("additions"),
                    "deletions": item.get("deletions"),
                }
            )
        return files

    def _fetch_pr_commits(
        self,
        owner_repo: str,
        pr_number: str,
        installation_id: int,
    ) -> list[dict[str, Any]]:
        """GET the commits in the PR — message + author login only.

        The investigator uses commit messages as a secondary signal for
        "what the engineer says they're doing" (often more concrete
        than the PR body). SHA is included so the validator can tie
        findings back to the specific commit that introduced them.
        """
        url = f"{_API_BASE}/repos/{owner_repo}/pulls/{pr_number}/commits"
        try:
            items = _paginated_get(url, installation_id)
        except GitHubFetchError as exc:
            logger.warning(
                "github_adapter_event=fetch_pr_commits status=failed "
                "owner_repo=%s pr=%s error_class=%s",
                owner_repo,
                pr_number,
                type(exc).__name__,
            )
            return []
        commits: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            commit_block = item.get("commit") if isinstance(item.get("commit"), dict) else {}
            message = commit_block.get("message") if isinstance(commit_block, dict) else None
            author_block = item.get("author") if isinstance(item.get("author"), dict) else {}
            author_login = author_block.get("login") if isinstance(author_block, dict) else None
            commits.append(
                {
                    "sha": item.get("sha"),
                    "message": message if isinstance(message, str) else None,
                    "author": author_login if isinstance(author_login, str) else None,
                }
            )
        return commits

    def _fetch_pr_comments(
        self,
        owner_repo: str,
        pr_number: str,
        installation_id: int,
    ) -> list[dict[str, Any]]:
        """GET both ``issue_comment`` and ``pull_request_review_comment``
        threads, merged into a single list.

        Used only on followup investigations so the LLM can see the
        full conversation Aurora previously had with the engineer.
        Each entry is normalised to
        ``{id, user_login, body, in_reply_to_id, created_at, kind}``
        where ``kind`` is ``issue`` or ``review``.
        """
        issue_url = f"{_API_BASE}/repos/{owner_repo}/issues/{pr_number}/comments"
        review_url = f"{_API_BASE}/repos/{owner_repo}/pulls/{pr_number}/comments"

        comments: list[dict[str, Any]] = []
        for kind, url in (("issue", issue_url), ("review", review_url)):
            try:
                items = _paginated_get(url, installation_id)
            except GitHubFetchError as exc:
                logger.warning(
                    "github_adapter_event=fetch_pr_comments status=partial_fail "
                    "owner_repo=%s pr=%s kind=%s error_class=%s",
                    owner_repo,
                    pr_number,
                    kind,
                    type(exc).__name__,
                )
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                user_block = item.get("user") if isinstance(item.get("user"), dict) else {}
                comments.append(
                    {
                        "id": item.get("id"),
                        "user_login": user_block.get("login")
                        if isinstance(user_block, dict)
                        else None,
                        "body": item.get("body"),
                        "in_reply_to_id": item.get("in_reply_to_id"),
                        "created_at": item.get("created_at"),
                        "kind": kind,
                    }
                )
        return comments

    # ─── Part 3 stubs ────────────────────────────────────────────────

    def post_verdict(
        self,
        event: NormalizedChangeEvent,
        investigation: dict[str, Any],
    ) -> PostedVerdict:
        """Submit Aurora's PR Review. Wired in Part 3."""
        raise NotImplementedError(
            "post_verdict is wired in Part 3 of the rollout — "
            "Part 1 only persists change_events rows for audit."
        )

    def dismiss_prior(
        self,
        event: NormalizedChangeEvent,
        prior_verdict: PostedVerdict,
    ) -> None:
        """Dismiss the prior Review before re-posting. Wired in Part 3."""
        raise NotImplementedError(
            "dismiss_prior is wired in Part 3 of the rollout."
        )
