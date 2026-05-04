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
| issues                         | ``_handle_issues_event``                         |
| deployment                     | ``_handle_deployment_event``                     |
| deployment_status              | ``_handle_deployment_status_event``              |
| workflow_run                   | ``_handle_workflow_run_event``                   |
| check_run                      | ``_handle_check_run_event``                      |
| check_suite                    | ``_handle_check_suite_event``                    |
| <anything else>                | WARNING ``unknown_event`` + ``status=processed`` |
+--------------------------------+--------------------------------------------------+

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
                cur.execute(
                    "DELETE FROM github_installations WHERE installation_id = %s",
                    (installation_id,),
                )
                rows_deleted = cur.rowcount
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
            "rows_deleted=%s duration_ms=%d",
            installation_id,
            account_login,
            delivery_id,
            rows_deleted,
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

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                rows_deleted = 0
                if repo_full_names:
                    cur.execute(
                        """DELETE FROM github_connected_repos
                           WHERE installation_id = %s
                             AND repo_full_name = ANY(%s)""",
                        (installation_id, repo_full_names),
                    )
                    rows_deleted = cur.rowcount
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
    a present-but-falsy value from an absent field. All other values are
    coerced via ``str()``; we deliberately do NOT quote-wrap so the log
    line stays grep-friendly with the rest of the file's key=value style.
    """
    if value is None:
        return _MISSING_FIELD_LITERAL
    return str(value)


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
    """Log a ``pull_request.<action>`` webhook and dispatch to change-intercept.

    MVP logging fields per spec: ``repo, pr_number, action, state,
    merged_at, head_sha, base_sha, author, title``.

    PR actions ``opened``, ``synchronize``, and ``reopened`` are routed
    to the change-intercept pipeline for risk assessment.
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
    _update_delivery_status(delivery_id, status="processed")

    if action in ("opened", "synchronize", "reopened") and installation_id:
        _dispatch_change_intercept(payload, installation_id)


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


def _dispatch_change_intercept(
    payload: dict[str, Any],
    installation_id: int,
) -> None:
    """Resolve org_id and enqueue a change-intercept investigation."""
    from services.change_intercept.adapters.github import GitHubAdapter

    org_id = GitHubAdapter._resolve_org_id(installation_id)

    from tasks.change_intercept_tasks import process_change_event
    process_change_event.delay("github", payload, org_id, installation_id)


class _WebhookRequest:
    """Minimal request-like object for adapter.is_reply_to_us."""

    def __init__(self, event_type: str, payload: dict[str, Any]):
        self.headers = {"X-GitHub-Event": event_type}
        self._json = payload

    def get_json(self, silent: bool = False) -> dict:
        return self._json


def _try_dispatch_reply(
    event_type: str,
    payload: dict[str, Any],
    installation_id: int,
) -> None:
    """Check if a comment event is a reply to Aurora and dispatch if so."""
    from services.change_intercept.adapters.github import GitHubAdapter

    adapter = GitHubAdapter()
    reply = adapter.is_reply_to_us(_WebhookRequest(event_type, payload))
    if reply:
        org_id = GitHubAdapter._resolve_org_id(installation_id)
        from tasks.change_intercept_tasks import launch_followup_investigation
        launch_followup_investigation.delay(
            reply.original_event_dedup_key, reply.reply_body, org_id,
        )


def _handle_issue_comment_event(
    payload: dict[str, Any],
    action: str | None,
    delivery_id: str,
) -> None:
    """Route ``issue_comment`` events -- detect replies to Aurora reviews."""
    installation_id = _extract_installation_id(payload)

    logger.info(
        "event_type=issue_comment action=%s installation_id=%s "
        "delivery_id=%s status=processed",
        _fmt_field(action),
        _fmt_field(installation_id),
        delivery_id,
    )
    _update_delivery_status(delivery_id, status="processed")

    if action == "created" and installation_id:
        _try_dispatch_reply("issue_comment", payload, installation_id)


def _handle_pr_review_comment_event(
    payload: dict[str, Any],
    action: str | None,
    delivery_id: str,
) -> None:
    """Route ``pull_request_review_comment`` events -- detect threaded replies."""
    installation_id = _extract_installation_id(payload)

    logger.info(
        "event_type=pull_request_review_comment action=%s installation_id=%s "
        "delivery_id=%s status=processed",
        _fmt_field(action),
        _fmt_field(installation_id),
        delivery_id,
    )
    _update_delivery_status(delivery_id, status="processed")

    if action == "created" and installation_id:
        _try_dispatch_reply("pull_request_review_comment", payload, installation_id)


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

        if event_type == "installation":
            _handle_installation_event(payload, action, delivery_id)
        elif event_type == "installation_repositories":
            _handle_installation_repositories_event(payload, action, delivery_id)
        elif event_type == "pull_request":
            _handle_pull_request_event(payload, action, delivery_id)
        elif event_type == "issues":
            _handle_issues_event(payload, action, delivery_id)
        elif event_type == "deployment":
            _handle_deployment_event(payload, action, delivery_id)
        elif event_type == "deployment_status":
            _handle_deployment_status_event(payload, action, delivery_id)
        elif event_type == "workflow_run":
            _handle_workflow_run_event(payload, action, delivery_id)
        elif event_type == "check_run":
            _handle_check_run_event(payload, action, delivery_id)
        elif event_type == "check_suite":
            _handle_check_suite_event(payload, action, delivery_id)
        elif event_type == "issue_comment":
            _handle_issue_comment_event(payload, action, delivery_id)
        elif event_type == "pull_request_review_comment":
            _handle_pr_review_comment_event(payload, action, delivery_id)
        else:
            # Unsubscribed / future event type. Acknowledge so GitHub
            # stops retrying; log as a breadcrumb for ops.
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
