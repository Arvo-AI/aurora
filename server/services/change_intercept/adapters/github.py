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


def _coerce_str(value: Any) -> str:
    """Return ``value`` as a stripped string, or empty for non-strings.

    Local copy of the same helper in :mod:`verdict_validator` to keep
    the adapter free of cross-package imports — both modules treat the
    helper as a tiny private convenience, not a shared contract.
    """
    if isinstance(value, str):
        return value.strip()
    return ""


def _parent_comment_is_aurora(
    *,
    payload: dict[str, Any],
    in_reply_to_id: int,
    repo_full_name: str,
    pr_number: int,
    our_bot: str,
) -> bool:
    """True iff the comment ``in_reply_to_id`` was authored by Aurora.

    Tries three signals in order of cheapness:
      1. ``payload.comment.in_reply_to.user.login`` — GitHub embeds
         the parent in some webhook versions; if present, settle here.
      2. Local DB lookup against
         ``change_investigations.inline_comment_ids`` for the matching
         PR. This is the cheapest cross-machine source of truth.
      3. Fall back to ``False`` (drop the reply) — a missed positive
         is far less costly than spoofed re-review traffic.

    Never raises; DB failure / missing config logs and returns False.
    """
    if not our_bot:
        return False

    embedded_login = _safe_get(
        payload, "comment", "in_reply_to", "user", "login"
    )
    if isinstance(embedded_login, str) and embedded_login.lower() == our_bot.lower():
        return True

    try:
        from utils.db.connection_pool import db_pool

        dedup_key = f"github:{repo_full_name}:{pr_number}"
        target = str(in_reply_to_id)
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                # No RLS context — we're checking a global property
                # (was this comment ID written to our DB), and the
                # join is bounded by the dedup_key so cross-org leakage
                # is impossible.
                cur.execute(
                    """SELECT 1
                         FROM change_investigations ci
                         JOIN change_events ce ON ce.id = ci.change_event_id
                        WHERE ce.dedup_key = %s
                          AND ci.inline_comment_ids IS NOT NULL
                          AND ci.inline_comment_ids ? %s
                        LIMIT 1""",
                    (dedup_key, target),
                )
                return cur.fetchone() is not None
    except Exception as exc:
        logger.warning(
            "github_adapter_event=parent_lookup_failed in_reply_to=%s "
            "error_class=%s",
            in_reply_to_id,
            type(exc).__name__,
        )
        return False


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

    Carries ``status_code`` when the failure originated from a non-2xx
    HTTP response so callers can discriminate benign 4xx (404 review
    already dismissed, 422 PR closed) from genuine outages (5xx, 401
    auth failure). ``None`` for transport-level failures (DNS,
    connection refused, timeout).
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


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
            f"(status={response.status_code}): {_redact(response.text or '')[:200]}",
            status_code=response.status_code,
        )
    return response


def _post(
    url: str,
    installation_id: int,
    *,
    json_body: dict[str, Any],
    accept: str = "application/vnd.github+json",
) -> requests.Response:
    """Issue a POST with the App's installation token.

    Used by ``post_verdict`` to submit a Review and (potentially) by
    a future ``post_comment`` helper for the thrash-guard escalation
    note. Raises :class:`GitHubFetchError` on transport errors or
    non-2xx; the caller catches and decides whether to surface to the
    operator or retry.
    """
    try:
        headers = _auth_headers(installation_id, accept)
    except (GitHubAppInstallationNotFound, GitHubAppInstallationSuspended):
        raise
    except GitHubAppTokenError as exc:
        raise GitHubFetchError(
            f"Failed to obtain installation token for installation_id={installation_id}: "
            f"{type(exc).__name__}: {_redact(str(exc))}"
        ) from exc

    try:
        response = requests.post(
            url,
            headers=headers,
            json=json_body,
            timeout=_GITHUB_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise GitHubFetchError(
            f"GitHub POST failed for url={url}: "
            f"{type(exc).__name__}: {_redact(str(exc))}"
        ) from exc

    if response.status_code >= 300:
        raise GitHubFetchError(
            f"GitHub POST returned non-2xx for url={url} "
            f"(status={response.status_code}): {_redact(response.text or '')[:300]}",
            status_code=response.status_code,
        )
    return response


def _put(
    url: str,
    installation_id: int,
    *,
    json_body: dict[str, Any] | None = None,
    accept: str = "application/vnd.github+json",
    ok_statuses: tuple[int, ...] = (200, 201, 204),
) -> requests.Response:
    """Issue a PUT with the App's installation token.

    Used by ``dismiss_prior`` to call
    ``PUT /pulls/{n}/reviews/{id}/dismissals``. Accepts a tuple of
    success status codes — GitHub returns 200 on dismissal but other
    PUT endpoints may return 201/204.
    """
    try:
        headers = _auth_headers(installation_id, accept)
    except (GitHubAppInstallationNotFound, GitHubAppInstallationSuspended):
        raise
    except GitHubAppTokenError as exc:
        raise GitHubFetchError(
            f"Failed to obtain installation token for installation_id={installation_id}: "
            f"{type(exc).__name__}: {_redact(str(exc))}"
        ) from exc

    try:
        response = requests.put(
            url,
            headers=headers,
            json=json_body if json_body is not None else {},
            timeout=_GITHUB_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise GitHubFetchError(
            f"GitHub PUT failed for url={url}: "
            f"{type(exc).__name__}: {_redact(str(exc))}"
        ) from exc

    if response.status_code not in ok_statuses:
        raise GitHubFetchError(
            f"GitHub PUT returned unexpected status for url={url} "
            f"(status={response.status_code}): {_redact(response.text or '')[:300]}",
            status_code=response.status_code,
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

        # 3a. Threaded reply on an inline / review comment. The parent
        #     comment MUST be authored by Aurora — otherwise any
        #     human↔human review thread would trigger us. We verify
        #     two ways: (a) check the payload's nested
        #     ``in_reply_to.user.login`` if GitHub embedded the parent,
        #     and (b) consult the local change_investigations index for
        #     the inline-comment-id (cheap DB lookup, no extra REST
        #     round-trip). Either match suffices; both missing → drop.
        if event_type == "pull_request_review_comment" and isinstance(
            in_reply_to, int
        ):
            if _parent_comment_is_aurora(
                payload=payload,
                in_reply_to_id=in_reply_to,
                repo_full_name=repo_full_name,
                pr_number=pr_number,
                our_bot=our_bot,
            ):
                return ReplyMatch(
                    repo=repo_full_name,
                    parent_pr_external_id=str(pr_number),
                    comment_id=str(comment_id),
                    comment_body=body,
                    replier=sender_login or "unknown",
                    match_kind="threaded",
                )
            # Threaded reply but not on one of our comments → drop.
            return None

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

    # ─── Live review posting (Part 3) ────────────────────────────────

    def post_verdict(
        self,
        event: NormalizedChangeEvent,
        investigation: dict[str, Any],
    ) -> PostedVerdict:
        """Submit Aurora's PR Review.

        Builds a single ``POST /repos/{owner}/{repo}/pulls/{n}/reviews``
        call with ``{event, body, comments[]}``. The ``comments[]``
        array maps directly to the ``rendered_review.inline_comments``
        the review-poster produced — each entry becomes one inline
        per-hunk comment on the PR.

        Args:
            event: the parsed event the verdict is for. Must carry
                ``installation_id``, ``repo``, and (for code_change
                kinds) ``external_id`` set to the PR number.
            investigation: rendered review payload — caller passes
                ``{verdict_event, body, inline_comments, commit_sha}``
                where ``inline_comments`` is the list of
                ``{path, start_line, end_line, body}`` dicts produced
                by ``pr_review_poster.render_review``. ``commit_sha``
                pins the review to the exact SHA the investigator
                analysed (GitHub's API uses this to anchor inline
                comments correctly across subsequent pushes).

        Returns:
            :class:`PostedVerdict` carrying the review id (persisted
            as ``change_investigations.external_verdict_id``) and the
            per-comment ids (persisted as
            ``change_investigations.inline_comment_ids``).

        Raises:
            GitHubFetchError: on any non-2xx response or transport
                failure. The Celery task catches this and persists the
                investigation without ``external_verdict_id`` so the
                row reflects a posting failure rather than a missing
                investigation.
        """
        if event.installation_id is None or event.repo is None:
            raise GitHubFetchError(
                "post_verdict requires installation_id + repo on the event"
            )
        pr_number = event.parent_external_id or event.external_id
        if not pr_number or not pr_number.isdigit():
            raise GitHubFetchError(
                f"post_verdict requires a numeric PR id; got {pr_number!r}"
            )

        verdict_event = _coerce_str(investigation.get("verdict_event")) or "COMMENT"
        body = _coerce_str(investigation.get("body"))
        inline_comments_in = investigation.get("inline_comments") or []
        if not isinstance(inline_comments_in, list):
            inline_comments_in = []

        github_comments: list[dict[str, Any]] = []
        for raw in inline_comments_in:
            if not isinstance(raw, dict):
                continue
            translated = _to_github_review_comment(raw)
            if translated is not None:
                github_comments.append(translated)

        url = f"{_API_BASE}/repos/{event.repo}/pulls/{pr_number}/reviews"
        payload: dict[str, Any] = {
            "event": verdict_event,
            "body": body,
        }
        # Pin the review to the investigated SHA when we have one. GitHub
        # uses ``commit_id`` to anchor inline comments to a specific
        # commit on the PR — without it, comments anchor to HEAD which
        # can race with engineer pushes mid-investigation.
        commit_sha = _coerce_str(investigation.get("commit_sha")) or _coerce_str(
            event.commit_sha
        )
        if commit_sha:
            payload["commit_id"] = commit_sha
        if github_comments:
            payload["comments"] = github_comments

        response = _post(url, event.installation_id, json_body=payload)
        try:
            response_payload = response.json()
        except ValueError as exc:
            raise GitHubFetchError(
                f"post_verdict received non-JSON body: {type(exc).__name__}"
            ) from exc

        review_id = response_payload.get("id")
        if not isinstance(review_id, int):
            raise GitHubFetchError(
                "post_verdict response missing 'id' field"
            )

        # The Reviews API doesn't return per-comment ids on the initial
        # POST — fetch the review's comments to capture them so a
        # subsequent ``dismiss_prior`` can target them individually if
        # we ever need per-comment dismissal. The fetch is cheap (one
        # paginated GET) and only fires when we actually posted inline
        # comments.
        inline_comment_ids: list[str] = []
        if github_comments:
            try:
                inline_comment_ids = _fetch_review_comment_ids(
                    repo=event.repo,
                    pr_number=pr_number,
                    review_id=review_id,
                    installation_id=event.installation_id,
                )
            except GitHubFetchError as exc:
                # Non-fatal — we still have the review id, and the
                # next dismiss_prior will dismiss the whole review.
                logger.warning(
                    "github_adapter_event=post_verdict status=comment_id_fetch_failed "
                    "review_id=%s error_class=%s",
                    review_id,
                    type(exc).__name__,
                )

        logger.info(
            "github_adapter_event=post_verdict status=ok review_id=%s "
            "verdict_event=%s comment_count=%d repo=%s pr=%s",
            review_id,
            verdict_event,
            len(github_comments),
            event.repo,
            pr_number,
        )

        return PostedVerdict(
            verdict_id=str(review_id),
            inline_comment_ids=inline_comment_ids,
        )

    def dismiss_prior(
        self,
        event: NormalizedChangeEvent,
        prior_verdict: PostedVerdict,
    ) -> None:
        """Dismiss the prior Review before re-posting.

        Calls ``PUT /repos/{owner}/{repo}/pulls/{n}/reviews/{id}/dismissals``
        with a brief message. Inline comments are left in place but
        marked outdated by GitHub when their anchored lines change on
        a subsequent push.

        404 / 422 are treated as success: the prior review may already
        be dismissed, or the PR may be closed / merged. Either way we
        don't want to crash the followup investigation that triggered
        this call.

        Args:
            event: the parsed event we're re-running for.
            prior_verdict: the ``PostedVerdict`` we previously stored
                on ``change_investigations.external_verdict_id``.
        """
        if event.installation_id is None or event.repo is None:
            logger.warning(
                "github_adapter_event=dismiss_prior status=skipped "
                "reason=missing_install_or_repo external_id=%s",
                event.external_id,
            )
            return
        pr_number = event.parent_external_id or event.external_id
        if not pr_number or not pr_number.isdigit():
            logger.warning(
                "github_adapter_event=dismiss_prior status=skipped "
                "reason=non_numeric_pr",
            )
            return
        if not prior_verdict.verdict_id:
            return

        url = (
            f"{_API_BASE}/repos/{event.repo}/pulls/{pr_number}/reviews/"
            f"{prior_verdict.verdict_id}/dismissals"
        )
        try:
            _put(
                url,
                event.installation_id,
                json_body={
                    "message": (
                        "Aurora is re-evaluating this PR with new context "
                        "(commit push or engineer reply)."
                    ),
                    "event": "DISMISS",
                },
            )
            logger.info(
                "github_adapter_event=dismiss_prior status=ok "
                "review_id=%s repo=%s pr=%s",
                prior_verdict.verdict_id,
                event.repo,
                pr_number,
            )
        except GitHubFetchError as exc:
            # Discriminate by status: 404/422 are benign (review already
            # dismissed / PR closed / merged). 5xx + 401/403 are real
            # outages — surface them so the caller can decide whether
            # to skip the new post (avoid stacking review_request states)
            # rather than silently steamrolling.
            if exc.status_code in (404, 422):
                logger.info(
                    "github_adapter_event=dismiss_prior status=ignored_benign "
                    "review_id=%s http_status=%s",
                    prior_verdict.verdict_id,
                    exc.status_code,
                )
                return
            logger.warning(
                "github_adapter_event=dismiss_prior status=upstream_error "
                "review_id=%s http_status=%s reason=%s",
                prior_verdict.verdict_id,
                exc.status_code,
                type(exc).__name__,
            )
            raise


# ─── Helpers private to the live-posting path ───────────────────────


_GITHUB_REVIEW_EVENTS = frozenset(
    {"APPROVE", "REQUEST_CHANGES", "COMMENT", "PENDING"}
)


def _to_github_review_comment(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Translate one ``RenderedReview.inline_comments`` entry into the
    shape GitHub's Reviews API expects.

    The vendor-neutral entry has ``{path, start_line, end_line, body}``.
    GitHub's wire format is ``{path, line, side, body[, start_line,
    start_side]}`` where ``line`` is the END line and ``start_line`` is
    populated only for multi-line ranges. Returns ``None`` for malformed
    entries (the caller drops them without crashing).
    """
    path = _coerce_str(raw.get("path"))
    body = _coerce_str(raw.get("body"))
    start_line = raw.get("start_line")
    end_line = raw.get("end_line")
    if not path or not body:
        return None
    try:
        start_line_i = int(start_line) if start_line is not None else None
    except (TypeError, ValueError):
        return None
    try:
        end_line_i = int(end_line) if end_line is not None else None
    except (TypeError, ValueError):
        end_line_i = None
    if start_line_i is None or start_line_i <= 0:
        return None

    comment: dict[str, Any] = {
        "path": path,
        "body": body,
        "side": "RIGHT",
    }
    if end_line_i is not None and end_line_i > start_line_i:
        comment["start_line"] = start_line_i
        comment["start_side"] = "RIGHT"
        comment["line"] = end_line_i
    else:
        comment["line"] = start_line_i
    return comment


def _fetch_review_comment_ids(
    *,
    repo: str,
    pr_number: str,
    review_id: int,
    installation_id: int,
) -> list[str]:
    """GET the per-comment ids for a freshly posted Review.

    Uses ``GET /repos/{owner}/{repo}/pulls/{n}/reviews/{id}/comments``.
    Returns a list of stringified comment ids the caller persists to
    ``change_investigations.inline_comment_ids``.
    """
    url = f"{_API_BASE}/repos/{repo}/pulls/{pr_number}/reviews/{review_id}/comments"
    items = _paginated_get(url, installation_id)
    ids: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        comment_id = item.get("id")
        if isinstance(comment_id, int):
            ids.append(str(comment_id))
    return ids
