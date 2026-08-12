"""Celery dispatcher for incoming Bitbucket webhook deliveries.

Bitbucket sibling of ``tasks.github_webhook_tasks``: the Flask ingress
(``routes/bitbucket/bitbucket_webhook.py``) validates the HMAC, records
the delivery in ``webhook_deliveries`` and enqueues this task. We route
``pullrequest:*`` events into the change-gating filter chain and enqueue
``tasks.change_gating.investigate_bitbucket_pr`` for qualifying PRs.

Handler Matrix
--------------
+---------------------------+------------------------------------------------+
| event_type (X-Event-Key)  | handler                                        |
+===========================+================================================+
| pullrequest:created       | ``_handle_pullrequest_event``                  |
| pullrequest:updated       | ``_handle_pullrequest_event``                  |
| <anything else>           | INFO ``no_handler`` + ``status=processed``     |
+---------------------------+------------------------------------------------+

``pullrequest:updated`` fires for pushes AND metadata edits (title,
description, reviewers) — the Redis ``seen`` key on ``(repo, pr, sha)``
drops same-SHA noise, so only genuinely new commits reach the task. A
reopen also arrives as ``updated`` with ``state=OPEN``. We deliberately
do NOT subscribe to ``pullrequest:approved`` or ``pullrequest:comment_*``
— those would fire when Aurora itself posts and loop.

Filter chain (mirrors ``_maybe_enqueue_change_gating``): feature flag →
gated event → not draft → PR open → destination branch equals
``connected_repos.default_branch`` (Bitbucket payloads carry no
GitHub-style ``repository.default_branch``) → Redis ``seen`` claim →
enrolled → owner resolution → enqueue. **Every skip after the seen-claim
releases the key** — otherwise that ``(repo, pr, sha)`` would be blocked
for 24 hours.

Token values are NEVER logged.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

from celery_config import celery_app
from utils.auth.log_redact import redact_token

logger = logging.getLogger(__name__)

_GATED_EVENTS = frozenset({"pullrequest:created", "pullrequest:updated"})

_RESET_RLS_SQL = "RESET myapp.current_user_id; RESET myapp.current_org_id;"


def _update_delivery_status(delivery_id: str, status: str, error: str | None = None) -> None:
    """Best-effort ``webhook_deliveries`` status update (never raises)."""
    from utils.db.connection_pool import db_pool

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                if error is None:
                    cur.execute(
                        """UPDATE webhook_deliveries
                           SET status = %s, processed_at = NOW()
                           WHERE delivery_id = %s""",
                        (status, delivery_id),
                    )
                else:
                    cur.execute(
                        """UPDATE webhook_deliveries
                           SET status = %s, error = %s, processed_at = NOW()
                           WHERE delivery_id = %s""",
                        (status, error[:500], delivery_id),
                    )
            conn.commit()
    except Exception as exc:
        logger.warning(
            "Failed to update webhook_deliveries status for delivery_id=%s status=%s: %s",
            delivery_id, status, type(exc).__name__,
        )


def _safe_get(payload: dict[str, Any], *keys: str) -> Any:
    """Walk a nested dict; None if any key is absent or non-dict."""
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _fmt_field(value: Any) -> str:
    """Render a payload field for ``key=value`` structured logs (sanitized)."""
    if value is None:
        return "<missing>"
    from utils.log_sanitizer import sanitize

    cleaned = sanitize(value).replace("\r", " ").replace("\n", " ")
    return " ".join(cleaned.split())


def _repo_gating_state(org_id: str, repo_full_name: str) -> tuple[Optional[str], bool]:
    """Return ``(default_branch, enrolled)`` for the org's Bitbucket repo.

    ``connected_repos`` is FORCE-RLS by org, and the webhook path knows the
    org directly (it's in the URL and validated by the org's HMAC secret),
    so the GUC is set from ``org_id`` — no user round-trip needed.
    ``change_gating_enabled`` is OR-ed across the org's duplicate rows
    (UNIQUE is per user), matching the GitHub selection API's semantics.
    """
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SET myapp.current_org_id = %s;", (org_id,))
            cur.execute(
                """SELECT MAX(default_branch),
                          bool_or(change_gating_enabled)
                     FROM connected_repos
                    WHERE provider = 'bitbucket'
                      AND repo_full_name = %s""",
                (repo_full_name,),
            )
            row = cur.fetchone()
            cur.execute(_RESET_RLS_SQL)
    if not row:
        return (None, False)
    return (row[0], bool(row[1]))


def _resolve_bitbucket_owner(org_id: str) -> Optional[str]:
    """Return an org member who holds active Bitbucket credentials.

    Bitbucket has no GitHub-style ``installation_id``; the investigation
    runs as whichever org user connected Bitbucket (their token is what
    the agent tools use anyway). Oldest active connection wins for
    determinism. ``user_tokens`` is RLS-protected by org.
    """
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SET myapp.current_org_id = %s;", (org_id,))
            cur.execute(
                """SELECT user_id
                     FROM user_tokens
                    WHERE provider = 'bitbucket'
                      AND is_active = TRUE
                    ORDER BY timestamp ASC, user_id ASC
                    LIMIT 1""",
            )
            row = cur.fetchone()
            cur.execute(_RESET_RLS_SQL)
    return row[0] if row and row[0] else None


def _prefilter_pullrequest(
    org_id: str, payload: dict[str, Any], event_type: str
) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    """Cheap pre-claim filters; returns ``(skip_reason, fields)``.

    ``fields`` (repo / pr_number / head_sha) is only set when every filter
    passes — the caller then claims the Redis seen-key and enqueues.
    """
    repo = _safe_get(payload, "repository", "full_name")
    pr_number = _safe_get(payload, "pullrequest", "id")
    head_sha = _safe_get(payload, "pullrequest", "source", "commit", "hash")
    dest_branch = _safe_get(payload, "pullrequest", "destination", "branch", "name")
    state = _safe_get(payload, "pullrequest", "state")
    draft = _safe_get(payload, "pullrequest", "draft")

    from utils.flags.feature_flags import is_incident_prevention_enabled

    if not is_incident_prevention_enabled():
        return ("feature_disabled", None)
    if event_type not in _GATED_EVENTS:
        return ("event_not_gated", None)
    if draft:
        return ("draft", None)
    if str(state or "").upper() != "OPEN":
        return ("not_open", None)
    if not repo or pr_number is None or not head_sha or not dest_branch:
        return ("missing_pr_fields", None)

    default_branch, enrolled = _repo_gating_state(org_id, repo)
    if not default_branch or dest_branch != default_branch:
        return ("non_default_base", None)

    fields = {
        "repo": repo,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "enrolled": enrolled,
    }
    return (None, fields)


def _claim_seen_key(dedupe_key: str, delivery_id: str):
    """Claim the Redis seen-key; returns ``(redis_client, claimed, duplicate)``.

    Redis being down is non-fatal — the investigation task has its own
    idempotency keys.
    """
    from utils.cache.redis_client import get_redis_client

    try:
        redis_client = get_redis_client()
        if redis_client is None:
            logger.warning(
                "change_gating: redis unavailable, dedupe skipped delivery_id=%s",
                delivery_id,
            )
            return (None, False, False)
        if not redis_client.set(dedupe_key, delivery_id, nx=True, ex=86400):
            return (redis_client, False, True)
        return (redis_client, True, False)
    except Exception as exc:
        logger.warning(
            "change_gating: dedupe check failed (%s), proceeding delivery_id=%s",
            type(exc).__name__, delivery_id,
        )
        return (None, False, False)


def _handle_pullrequest_event(
    org_id: str,
    payload: dict[str, Any],
    event_type: str,
    delivery_id: str,
) -> None:
    """Filter a ``pullrequest:*`` delivery and enqueue the investigation."""

    def _skip(reason: str) -> None:
        logger.info(
            "change_gating: skipped reason=%s provider=bitbucket org_id=%s "
            "repo=%s pr=%s event=%s delivery_id=%s",
            reason, org_id,
            _fmt_field(_safe_get(payload, "repository", "full_name")),
            _fmt_field(_safe_get(payload, "pullrequest", "id")),
            event_type, delivery_id,
        )

    skip_reason, fields = _prefilter_pullrequest(org_id, payload, event_type)
    if skip_reason:
        _skip(skip_reason)
        return
    repo, pr_number, head_sha = fields["repo"], fields["pr_number"], fields["head_sha"]
    # created → "opened", updated → "synchronize": the investigation task's
    # `action` argument keeps GitHub's vocabulary so downstream logging and
    # session metadata stay uniform.
    action = "opened" if event_type == "pullrequest:created" else "synchronize"

    # Dedupe on (repo, pr, head_sha) — Bitbucket fires `updated` for title
    # edits and redeliveries; same-SHA noise stops here.
    from tasks.change_gating import change_gating_keys, investigate_bitbucket_pr

    dedupe_key = change_gating_keys(repo, pr_number, head_sha, provider="bitbucket")["seen"]
    redis_client, dedupe_claimed, duplicate = _claim_seen_key(dedupe_key, delivery_id)
    if duplicate:
        _skip("duplicate_delivery")
        return

    def _release_dedupe_key() -> None:
        """Free the seen-key when no task was enqueued for this delivery —
        otherwise this (repo, pr, sha) is blocked for 24h even after the
        admin enables the repo or reconnects Bitbucket."""
        if dedupe_claimed and redis_client is not None:
            try:
                redis_client.delete(dedupe_key)
            except Exception as exc:
                logger.warning(
                    "change_gating: dedupe key release failed (%s) delivery_id=%s",
                    type(exc).__name__, delivery_id,
                )

    try:
        if not fields["enrolled"]:
            _skip("not_enrolled")
            _release_dedupe_key()
            return
        owner_user_id = _resolve_bitbucket_owner(org_id)
        if not owner_user_id:
            _skip("no_owner")
            _release_dedupe_key()
            return

        investigate_bitbucket_pr.delay(
            user_id=owner_user_id,
            repo_full_name=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            action=action,
            delivery_id=delivery_id,
        )
    except Exception:
        _release_dedupe_key()
        raise
    logger.info(
        "change_gating: enqueued provider=bitbucket repo=%s pr=%s head_sha=%s "
        "action=%s user=%s delivery_id=%s",
        _fmt_field(repo), _fmt_field(pr_number), _fmt_field(head_sha),
        action, owner_user_id, delivery_id,
    )


@celery_app.task(
    name="tasks.bitbucket_webhook_tasks.dispatch_bitbucket_webhook",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def dispatch_bitbucket_webhook(
    self,
    org_id: str,
    delivery_id: str,
    event_type: str,
    payload_json_str: str,
) -> None:
    """Route a Bitbucket webhook delivery to the correct event handler."""
    start = time.monotonic()
    logger.info(
        "bb_webhook_handler=dispatch org_id=%s event_type=%s delivery_id=%s status=received",
        org_id, event_type, delivery_id,
    )

    try:
        try:
            payload = json.loads(payload_json_str)
        except json.JSONDecodeError as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            logger.exception(
                "bb_webhook_handler=dispatch org_id=%s event_type=%s delivery_id=%s "
                "status=failed duration_ms=%d error_class=%s reason=invalid_json",
                org_id, event_type, delivery_id, duration_ms, type(exc).__name__,
            )
            _update_delivery_status(
                delivery_id, status="failed",
                error=f"invalid_json: {type(exc).__name__}",
            )
            return

        if not isinstance(payload, dict):
            _update_delivery_status(delivery_id, status="processed")
            return

        if event_type in _GATED_EVENTS:
            _handle_pullrequest_event(org_id, payload, event_type, delivery_id)
        else:
            duration_ms = int((time.monotonic() - start) * 1000)
            logger.info(
                "bb_webhook_handler=%s org_id=%s delivery_id=%s status=no_handler "
                "duration_ms=%d",
                event_type, org_id, delivery_id, duration_ms,
            )
        _update_delivery_status(delivery_id, status="processed")
    except Exception as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.exception(
            "bb_webhook_handler=dispatch org_id=%s event_type=%s delivery_id=%s "
            "status=failed duration_ms=%d error_class=%s msg=%s",
            org_id, event_type, delivery_id, duration_ms,
            type(exc).__name__, redact_token(str(exc)),
        )
        retries = getattr(self.request, "retries", 0) or 0
        max_retries = getattr(self, "max_retries", 0) or 0
        if retries >= max_retries:
            _update_delivery_status(
                delivery_id, status="failed", error=type(exc).__name__,
            )
        raise self.retry(exc=exc)
