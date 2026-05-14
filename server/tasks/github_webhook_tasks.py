"""Celery dispatcher for incoming GitHub App webhook deliveries.

Wave 2 (Task 11) shipped the dispatcher stub. Wave 3 (Task 13) wires the
``installation`` and ``installation_repositories`` state-sync handlers.
Wave 3 (Task 14) wires the code-event handlers (``pull_request``,
``issues``, ``deployment``, ``deployment_status``, ``workflow_run``,
``check_run``, ``check_suite``).

Handler Matrix
--------------
+--------------------------------+--------------------------------------------------+
| event_type                     | handler                                          |
+================================+==================================================+
| installation                   | ``_handle_installation_event``                   |
| installation_repositories      | ``_handle_installation_repositories_event``      |
| pull_request                   | ``_handle_pull_request_event``                   |
| issue_comment                  | ``_handle_issue_comment_event``                  |
| pull_request_review_comment    | ``_handle_pull_request_review_comment_event``    |
| issues                         | ``_handle_issues_event``                         |
| deployment                     | ``_handle_deployment_event``                     |
| deployment_status              | ``_handle_deployment_status_event``              |
| workflow_run                   | ``_handle_workflow_run_event``                   |
| check_run                      | ``_handle_check_run_event``                      |
| check_suite                    | ``_handle_check_suite_event``                    |
| <anything else>                | WARNING ``unknown_event`` + ``status=processed`` |
+--------------------------------+--------------------------------------------------+

The ``pull_request``, ``issue_comment`` and ``pull_request_review_comment``
handlers additionally invoke ``_ingest_change_intercept_event`` to persist
a ``change_events`` row per Aurora org linked to the installation —
the Phase 1a "PR Risk Review" pipeline (see
``services.change_intercept``). Phase 1a Part 1 stops at persistence;
Part 2 enqueues an investigation Celery task off the new row.

Excluded by design (per plan): ``push``, ``release`` — the Aurora
GitHub App is NOT subscribed to these events; they should never reach
the dispatcher. If they do, they fall through the unknown-event path.

Per-event payload field schemas (cross-reference with GitHub docs):
- ``pull_request``  : repo, pr_number, action, state, merged_at,
                      head_sha, base_sha, author, title
- ``issues``        : repo, issue_number, action, state, author, title
- ``deployment``    : repo, deployment_id, environment, ref, sha, creator
- ``deployment_status``: repo, deployment_id, state, environment,
                         target_url, creator
- ``workflow_run``  : repo, workflow_run_id, name, conclusion, head_sha,
                      head_branch, run_attempt
- ``check_run``     : repo, check_id, name, status, conclusion, head_sha
- ``check_suite``   : repo, check_id, name, status, conclusion, head_sha

GitHub webhook payload reference:
https://docs.github.com/en/webhooks/webhook-events-and-payloads

Design notes
------------
- The Flask endpoint validates the HMAC signature, idempotently records
  the delivery in ``webhook_deliveries`` and only then enqueues this
  task. By the time we run, the row exists with ``status='processing'``.
- We accept the body as a JSON string (not a dict) so Celery's JSON
  serializer doesn't have to round-trip nested objects, and so the
  Flask side can pass the byte-exact body without a re-serialize step.
- Each per-event handler runs the action AND marks
  ``webhook_deliveries.status='processed'`` inside a single DB
  transaction. On exception, the transaction rolls back, the dispatcher
  marks ``status='failed'`` (best-effort, separate connection), and
  re-raises so Celery's retry policy applies.
- Code-event handlers (Task 14) are pure structured-log emitters as
  the MVP — no bespoke event tables, no GitHub API calls. Each handler
  emits ONE INFO line tagged ``event_type=<name>`` so a future event-
  store work item can grep them. Missing payload fields render as
  ``<field>=<missing>`` rather than crashing the worker.
- We never log the full payload at INFO; only ``event_type``,
  ``action``, ``installation_id``, ``account_login`` and ``delivery_id``
  are safe identifiers.

Standard log keys
-----------------
This module emits structured ``key=value`` log lines on the canonical
key ``gh_webhook_handler``. The known handler values are:

    * ``dispatch``                    — entry / failure of the router itself.
    * ``installation``                — ``installation`` event handler.
    * ``installation_repositories``   — ``installation_repositories`` handler.
    * ``<event_type>``                — fall-through for not-yet-wired events.

Other keys present on these lines:

    * ``action``           — payload's ``action`` field, or ``-`` if absent.
    * ``installation_id``  — installation id from payload (when extracted).
    * ``account_login``    — installation account's login (when extracted).
    * ``delivery_id``      — ``X-GitHub-Delivery`` UUID (always present).
    * ``status``           — ``processed | failed | no_handler | invalid_json | ignored_unknown_action | noop_lazy_population``.
    * ``duration_ms``      — wall-clock for the handler body.
    * ``error_class``      — exception class name (failure paths only).
    * ``rows_deleted`` / ``rows_updated`` / ``repo_count`` — action-specific counters.

Token values are NEVER logged. Any exception text we include in the
``status=failed`` line is passed through ``redact_token()`` first.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from celery_config import celery_app
from utils.auth.log_redact import redact_token

logger = logging.getLogger(__name__)


# ─── Change-Intercept persistence (Phase 1a, Part 1) ─────────────────
#
# The handlers below extend the existing RCA-correlation logs with a
# new persistence step: a row in ``change_events`` per Aurora org
# linked to the GitHub installation. This is the foundation the Part 2
# investigator and Part 3 review-poster build on; Part 1 stops at
# persistence (no LLM call, no PR comment).
#
# Why the change-intercept ingest lives inside the existing dispatcher
# instead of behind a new Celery task: at Part 1 scope the work is a
# small DB insert + a couple of REST calls. Adding a new queue would
# require a separate worker definition and complicate ops without
# buying isolation we need yet. Part 2 introduces ``launch_investigation``
# on a dedicated ``change_intercept`` queue because the LLM call is
# heavy and we want its retries / timeouts independent of the webhook
# dispatcher.


def _resolve_orgs_for_installation(installation_id: int) -> list[tuple[str, str]]:
    """Return active ``(org_id, user_id)`` tuples for the installation.

    One row per org — when an installation is linked by multiple users
    in the same Aurora org, we pick the primary user (and fall back
    to the earliest linked) so each org has a stable user_id for
    ``set_rls_context``. Disconnected links are skipped.

    Empty result means no Aurora user has linked this installation yet
    — the dispatcher acknowledges the webhook delivery but persists
    no ``change_events`` row.
    """
    from utils.db.connection_pool import db_pool

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                # No RLS needed — user_github_installations is intentionally
                # NOT RLS-protected so cross-org enumeration works.
                cur.execute(
                    """SELECT DISTINCT ON (org_id) org_id, user_id
                         FROM user_github_installations
                        WHERE installation_id = %s
                          AND disconnected_at IS NULL
                          AND org_id IS NOT NULL
                          AND org_id <> ''
                        ORDER BY org_id, is_primary DESC, linked_at ASC""",
                    (installation_id,),
                )
                return [
                    (row[0], row[1])
                    for row in cur.fetchall()
                    if row[0] and row[1]
                ]
    except Exception as exc:
        logger.warning(
            "change_intercept_event=resolve_orgs_failed installation_id=%s "
            "error_class=%s",
            installation_id,
            type(exc).__name__,
        )
        return []


def _lookup_parent_event_id(
    cur,
    org_id: str,
    dedup_key: str,
) -> str | None:
    """Return the most recent code_change ``change_events.id`` for the
    given ``(org_id, dedup_key)`` so a followup row can link back.

    Returns ``None`` when no parent exists (the engineer commented on a
    PR Aurora has never seen — possible if the App was installed AFTER
    the PR was opened). The followup row is still persisted; the link
    is just left null.
    """
    cur.execute(
        """SELECT id FROM change_events
            WHERE org_id = %s AND dedup_key = %s AND kind = 'code_change'
            ORDER BY received_at DESC
            LIMIT 1""",
        (org_id, dedup_key),
    )
    row = cur.fetchone()
    return row[0] if row else None


# Hard cap on the unified-diff bytes we persist to change_events.diff.
# Without a cap, a 100MB monorepo refactor would blow Postgres TOAST
# limits + downstream parser memory. The investigator's prompt builder
# truncates separately at ~80K chars; this cap protects the persistence
# tier independently. Diffs over the cap are stored truncated with a
# trailing marker so the investigator sees they were clipped.
_PERSIST_DIFF_CHAR_CAP = 1_000_000


def _truncate_diff_for_persist(diff: str) -> str:
    if not diff or len(diff) <= _PERSIST_DIFF_CHAR_CAP:
        return diff or ""
    return diff[:_PERSIST_DIFF_CHAR_CAP] + (
        f"\n... [persistence-truncated at {_PERSIST_DIFF_CHAR_CAP} chars]\n"
    )


def _persist_change_event(
    event: Any,  # NormalizedChangeEvent — typed loosely to avoid an import cycle
    snapshot: Any,  # ChangeSnapshot
    user_id: str,
    delivery_id: str,
) -> str | None:
    """INSERT one ``change_events`` row under RLS context.

    Idempotent via the ``(org_id, vendor, external_id, commit_sha, kind)``
    UNIQUE constraint: a GitHub retry or a Celery retry that gets past
    ``webhook_deliveries`` dedup lands on the unique-violation branch
    and is treated as a successful re-ack rather than an error.

    Args:
        event: parsed event from the adapter. Must have ``org_id``
            populated (used for the RLS sanity check below).
        snapshot: fetched diff + files + commits + body + comments.
        user_id: any user from the target org — used to set RLS
            context. ``set_rls_context`` looks up the actual org from
            this user and refuses to set RLS if it doesn't match.
        delivery_id: ``X-GitHub-Delivery`` UUID for log correlation.

    Returns:
        The new row's UUID as a string when persistence succeeded, or
        ``None`` on dedup (UniqueViolation) / RLS-mismatch / failure.
        The dispatcher uses this return value to decide whether to
        enqueue ``launch_investigation`` — re-dispatching on dedup
        would duplicate work, and an RLS mismatch is a hard failure
        we don't want to amplify.
    """
    from psycopg2 import IntegrityError, errors as psycopg_errors

    from utils.auth.stateless_auth import set_rls_context
    from utils.db.connection_pool import db_pool

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                org_id = set_rls_context(
                    cur, conn, user_id, log_prefix="[change_intercept]"
                )
                if not org_id:
                    logger.warning(
                        "change_intercept_event=persist_skipped reason=no_rls_context "
                        "delivery_id=%s user=%s",
                        delivery_id,
                        user_id,
                    )
                    return None
                if org_id != event.org_id:
                    # Defence in depth: the dispatcher passes the user
                    # from the same org as the event, so this mismatch
                    # would indicate a stale RLS cache or a corrupted
                    # user→org link. Fail-closed.
                    logger.error(
                        "change_intercept_event=persist_skipped reason=org_mismatch "
                        "delivery_id=%s event_org=%s rls_org=%s",
                        delivery_id,
                        event.org_id,
                        org_id,
                    )
                    return None

                parent_event_id: str | None = None
                if event.kind == "code_change_followup":
                    parent_event_id = _lookup_parent_event_id(
                        cur, org_id, event.dedup_key
                    )

                try:
                    cur.execute(
                        """INSERT INTO change_events (
                               org_id, vendor, kind, external_id, dedup_key,
                               installation_id, repo, ref, base_ref, commit_sha,
                               actor, target_env, change_body, change_diff,
                               change_files, change_commits, follow_up_comment,
                               parent_event_id, payload
                           ) VALUES (
                               %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s::jsonb
                           ) RETURNING id""",
                        (
                            event.org_id,
                            event.vendor,
                            event.kind,
                            event.external_id,
                            event.dedup_key,
                            event.installation_id,
                            event.repo,
                            event.ref,
                            event.base_ref,
                            event.commit_sha,
                            event.actor,
                            event.target_env,
                            snapshot.body or None,
                            _truncate_diff_for_persist(snapshot.diff or "") or None,
                            json.dumps(snapshot.files or []),
                            json.dumps(snapshot.commits or []),
                            event.follow_up_comment,
                            parent_event_id,
                            json.dumps(event.raw_payload or {}),
                        ),
                    )
                    new_id_row = cur.fetchone()
                    conn.commit()
                    logger.info(
                        "change_intercept_event=persisted delivery_id=%s "
                        "org_id=%s kind=%s dedup_key=%s diff_bytes=%d files=%d "
                        "commits=%d",
                        delivery_id,
                        event.org_id,
                        event.kind,
                        event.dedup_key,
                        len(snapshot.diff or ""),
                        len(snapshot.files or []),
                        len(snapshot.commits or []),
                    )
                    return str(new_id_row[0]) if new_id_row else None
                except (IntegrityError, psycopg_errors.UniqueViolation):
                    conn.rollback()
                    logger.info(
                        "change_intercept_event=deduped delivery_id=%s "
                        "org_id=%s kind=%s dedup_key=%s",
                        delivery_id,
                        event.org_id,
                        event.kind,
                        event.dedup_key,
                    )
                    return None
    except Exception as exc:
        # Transient: re-raise so the dispatcher marks the delivery
        # 'failed' and GitHub's redelivery re-runs the path. Without
        # this, a DB blip during persistence would permanently lose
        # the event for that PR.
        logger.warning(
            "change_intercept_event=persist_failed delivery_id=%s kind=%s "
            "error_class=%s",
            delivery_id,
            getattr(event, "kind", "unknown"),
            type(exc).__name__,
        )
        raise _TransientIngestError(
            f"persist_failed: {type(exc).__name__}"
        ) from exc


# PR actions that justify spending an LLM call. ``synchronize`` is
# deliberately excluded — per the resolved open question we don't
# re-investigate on every push; comment-reuse on subsequent pushes is
# handled by GitHub's automatic outdated-comment behaviour. The
# engineer can request a fresh investigation via ``@<slug> re-review``.
_INVESTIGATE_PR_ACTIONS: frozenset[str] = frozenset(
    {"opened", "reopened", "ready_for_review"}
)


def _should_enqueue_investigation(event: Any) -> bool:
    """Return True iff the parsed event warrants a fresh investigation.

    Code-change events fire only on opened / reopened / ready_for_review.
    Code-change-followup events ALWAYS fire (the dispatcher's
    ``is_reply_to_us`` already filtered out non-Aurora chatter and bot
    self-comments, so anything reaching here is an engineer reply we
    explicitly want to respond to).
    """
    kind = getattr(event, "kind", "")
    action = getattr(event, "action", "")
    if kind == "code_change":
        return action in _INVESTIGATE_PR_ACTIONS
    if kind == "code_change_followup":
        return True
    return False


def _enqueue_investigation(
    change_event_id: str,
    user_id: str,
    delivery_id: str,
) -> None:
    """Dispatch ``launch_investigation`` for ``change_event_id``.

    Failure is non-fatal: the row is already persisted, so the
    investigation can be backfilled later by a calibration job or an
    ops-side recheck if Celery hiccups. The webhook dispatcher MUST
    NOT crash on enqueue failure — that would mark
    ``webhook_deliveries`` as failed and GitHub would retry the whole
    delivery, duplicating the event row insertion attempt.
    """
    try:
        from services.change_intercept.tasks import launch_investigation

        launch_investigation.delay(
            change_event_id=change_event_id, user_id_for_rls=user_id
        )
        logger.info(
            "change_intercept_event=investigation_enqueued delivery_id=%s "
            "change_event_id=%s",
            delivery_id,
            change_event_id,
        )
    except Exception as exc:
        logger.warning(
            "change_intercept_event=enqueue_failed delivery_id=%s "
            "change_event_id=%s error_class=%s",
            delivery_id,
            change_event_id,
            type(exc).__name__,
        )


class _TransientIngestError(Exception):
    """Raised inside _ingest_change_intercept_event when a failure mode
    is recoverable by retry (DB blip, transient GitHub 5xx, etc.).

    The dispatcher catches this and re-raises so Celery's retry policy
    applies and ``webhook_deliveries.status`` flips to 'failed', which
    causes the next GitHub retry of the same X-GitHub-Delivery to
    re-dispatch. Without this, transient errors during persistence
    would silently drop the event and the PR would never be reviewed.

    DETERMINISTIC failures (no linked orgs, parse returned None,
    unknown vendor) do NOT raise — those would never succeed on retry,
    so the dispatcher acknowledges and moves on.
    """


def _ingest_change_intercept_event(
    event_type: str,
    payload: dict[str, Any],
    delivery_id: str,
) -> None:
    """Run the change-intercept adapter pipeline for one webhook event.

    For each Aurora org linked to the installation:
        1. Adapter parses the payload into a ``NormalizedChangeEvent``.
        2. Adapter fetches the snapshot (diff + files + commits + body).
        3. We INSERT a ``change_events`` row under RLS context.

    Discriminates failure modes:
      - Deterministic / non-recoverable (no linked orgs, parse=None,
        unknown vendor) → log + return; webhook acknowledged.
      - Transient (snapshot fetch, DB, enqueue failures) → raise
        :class:`_TransientIngestError` so the dispatcher marks the
        delivery 'failed' and GitHub's redelivery re-runs the path.

    Args:
        event_type: ``pull_request`` / ``issue_comment`` /
            ``pull_request_review_comment``.
        payload: parsed webhook body.
        delivery_id: ``X-GitHub-Delivery`` UUID for log correlation.
    """
    installation_id = _extract_installation_id(payload)
    if installation_id is None:
        return

    linked = _resolve_orgs_for_installation(installation_id)
    if not linked:
        logger.info(
            "change_intercept_event=no_linked_orgs delivery_id=%s installation_id=%s "
            "event_type=%s",
            delivery_id,
            installation_id,
            event_type,
        )
        return

    try:
        from services.change_intercept.adapters.registry import (
            UnknownVendorError,
            get_adapter,
        )

        adapter = get_adapter("github")
    except UnknownVendorError:
        return
    except Exception as exc:
        logger.warning(
            "change_intercept_event=adapter_load_failed delivery_id=%s "
            "error_class=%s",
            delivery_id,
            type(exc).__name__,
        )
        return

    for org_id, user_id in linked:
        try:
            event = adapter.parse(event_type, payload, org_id=org_id)
        except Exception as exc:
            logger.warning(
                "change_intercept_event=parse_failed delivery_id=%s org_id=%s "
                "event_type=%s error_class=%s",
                delivery_id,
                org_id,
                event_type,
                type(exc).__name__,
            )
            continue

        if event is None:
            continue

        try:
            snapshot = adapter.fetch_snapshot(event)
        except Exception as exc:
            # Persist with an empty snapshot rather than dropping the event
            # entirely — the webhook delivery and parsed-event metadata are
            # still valuable signals (we know the PR exists; we just can't
            # render the diff yet). Part 2 can re-fetch on demand.
            logger.warning(
                "change_intercept_event=fetch_snapshot_failed delivery_id=%s "
                "org_id=%s dedup_key=%s error_class=%s",
                delivery_id,
                org_id,
                event.dedup_key,
                type(exc).__name__,
            )
            # Lazy import to avoid a top-level cycle.
            from services.change_intercept.adapters.base import ChangeSnapshot

            snapshot = ChangeSnapshot(body="", diff="")

        new_event_id = _persist_change_event(
            event, snapshot, user_id=user_id, delivery_id=delivery_id
        )

        # Enqueue investigation for actionable events (Part 2). Per the
        # resolved open question on token spend, ``synchronize`` is
        # persisted for audit but does NOT re-investigate; the engineer
        # must explicitly ``@<slug> re-review`` (which lands here as a
        # reply event via _classify_reply, match_kind='re_review').
        if new_event_id is not None and _should_enqueue_investigation(event):
            _enqueue_investigation(
                change_event_id=new_event_id,
                user_id=user_id,
                delivery_id=delivery_id,
            )


def _update_delivery_status(
    delivery_id: str,
    status: str,
    error: str | None = None,
) -> None:
    """Update ``webhook_deliveries.status`` (and optionally ``error``).

    Defensive: never raises. A logging-table failure here must not crash
    the Celery task itself - the row update is a best-effort audit
    signal, not the source of truth for the webhook itself.
    """
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
                    # Truncate to keep the audit row compact; the full
                    # traceback is in the worker log.
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
            delivery_id,
            status,
            type(exc).__name__,
        )


def _extract_installation_block(payload: dict[str, Any]) -> tuple[int, dict[str, Any], str]:
    """Pull and validate the ``installation`` block common to both event families.

    Returns ``(installation_id, installation_dict, account_login)``. Raises
    ``ValueError`` with a short, log-safe label when the payload is missing
    the required ``installation.id`` field — the dispatcher converts this
    into a ``failed`` delivery row.
    """
    installation = payload.get("installation")
    if not isinstance(installation, dict):
        raise ValueError("payload missing 'installation' object")

    installation_id = installation.get("id")
    if not isinstance(installation_id, int):
        raise ValueError("payload missing 'installation.id' int")

    account = installation.get("account") or {}
    account_login = account.get("login") if isinstance(account, dict) else None
    return installation_id, installation, account_login or ""


def _handle_installation_event(
    payload: dict[str, Any],
    action: str | None,
    delivery_id: str,
) -> None:
    """Apply an ``installation.<action>`` webhook to ``github_installations``.

    Supported actions:
        * ``created``: UPSERT row from payload's ``installation`` block
          (key by ``installation_id``).
        * ``deleted``: DELETE row by ``installation_id``. The
          ``user_github_installations`` join is cleared by ON DELETE
          CASCADE. ``github_connected_repos.installation_id`` rows are
          intentionally NOT touched here — see Task 9 lazy cleanup in
          the auth router.
        * ``suspend``: ``UPDATE suspended_at = NOW()``.
        * ``unsuspend``: ``UPDATE suspended_at = NULL``.
        * ``new_permissions_accepted``: refresh ``permissions`` JSONB
          from payload AND clear ``permissions_pending_update``.

    The action AND ``webhook_deliveries.status='processed'`` happen in a
    single transaction. On exception, the dispatcher's outer ``except``
    marks the delivery ``failed`` (best-effort, separate connection)
    and re-raises so Celery retries.
    """
    from utils.db.connection_pool import db_pool

    start = time.monotonic()
    installation_id, installation, account_login = _extract_installation_block(payload)

    if action == "created":
        permissions_json = json.dumps(installation.get("permissions") or {})
        events_json = json.dumps(installation.get("events") or [])
        account = installation.get("account") or {}
        account_id = account.get("id") if isinstance(account, dict) else None
        account_type = (account.get("type") if isinstance(account, dict) else None) or "Organization"
        target_type = installation.get("target_type") or "Organization"
        repository_selection = installation.get("repository_selection") or "selected"

        if not isinstance(account_id, int):
            raise ValueError("installation.account.id missing or not int")

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO github_installations (
                           installation_id, account_login, account_id, account_type,
                           target_type, permissions, events, repository_selection,
                           suspended_at, permissions_pending_update
                       ) VALUES (
                           %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, NULL, FALSE
                       )
                       ON CONFLICT (installation_id) DO UPDATE SET
                           account_login = EXCLUDED.account_login,
                           account_id = EXCLUDED.account_id,
                           account_type = EXCLUDED.account_type,
                           target_type = EXCLUDED.target_type,
                           permissions = EXCLUDED.permissions,
                           events = EXCLUDED.events,
                           repository_selection = EXCLUDED.repository_selection,
                           updated_at = NOW()
                    """,
                    (
                        installation_id,
                        account_login,
                        account_id,
                        account_type,
                        target_type,
                        permissions_json,
                        events_json,
                        repository_selection,
                    ),
                )
                cur.execute(
                    """UPDATE webhook_deliveries
                       SET status = 'processed', processed_at = NOW()
                       WHERE delivery_id = %s""",
                    (delivery_id,),
                )
            conn.commit()
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            "gh_webhook_handler=installation action=created installation_id=%s "
            "account_login=%s delivery_id=%s status=processed duration_ms=%d",
            installation_id,
            account_login,
            delivery_id,
            duration_ms,
        )
        return

    if action == "deleted":
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                # Drop the parent row first. ``user_github_installations``
                # has ``ON DELETE CASCADE`` so the user-link rows go with
                # it. ``github_connected_repos.installation_id`` is a plain
                # column with no FK, so we null it explicitly below to
                # avoid leaving dangling references that would surface as
                # "App-bound repo with no install" in the picker.
                cur.execute(
                    "DELETE FROM github_installations WHERE installation_id = %s",
                    (installation_id,),
                )
                rows_deleted = cur.rowcount
                cur.execute(
                    """UPDATE github_connected_repos
                          SET installation_id = NULL,
                              updated_at = NOW()
                        WHERE installation_id = %s""",
                    (installation_id,),
                )
                connected_repos_unbound = cur.rowcount
                cur.execute(
                    """UPDATE webhook_deliveries
                       SET status = 'processed', processed_at = NOW()
                       WHERE delivery_id = %s""",
                    (delivery_id,),
                )
            conn.commit()
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            "gh_webhook_handler=installation action=deleted installation_id=%s "
            "account_login=%s delivery_id=%s status=processed "
            "rows_deleted=%s connected_repos_unbound=%s duration_ms=%d",
            installation_id,
            account_login,
            delivery_id,
            rows_deleted,
            connected_repos_unbound,
            duration_ms,
        )
        return

    if action in ("suspend", "unsuspend"):
        # GitHub sometimes sends `suspended` instead of `suspend`; we accept
        # the canonical form documented in the webhook reference.
        if action == "suspend":
            sql = (
                "UPDATE github_installations "
                "SET suspended_at = NOW(), updated_at = NOW() "
                "WHERE installation_id = %s"
            )
        else:
            sql = (
                "UPDATE github_installations "
                "SET suspended_at = NULL, updated_at = NOW() "
                "WHERE installation_id = %s"
            )

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (installation_id,))
                rows_updated = cur.rowcount
                cur.execute(
                    """UPDATE webhook_deliveries
                       SET status = 'processed', processed_at = NOW()
                       WHERE delivery_id = %s""",
                    (delivery_id,),
                )
            conn.commit()
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            "gh_webhook_handler=installation action=%s installation_id=%s "
            "account_login=%s delivery_id=%s status=processed "
            "rows_updated=%s duration_ms=%d",
            action,
            installation_id,
            account_login,
            delivery_id,
            rows_updated,
            duration_ms,
        )
        return

    if action == "new_permissions_accepted":
        permissions_json = json.dumps(installation.get("permissions") or {})
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE github_installations
                       SET permissions = %s::jsonb,
                           permissions_pending_update = FALSE,
                           updated_at = NOW()
                       WHERE installation_id = %s""",
                    (permissions_json, installation_id),
                )
                rows_updated = cur.rowcount
                cur.execute(
                    """UPDATE webhook_deliveries
                       SET status = 'processed', processed_at = NOW()
                       WHERE delivery_id = %s""",
                    (delivery_id,),
                )
            conn.commit()
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            "gh_webhook_handler=installation action=new_permissions_accepted "
            "installation_id=%s account_login=%s delivery_id=%s status=processed "
            "rows_updated=%s duration_ms=%d",
            installation_id,
            account_login,
            delivery_id,
            rows_updated,
            duration_ms,
        )
        return

    # Unknown action (e.g. future ``request`` action). Acknowledge so
    # GitHub stops retrying; record processed in the audit trail.
    duration_ms = int((time.monotonic() - start) * 1000)
    logger.info(
        "gh_webhook_handler=installation action=%s installation_id=%s "
        "account_login=%s delivery_id=%s status=ignored_unknown_action "
        "duration_ms=%d",
        action,
        installation_id,
        account_login,
        delivery_id,
        duration_ms,
    )
    _update_delivery_status(delivery_id, status="processed")


def _handle_installation_repositories_event(
    payload: dict[str, Any],
    action: str | None,
    delivery_id: str,
) -> None:
    """Apply an ``installation_repositories.<action>`` webhook.

    Supported actions:
        * ``added``: **NO-OP**. Aurora populates ``github_connected_repos``
          lazily when a user fetches their installation's repos via the
          auth router (Task 9). Eagerly inserting per-user rows here
          would require iterating ``user_github_installations`` for every
          linked user, which is wasteful for installations with no
          active Aurora users yet.
        * ``removed``: DELETE matching rows from
          ``github_connected_repos`` for ALL users that have this
          ``(installation_id, repo_full_name)`` pair. Multi-user
          installations exist; we must clean every user's view.

    Both branches mark ``webhook_deliveries.status='processed'`` (the
    ``removed`` path does so inside the same DB transaction as the
    DELETE). Exceptions propagate to the dispatcher for Celery retry.
    """
    from utils.db.connection_pool import db_pool

    start = time.monotonic()
    installation_id, _installation, account_login = _extract_installation_block(payload)

    if action == "added":
        # Lazy population: count for log breadcrumb only, no DB writes.
        repositories_added = payload.get("repositories_added")
        repo_count = len(repositories_added) if isinstance(repositories_added, list) else 0
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            "gh_webhook_handler=installation_repositories action=added "
            "installation_id=%s account_login=%s delivery_id=%s "
            "status=noop_lazy_population repo_count=%s duration_ms=%d",
            installation_id,
            account_login,
            delivery_id,
            repo_count,
            duration_ms,
        )
        _update_delivery_status(delivery_id, status="processed")
        return

    if action == "removed":
        repositories_removed = payload.get("repositories_removed")
        repo_full_names: list[str] = []
        if isinstance(repositories_removed, list):
            for repo in repositories_removed:
                if isinstance(repo, dict):
                    full_name = repo.get("full_name")
                    if isinstance(full_name, str) and full_name:
                        repo_full_names.append(full_name)

        # github_connected_repos is RLS-protected; the Celery worker has
        # no Flask request context so RLS vars are unset by default. Set
        # the RLS context per-user before DELETE so FORCE RLS doesn't
        # silently no-op the cleanup. An installation can be linked by
        # multiple users (different orgs), so we iterate over the join
        # table and reset context for each.
        from utils.auth.stateless_auth import set_rls_context

        rows_deleted = 0
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                if repo_full_names:
                    cur.execute(
                        """SELECT user_id
                             FROM user_github_installations
                            WHERE installation_id = %s""",
                        (installation_id,),
                    )
                    linked_users = [row[0] for row in cur.fetchall() if row[0]]

                    for linked_user_id in linked_users:
                        if not set_rls_context(
                            cur,
                            conn,
                            linked_user_id,
                            log_prefix="[gh_webhook:installation_repositories]",
                        ):
                            logger.warning(
                                "gh_webhook_handler=installation_repositories action=removed "
                                "installation_id=%s user=%s status=skipped_no_org_context",
                                installation_id,
                                linked_user_id,
                            )
                            continue
                        cur.execute(
                            """DELETE FROM github_connected_repos
                                WHERE installation_id = %s
                                  AND repo_full_name = ANY(%s)""",
                            (installation_id, repo_full_names),
                        )
                        rows_deleted += cur.rowcount
                # Reset RLS for the audit-row update; webhook_deliveries
                # is not RLS-protected but leaving stale per-user vars on
                # the connection just hides bugs in adjacent code.
                cur.execute(
                    "RESET myapp.current_user_id; RESET myapp.current_org_id;"
                )
                cur.execute(
                    """UPDATE webhook_deliveries
                       SET status = 'processed', processed_at = NOW()
                       WHERE delivery_id = %s""",
                    (delivery_id,),
                )
            conn.commit()
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            "gh_webhook_handler=installation_repositories action=removed "
            "installation_id=%s account_login=%s delivery_id=%s status=processed "
            "repo_count=%s rows_deleted=%s duration_ms=%d",
            installation_id,
            account_login,
            delivery_id,
            len(repo_full_names),
            rows_deleted,
            duration_ms,
        )
        return

    # Unknown action - acknowledge to stop GitHub retries.
    duration_ms = int((time.monotonic() - start) * 1000)
    logger.info(
        "gh_webhook_handler=installation_repositories action=%s installation_id=%s "
        "account_login=%s delivery_id=%s status=ignored_unknown_action "
        "duration_ms=%d",
        action,
        installation_id,
        account_login,
        delivery_id,
        duration_ms,
    )
    _update_delivery_status(delivery_id, status="processed")


_MISSING_FIELD_LITERAL = "<missing>"


def _safe_get(payload: dict[str, Any], *keys: str) -> Any:
    """Walk a nested dict; return ``None`` if any key is absent or non-dict.

    Used by the Task 14 code-event handlers to extract deeply-nested
    payload fields (e.g. ``pull_request.head.sha``) without exception
    handling boilerplate at every call site. A missing path returns
    ``None``, which ``_fmt_field`` then renders as ``<missing>``.
    """
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _fmt_field(value: Any) -> str:
    """Render a payload field for ``key=value`` structured logs.

    ``None`` renders as the literal ``<missing>`` so ops can distinguish
    a present-but-falsy value from an absent field.

    All other values run through :func:`utils.log_sanitizer.sanitize` to
    strip C0/C1 control chars and Unicode line separators (PR titles and
    target_urls are user-controlled and would otherwise inject log
    lines), then have any internal whitespace collapsed to a single
    space so the ``key=value`` log format isn't broken by spaces inside
    a single field.
    """
    if value is None:
        return _MISSING_FIELD_LITERAL
    from utils.log_sanitizer import sanitize

    cleaned = sanitize(value).replace("\r", " ").replace("\n", " ")
    return " ".join(cleaned.split())


def _extract_installation_id(payload: dict[str, Any]) -> int | None:
    """Best-effort extraction of ``installation.id`` for log correlation.

    Code-event handlers (Task 14) include ``installation_id`` in their
    structured log line, but unlike the installation/installation_repositories
    handlers (Task 13) they do NOT depend on it being present — a webhook
    delivered via a non-App route still gets logged. Returns ``None`` if
    the field is missing or non-int.
    """
    value = _safe_get(payload, "installation", "id")
    return value if isinstance(value, int) else None


def _handle_pull_request_event(
    payload: dict[str, Any],
    action: str | None,
    delivery_id: str,
) -> None:
    """Log a ``pull_request.<action>`` webhook for RCA correlation.

    MVP: structured-log only (no DB write beyond ``webhook_deliveries``
    audit, no GitHub API call). Fields per the Task 14 spec:
    ``repo, pr_number, action, state, merged_at, head_sha, base_sha,
    author, title``.
    """
    repo = _safe_get(payload, "repository", "full_name")
    pr_number = _safe_get(payload, "pull_request", "number")
    state = _safe_get(payload, "pull_request", "state")
    merged_at = _safe_get(payload, "pull_request", "merged_at")
    head_sha = _safe_get(payload, "pull_request", "head", "sha")
    base_sha = _safe_get(payload, "pull_request", "base", "sha")
    author = _safe_get(payload, "pull_request", "user", "login")
    title = _safe_get(payload, "pull_request", "title")
    installation_id = _extract_installation_id(payload)

    logger.info(
        "event_type=pull_request repo=%s pr_number=%s action=%s state=%s "
        "merged_at=%s head_sha=%s base_sha=%s author=%s title=%s "
        "installation_id=%s delivery_id=%s status=processed",
        _fmt_field(repo),
        _fmt_field(pr_number),
        _fmt_field(action),
        _fmt_field(state),
        _fmt_field(merged_at),
        _fmt_field(head_sha),
        _fmt_field(base_sha),
        _fmt_field(author),
        _fmt_field(title),
        _fmt_field(installation_id),
        delivery_id,
    )

    # Change-Intercept persistence (Phase 1a, Part 1). Additive — the RCA
    # log above is independent of this and survives any failure here.
    _ingest_change_intercept_event(
        event_type="pull_request",
        payload=payload,
        delivery_id=delivery_id,
    )

    _update_delivery_status(delivery_id, status="processed")


def _handle_issue_comment_event(
    payload: dict[str, Any],
    action: str | None,
    delivery_id: str,
) -> None:
    """Log + persist an ``issue_comment.<action>`` webhook.

    The dispatcher subscribes to this event so the change-intercept
    adapter can capture top-level PR comments addressed to Aurora
    (via ``@<slug>`` mention). Non-PR Issues that happen to fire this
    event are filtered out by the adapter's ``is_reply_to_us``.

    Fields logged per spec: ``repo, issue_number, action, author,
    comment_id, in_reply_to_id`` — matches the existing structured-
    log shape for cross-reference with the RCA correlation lines.
    """
    repo = _safe_get(payload, "repository", "full_name")
    issue_number = _safe_get(payload, "issue", "number")
    is_pr = _safe_get(payload, "issue", "pull_request") is not None
    comment_id = _safe_get(payload, "comment", "id")
    author = _safe_get(payload, "comment", "user", "login")
    in_reply_to_id = _safe_get(payload, "comment", "in_reply_to_id")
    installation_id = _extract_installation_id(payload)

    logger.info(
        "event_type=issue_comment repo=%s issue_number=%s action=%s is_pr=%s "
        "author=%s comment_id=%s in_reply_to_id=%s installation_id=%s "
        "delivery_id=%s status=processed",
        _fmt_field(repo),
        _fmt_field(issue_number),
        _fmt_field(action),
        _fmt_field(is_pr),
        _fmt_field(author),
        _fmt_field(comment_id),
        _fmt_field(in_reply_to_id),
        _fmt_field(installation_id),
        delivery_id,
    )

    _ingest_change_intercept_event(
        event_type="issue_comment",
        payload=payload,
        delivery_id=delivery_id,
    )

    _update_delivery_status(delivery_id, status="processed")


def _handle_pull_request_review_comment_event(
    payload: dict[str, Any],
    action: str | None,
    delivery_id: str,
) -> None:
    """Log + persist a ``pull_request_review_comment.<action>`` webhook.

    These are inline-thread events — engineers replying directly to
    Aurora's per-hunk comments. The adapter's ``is_reply_to_us``
    matches on ``in_reply_to_id`` + bot self-filter.
    """
    repo = _safe_get(payload, "repository", "full_name")
    pr_number = _safe_get(payload, "pull_request", "number")
    comment_id = _safe_get(payload, "comment", "id")
    author = _safe_get(payload, "comment", "user", "login")
    in_reply_to_id = _safe_get(payload, "comment", "in_reply_to_id")
    commit_id = _safe_get(payload, "comment", "commit_id")
    installation_id = _extract_installation_id(payload)

    logger.info(
        "event_type=pull_request_review_comment repo=%s pr_number=%s action=%s "
        "author=%s comment_id=%s in_reply_to_id=%s commit_id=%s "
        "installation_id=%s delivery_id=%s status=processed",
        _fmt_field(repo),
        _fmt_field(pr_number),
        _fmt_field(action),
        _fmt_field(author),
        _fmt_field(comment_id),
        _fmt_field(in_reply_to_id),
        _fmt_field(commit_id),
        _fmt_field(installation_id),
        delivery_id,
    )

    _ingest_change_intercept_event(
        event_type="pull_request_review_comment",
        payload=payload,
        delivery_id=delivery_id,
    )

    _update_delivery_status(delivery_id, status="processed")


def _handle_issues_event(
    payload: dict[str, Any],
    action: str | None,
    delivery_id: str,
) -> None:
    """Log an ``issues.<action>`` webhook for incident-issue correlation.

    Fields per spec: ``repo, issue_number, action, state, author, title``.
    """
    repo = _safe_get(payload, "repository", "full_name")
    issue_number = _safe_get(payload, "issue", "number")
    state = _safe_get(payload, "issue", "state")
    author = _safe_get(payload, "issue", "user", "login")
    title = _safe_get(payload, "issue", "title")
    installation_id = _extract_installation_id(payload)

    logger.info(
        "event_type=issues repo=%s issue_number=%s action=%s state=%s "
        "author=%s title=%s installation_id=%s delivery_id=%s status=processed",
        _fmt_field(repo),
        _fmt_field(issue_number),
        _fmt_field(action),
        _fmt_field(state),
        _fmt_field(author),
        _fmt_field(title),
        _fmt_field(installation_id),
        delivery_id,
    )
    _update_delivery_status(delivery_id, status="processed")


def _handle_deployment_event(
    payload: dict[str, Any],
    action: str | None,
    delivery_id: str,
) -> None:
    """Log a ``deployment`` webhook for deploy-timeline correlation.

    Fields per spec: ``repo, deployment_id, environment, ref, sha, creator``.
    The ``action`` is logged for completeness even though most
    ``deployment`` events do not carry one.
    """
    repo = _safe_get(payload, "repository", "full_name")
    deployment_id = _safe_get(payload, "deployment", "id")
    environment = _safe_get(payload, "deployment", "environment")
    ref = _safe_get(payload, "deployment", "ref")
    sha = _safe_get(payload, "deployment", "sha")
    creator = _safe_get(payload, "deployment", "creator", "login")
    installation_id = _extract_installation_id(payload)

    logger.info(
        "event_type=deployment repo=%s deployment_id=%s action=%s environment=%s "
        "ref=%s sha=%s creator=%s installation_id=%s delivery_id=%s status=processed",
        _fmt_field(repo),
        _fmt_field(deployment_id),
        _fmt_field(action),
        _fmt_field(environment),
        _fmt_field(ref),
        _fmt_field(sha),
        _fmt_field(creator),
        _fmt_field(installation_id),
        delivery_id,
    )
    _update_delivery_status(delivery_id, status="processed")


def _handle_deployment_status_event(
    payload: dict[str, Any],
    action: str | None,
    delivery_id: str,
) -> None:
    """Log a ``deployment_status`` webhook.

    Fields per spec: ``repo, deployment_id, state, environment,
    target_url, creator``. ``environment`` is preferred from
    ``deployment_status`` then falls back to ``deployment`` (GitHub's
    payload places it on the status object for newer-style envs).
    """
    repo = _safe_get(payload, "repository", "full_name")
    deployment_id = _safe_get(payload, "deployment", "id")
    state = _safe_get(payload, "deployment_status", "state")
    environment = _safe_get(payload, "deployment_status", "environment")
    if environment is None:
        environment = _safe_get(payload, "deployment", "environment")
    target_url = _safe_get(payload, "deployment_status", "target_url")
    creator = _safe_get(payload, "deployment_status", "creator", "login")
    installation_id = _extract_installation_id(payload)

    logger.info(
        "event_type=deployment_status repo=%s deployment_id=%s action=%s state=%s "
        "environment=%s target_url=%s creator=%s installation_id=%s "
        "delivery_id=%s status=processed",
        _fmt_field(repo),
        _fmt_field(deployment_id),
        _fmt_field(action),
        _fmt_field(state),
        _fmt_field(environment),
        _fmt_field(target_url),
        _fmt_field(creator),
        _fmt_field(installation_id),
        delivery_id,
    )
    _update_delivery_status(delivery_id, status="processed")


def _handle_workflow_run_event(
    payload: dict[str, Any],
    action: str | None,
    delivery_id: str,
) -> None:
    """Log a ``workflow_run.<action>`` webhook for CI signal correlation.

    Fields per spec: ``repo, workflow_run_id, name, conclusion, head_sha,
    head_branch, run_attempt``.
    """
    repo = _safe_get(payload, "repository", "full_name")
    workflow_run_id = _safe_get(payload, "workflow_run", "id")
    name = _safe_get(payload, "workflow_run", "name")
    conclusion = _safe_get(payload, "workflow_run", "conclusion")
    head_sha = _safe_get(payload, "workflow_run", "head_sha")
    head_branch = _safe_get(payload, "workflow_run", "head_branch")
    run_attempt = _safe_get(payload, "workflow_run", "run_attempt")
    installation_id = _extract_installation_id(payload)

    logger.info(
        "event_type=workflow_run repo=%s workflow_run_id=%s action=%s name=%s "
        "conclusion=%s head_sha=%s head_branch=%s run_attempt=%s "
        "installation_id=%s delivery_id=%s status=processed",
        _fmt_field(repo),
        _fmt_field(workflow_run_id),
        _fmt_field(action),
        _fmt_field(name),
        _fmt_field(conclusion),
        _fmt_field(head_sha),
        _fmt_field(head_branch),
        _fmt_field(run_attempt),
        _fmt_field(installation_id),
        delivery_id,
    )
    _update_delivery_status(delivery_id, status="processed")


def _handle_check_run_event(
    payload: dict[str, Any],
    action: str | None,
    delivery_id: str,
) -> None:
    """Log a ``check_run.<action>`` webhook for CI status correlation.

    Fields per spec: ``repo, check_id, name, status, conclusion, head_sha``.
    """
    repo = _safe_get(payload, "repository", "full_name")
    check_id = _safe_get(payload, "check_run", "id")
    name = _safe_get(payload, "check_run", "name")
    status = _safe_get(payload, "check_run", "status")
    conclusion = _safe_get(payload, "check_run", "conclusion")
    head_sha = _safe_get(payload, "check_run", "head_sha")
    installation_id = _extract_installation_id(payload)

    logger.info(
        "event_type=check_run repo=%s check_id=%s action=%s name=%s status=%s "
        "conclusion=%s head_sha=%s installation_id=%s delivery_id=%s status=processed",
        _fmt_field(repo),
        _fmt_field(check_id),
        _fmt_field(action),
        _fmt_field(name),
        _fmt_field(status),
        _fmt_field(conclusion),
        _fmt_field(head_sha),
        _fmt_field(installation_id),
        delivery_id,
    )
    _update_delivery_status(delivery_id, status="processed")


def _handle_check_suite_event(
    payload: dict[str, Any],
    action: str | None,
    delivery_id: str,
) -> None:
    """Log a ``check_suite.<action>`` webhook for CI rollup correlation.

    Fields per spec: ``repo, check_id, name, status, conclusion, head_sha``.
    GitHub's ``check_suite`` payload exposes ``name`` only via the nested
    ``app`` block; missing/non-app suites render as ``<missing>``.
    """
    repo = _safe_get(payload, "repository", "full_name")
    check_id = _safe_get(payload, "check_suite", "id")
    name = _safe_get(payload, "check_suite", "app", "name")
    status = _safe_get(payload, "check_suite", "status")
    conclusion = _safe_get(payload, "check_suite", "conclusion")
    head_sha = _safe_get(payload, "check_suite", "head_sha")
    installation_id = _extract_installation_id(payload)

    logger.info(
        "event_type=check_suite repo=%s check_id=%s action=%s name=%s status=%s "
        "conclusion=%s head_sha=%s installation_id=%s delivery_id=%s status=processed",
        _fmt_field(repo),
        _fmt_field(check_id),
        _fmt_field(action),
        _fmt_field(name),
        _fmt_field(status),
        _fmt_field(conclusion),
        _fmt_field(head_sha),
        _fmt_field(installation_id),
        delivery_id,
    )
    _update_delivery_status(delivery_id, status="processed")


@celery_app.task(
    name="tasks.github_webhook_tasks.dispatch_github_webhook",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def dispatch_github_webhook(
    self,
    delivery_id: str,
    event_type: str,
    payload_json_str: str,
) -> None:
    """Route a GitHub App webhook delivery to the correct event handler.

    Args:
        delivery_id: ``X-GitHub-Delivery`` UUID. Used as the dedupe key
            in ``webhook_deliveries`` and as the correlation id in logs.
        event_type: ``X-GitHub-Event`` value (e.g. ``installation``,
            ``pull_request``). See the Handler Matrix in the module
            docstring for the full list.
        payload_json_str: Raw JSON body as a string. We re-parse here so
            handlers can index into it; callers must NOT pre-parse and
            pass a dict (Celery's JSON serializer round-trip can mangle
            nested values, and we want to mirror what GitHub sent).

    Behavior:
        * Routes to per-event handler per the module-level Handler Matrix.
        * Any unsubscribed/unknown event → WARNING ``unknown_event`` log
          + ``status=processed`` (never error: GitHub treats non-2xx as
          a retry signal and we want the unsubscribed event to drop).
        * Any handler exception → mark ``failed`` with the exception
          class name and re-raise via ``self.retry`` so Celery applies
          its retry policy.
    """
    start = time.monotonic()
    logger.info(
        "gh_webhook_handler=dispatch event_type=%s delivery_id=%s status=received",
        event_type,
        delivery_id,
    )

    try:
        try:
            payload = json.loads(payload_json_str)
        except json.JSONDecodeError as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            logger.error(
                "gh_webhook_handler=dispatch event_type=%s delivery_id=%s "
                "status=failed duration_ms=%d error_class=%s reason=invalid_json",
                event_type,
                delivery_id,
                duration_ms,
                type(exc).__name__,
            )
            _update_delivery_status(
                delivery_id,
                status="failed",
                error=f"invalid_json: {type(exc).__name__}",
            )
            return

        if not isinstance(payload, dict):
            duration_ms = int((time.monotonic() - start) * 1000)
            logger.warning(
                "gh_webhook_handler=dispatch event_type=%s delivery_id=%s "
                "status=processed duration_ms=%d reason=payload_not_object",
                event_type,
                delivery_id,
                duration_ms,
            )
            _update_delivery_status(delivery_id, status="processed")
            return

        action = payload.get("action") if isinstance(payload.get("action"), str) else None

        handlers = {
            "installation": _handle_installation_event,
            "installation_repositories": _handle_installation_repositories_event,
            "pull_request": _handle_pull_request_event,
            # Comment events subscribed for change-intercept reply handling.
            # The adapter's ``is_reply_to_us`` decides whether a comment is
            # addressed to Aurora; unrelated PR chatter is filtered out
            # there rather than at the dispatcher level.
            "issue_comment": _handle_issue_comment_event,
            "pull_request_review_comment": _handle_pull_request_review_comment_event,
            "issues": _handle_issues_event,
            "deployment": _handle_deployment_event,
            "deployment_status": _handle_deployment_status_event,
            "workflow_run": _handle_workflow_run_event,
            "check_run": _handle_check_run_event,
            "check_suite": _handle_check_suite_event,
        }
        handler = handlers.get(event_type)
        if handler is not None:
            handler(payload, action, delivery_id)
        else:
            # Unhandled event type (push/release are intentionally excluded;
            # anything else is an unexpected subscription). Acknowledge so
            # GitHub stops retrying; log as a breadcrumb for ops.
            duration_ms = int((time.monotonic() - start) * 1000)
            logger.info(
                "gh_webhook_handler=%s action=%s delivery_id=%s "
                "status=no_handler duration_ms=%d",
                event_type,
                action if action else "-",
                delivery_id,
                duration_ms,
            )
            _update_delivery_status(delivery_id, status="processed")
    except Exception as exc:
        # ``redact_token`` covers any token-shaped substring that an
        # exception message could echo back from a misbehaving handler
        # (e.g. a downstream HTTP call that surfaces token in the body).
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.exception(
            "gh_webhook_handler=dispatch event_type=%s delivery_id=%s "
            "status=failed duration_ms=%d error_class=%s msg=%s",
            event_type,
            delivery_id,
            duration_ms,
            type(exc).__name__,
            redact_token(str(exc)),
        )
        _update_delivery_status(
            delivery_id,
            status="failed",
            error=type(exc).__name__,
        )
        raise self.retry(exc=exc)
