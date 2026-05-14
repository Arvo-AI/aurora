"""Celery tasks for the change-intercept pipeline.

Phase 1a Part 2 introduces a single task: :func:`launch_investigation`.
It is enqueued by the webhook dispatcher after a ``change_events`` row
is persisted for an actionable PR event (opened / reopened /
ready_for_review). Per the resolved open question on token spend,
``synchronize`` events are persisted but NOT enqueued — we don't burn
LLM tokens on every push.

What the task does:

    1. Load the ``change_events`` row by id (with RLS context set
       from the org_id stored on the row).
    2. Render the prompt via
       :mod:`services.change_intercept.prompts`.
    3. Call the investigator
       (:func:`services.change_intercept.investigator.invoke`).
    4. Validate the output
       (:func:`services.change_intercept.verdict_validator.validate`).
    5. Persist a ``change_investigations`` row with ``dry_run=TRUE``
       (Part 3 will flip this on a per-install basis).
    6. Render the review via
       :mod:`services.change_intercept.pr_review_poster` — for Part 2
       this is only logged for calibration; Part 3 calls the adapter's
       ``post_verdict`` to actually post.

Failure modes:

    - Bad/missing change_events row → log + return (don't retry — the
      row would have to materialise out of thin air).
    - Investigator returns ok=False → persist a dry-run row with the
      validator's safe-default (verdict='approve', no findings, drop
      reason in the log).
    - Validator drops every finding → persist with whatever survives
      (often verdict='approve' after reconciliation).
    - Database failure → log + Celery retry per the standard policy.

The task name (``services.change_intercept.tasks.launch_investigation``)
matches the convention used elsewhere in the codebase. Register the
module in ``celery_config.py`` so the worker can find it.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from celery_config import celery_app
from utils.auth.log_redact import redact_token

logger = logging.getLogger(__name__)


# Thrash guard: max followup investigations Aurora will run per PR
# before refusing to re-investigate until a new commit lands. Engineer
# replies past this cap get a static one-time response from the
# adapter (in live mode) and are logged as ``status=thrash_guard``
# in dry-run mode. Per the design doc, start at 5 and revisit after
# calibration data shows how often genuine multi-round conversations
# happen vs. dispute spirals.
MAX_FOLLOWUPS_PER_CHANGE: int = 5


# Subset of fields the task reads from ``change_events`` — keeps the
# query specific and the worker memory profile predictable.
_CHANGE_EVENT_COLUMNS: tuple[str, ...] = (
    "id",
    "org_id",
    "vendor",
    "kind",
    "external_id",
    "dedup_key",
    "installation_id",
    "repo",
    "ref",
    "base_ref",
    "commit_sha",
    "actor",
    "target_env",
    "change_body",
    "change_diff",
    "change_files",
    "change_commits",
    "follow_up_comment",
    "parent_event_id",
)


@celery_app.task(
    name="services.change_intercept.tasks.link_risk_outcomes",
    bind=True,
    max_retries=2,
    default_retry_delay=300,
)
def link_risk_outcomes(self, *, lookback_hours: int = 36) -> dict[str, Any]:
    """Nightly task: scan recent incidents and link them to the
    ``change_investigations`` rows whose PR they reference.

    Thin Celery wrapper around :func:`services.change_intercept.linker.run_linker`.
    Keeping the regex + DB logic in a pure module means tests can
    import the matcher without bootstrapping the full Celery / Vault
    stack.
    """
    try:
        from services.change_intercept.linker import run_linker

        return run_linker(lookback_hours=lookback_hours)
    except Exception as exc:
        logger.exception(
            "change_intercept_linker=task_failed lookback_hours=%d error_class=%s",
            lookback_hours,
            type(exc).__name__,
        )
        raise self.retry(exc=exc)


@celery_app.task(
    name="services.change_intercept.tasks.launch_investigation",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    queue="change_intercept",
)
def launch_investigation(
    self,
    change_event_id: str,
    *,
    user_id_for_rls: str,
) -> dict[str, Any]:
    """Run an investigation against ``change_event_id`` in dry-run mode.

    Phase 1a Part 2 always persists with ``dry_run=TRUE``. The Part 3
    rollout adds a per-install ``dry_run`` flag on
    ``github_app_installations`` that, when ``FALSE``, also calls
    the adapter's ``post_verdict``.

    Args:
        change_event_id: UUID of the ``change_events`` row to
            investigate.
        user_id_for_rls: any active user from the event's org. Used
            to set the RLS context for the DB reads + writes.

    Returns:
        A small status dict for Celery introspection. Real output
        lives in the ``change_investigations`` row.
    """
    start = time.monotonic()
    logger.info(
        "change_intercept_event=launch_investigation status=received "
        "change_event_id=%s",
        change_event_id,
    )

    try:
        # ─── 0. Per-event try-lock to dedup parallel runs ─────────
        # A Celery retry can race with a manual re-enqueue (or two
        # webhook deliveries within the dedup window) for the SAME
        # change_event_id. Without serialization both workers would
        # call the LLM, INSERT two change_investigations rows, and
        # post two reviews. pg_try_advisory_lock returns FALSE if the
        # lock is already held; the second worker just exits cleanly.
        event_lock_token = _event_lock_token(change_event_id)
        with _try_advisory_lock(event_lock_token) as got_lock:
            if not got_lock:
                logger.info(
                    "change_intercept_event=launch_investigation status=already_in_flight "
                    "change_event_id=%s",
                    change_event_id,
                )
                return {"status": "already_in_flight"}
            return _launch_investigation_inner(
                change_event_id=change_event_id,
                user_id_for_rls=user_id_for_rls,
                start=start,
            )
    except Exception as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.exception(
            "change_intercept_event=launch_investigation status=failed "
            "change_event_id=%s duration_ms=%d error_class=%s msg=%s",
            change_event_id,
            duration_ms,
            type(exc).__name__,
            redact_token(str(exc)),
        )
        raise self.retry(exc=exc)


def _launch_investigation_inner(
    *,
    change_event_id: str,
    user_id_for_rls: str,
    start: float,
) -> dict[str, Any]:
    """The actual work, separated from the outer try-lock so the lock
    context manager owns the retry/cleanup semantics."""
    try:
        # ─── 1. Load the change_events row ───────────────────────
        event_row = _load_change_event(change_event_id, user_id_for_rls)
        if event_row is None:
            logger.warning(
                "change_intercept_event=launch_investigation status=event_not_found "
                "change_event_id=%s",
                change_event_id,
            )
            return {"status": "event_not_found"}

        # ─── 1b. Thrash guard (followups only) ───────────────────
        if event_row.get("kind") == "code_change_followup":
            followup_count = _count_followup_investigations(
                org_id=event_row["org_id"],
                dedup_key=event_row.get("dedup_key") or "",
                user_id_for_rls=user_id_for_rls,
            )
            if followup_count >= MAX_FOLLOWUPS_PER_CHANGE:
                logger.info(
                    "change_intercept_event=launch_investigation status=thrash_guard "
                    "change_event_id=%s dedup_key=%s followup_count=%d cap=%d",
                    change_event_id,
                    event_row.get("dedup_key"),
                    followup_count,
                    MAX_FOLLOWUPS_PER_CHANGE,
                )
                return {
                    "status": "thrash_guard",
                    "followup_count": followup_count,
                    "cap": MAX_FOLLOWUPS_PER_CHANGE,
                }

        # ─── 2. Build the prompt ─────────────────────────────────
        prompt, prior_investigation_id = _build_prompt_for_event(
            event_row, user_id_for_rls
        )
        if not prompt:
            logger.warning(
                "change_intercept_event=launch_investigation status=prompt_unavailable "
                "change_event_id=%s",
                change_event_id,
            )
            return {"status": "prompt_unavailable"}

        # ─── 3. Call investigator ────────────────────────────────
        from services.change_intercept.investigator import invoke

        inv_result = invoke(prompt)

        # ─── 4. Validate output ──────────────────────────────────
        from services.change_intercept.verdict_validator import validate

        validation = validate(
            inv_result.parsed, diff_text=event_row.get("change_diff") or ""
        )

        # ─── 5. Render review (for logging / calibration only) ───
        from services.change_intercept.pr_review_poster import render_review

        rendered = render_review(validation)

        # ─── 5b. Resolve dry_run BEFORE persistence so the column ────
        # matches the actual posting outcome. Doing this here instead
        # of after _persist_investigation prevents the bug where a
        # live-posted investigation would still record dry_run=TRUE
        # and skew calibration analytics.
        from services.change_intercept.install_config import is_dry_run as _is_dry_run

        resolved_installation_id = _resolve_installation_id(
            event_row, user_id_for_rls
        )
        will_dry_run = (
            True
            if resolved_installation_id is None
            else _is_dry_run(resolved_installation_id)
        )

        # ─── 6. Persist change_investigations row ────────────────
        investigation_id = _persist_investigation(
            event_row=event_row,
            validation=validation,
            inv_result=inv_result,
            user_id_for_rls=user_id_for_rls,
            parent_investigation_id=prior_investigation_id,
            dry_run=will_dry_run,
        )

        # ─── 7. Optionally post the live review (Part 3) ─────────
        live_review = _maybe_post_live_review(
            event_row=event_row,
            validation=validation,
            rendered=rendered,
            prior_investigation_id=prior_investigation_id,
            investigation_id=investigation_id,
            user_id_for_rls=user_id_for_rls,
            dry_run=will_dry_run,
            installation_id=resolved_installation_id,
        )

        duration_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            "change_intercept_event=launch_investigation status=done "
            "change_event_id=%s investigation_id=%s verdict=%s "
            "findings=%d dropped=%d inline=%d duration_ms=%d "
            "downgraded=%s dry_run=%s posted=%s",
            change_event_id,
            investigation_id,
            validation.verdict,
            len(validation.findings),
            len(validation.dropped),
            sum(1 for f in validation.findings if f.will_post_inline),
            duration_ms,
            validation.downgraded_to_approve,
            live_review["dry_run"],
            live_review["posted"],
        )

        return {
            "status": "ok",
            "investigation_id": investigation_id,
            "verdict": validation.verdict,
            "findings_count": len(validation.findings),
            "inline_count": sum(1 for f in validation.findings if f.will_post_inline),
            "downgraded": validation.downgraded_to_approve,
            "dry_run": live_review["dry_run"],
            "review_event": rendered.verdict_event,
            "posted_verdict_id": live_review["verdict_id"],
        }
    except Exception:
        # Re-raise to the outer ``launch_investigation`` which holds
        # ``self`` for Celery retry. Logging is done in the outer
        # except handler so we don't double-log on the way up.
        raise


# ─── Per-event try-lock helpers ─────────────────────────────────────


def _event_lock_token(change_event_id: str) -> int:
    """Stable 63-bit advisory-lock token derived from change_event_id."""
    import hashlib

    digest = hashlib.blake2b(
        f"event:{change_event_id}".encode("utf-8"), digest_size=8
    ).digest()
    token = int.from_bytes(digest, "big") & 0x7FFFFFFFFFFFFFFF
    return token or 1


class _try_advisory_lock:
    """Context manager wrapping ``pg_try_advisory_lock``.

    Yields ``True`` on entry when the lock was acquired, ``False`` when
    another worker already holds it. Always releases on exit (and
    always returns the connection to the pool). Used by
    ``launch_investigation`` to dedup parallel runs for the same
    change_event_id without blocking the second worker.
    """

    def __init__(self, token: int) -> None:
        self._token = token
        self._conn: Any = None
        self._conn_cm: Any = None
        self._got: bool = False

    def __enter__(self) -> bool:
        from utils.db.connection_pool import db_pool

        self._conn_cm = db_pool.get_admin_connection()
        self._conn = self._conn_cm.__enter__()
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_try_advisory_lock(%s);", (self._token,)
                )
                self._got = bool(cur.fetchone()[0])
        except BaseException:
            try:
                self._conn_cm.__exit__(None, None, None)
            finally:
                self._conn = None
            raise
        return self._got

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._got and self._conn is not None:
                with self._conn.cursor() as cur:
                    cur.execute(
                        "SELECT pg_advisory_unlock(%s);", (self._token,)
                    )
        finally:
            if self._conn_cm is not None:
                self._conn_cm.__exit__(exc_type, exc, tb)
                self._conn = None
                self._conn_cm = None


# ─── DB I/O ──────────────────────────────────────────────────────────


def _load_change_event(
    change_event_id: str, user_id_for_rls: str
) -> dict[str, Any] | None:
    """SELECT a single ``change_events`` row by id under RLS context.

    Returns a dict keyed by column name, or ``None`` if the row
    doesn't exist (or RLS hid it from us).
    """
    from utils.auth.stateless_auth import set_rls_context
    from utils.db.connection_pool import db_pool

    columns_sql = ", ".join(_CHANGE_EVENT_COLUMNS)
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            if not set_rls_context(
                cur, conn, user_id_for_rls, log_prefix="[change_intercept:load]"
            ):
                return None
            cur.execute(
                f"SELECT {columns_sql} FROM change_events WHERE id = %s",  # noqa: S608 — columns are static
                (change_event_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return dict(zip(_CHANGE_EVENT_COLUMNS, row))


def _load_prior_investigation(
    org_id: str, dedup_key: str, user_id_for_rls: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Find the most recent investigation for ``(org_id, dedup_key)``.

    Used for follow-up prompts to render the prior verdict + findings.
    Returns ``(investigation_dict_or_None, investigation_id_or_None)``.
    """
    from utils.auth.stateless_auth import set_rls_context
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            if not set_rls_context(
                cur,
                conn,
                user_id_for_rls,
                log_prefix="[change_intercept:prior]",
            ):
                return None, None
            cur.execute(
                """SELECT ci.id, ci.verdict, ci.summary, ci.findings
                     FROM change_investigations ci
                     JOIN change_events ce ON ce.id = ci.change_event_id
                    WHERE ce.org_id = %s
                      AND ce.dedup_key = %s
                      AND ce.kind = 'code_change'
                 ORDER BY ci.investigated_at DESC
                    LIMIT 1""",
                (org_id, dedup_key),
            )
            row = cur.fetchone()
            if row is None:
                return None, None
            investigation_id, verdict, summary, findings = row
            findings_list = findings if isinstance(findings, list) else []
            return {
                "verdict": verdict,
                "summary": summary,
                "findings": findings_list,
            }, str(investigation_id)


def _count_followup_investigations(
    *,
    org_id: str,
    dedup_key: str,
    user_id_for_rls: str,
) -> int:
    """Count followup investigations Aurora has already run for this PR.

    A "followup" is any ``change_investigations`` row whose joined
    ``change_events.kind == 'code_change_followup'``. The thrash guard
    uses this to cap engagement when an engineer keeps replying past
    the point where Aurora has anything new to say.

    Returns 0 on lookup failure — fail-open here is safer than
    accidentally double-blocking on a transient DB blip.
    """
    from utils.auth.stateless_auth import set_rls_context
    from utils.db.connection_pool import db_pool

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                if not set_rls_context(
                    cur,
                    conn,
                    user_id_for_rls,
                    log_prefix="[change_intercept:thrash]",
                ):
                    return 0
                cur.execute(
                    """SELECT COUNT(*)
                         FROM change_investigations ci
                         JOIN change_events ce ON ce.id = ci.change_event_id
                        WHERE ce.org_id = %s
                          AND ce.dedup_key = %s
                          AND ce.kind = 'code_change_followup'""",
                    (org_id, dedup_key),
                )
                row = cur.fetchone()
                return int(row[0]) if row else 0
    except Exception as exc:
        # Fail-closed: a DB blip during the count must NOT silently
        # disable the guard. Returning the cap is treated by the
        # caller as "skip investigation"; safer than letting an
        # uncapped engineer-reply stream burn LLM tokens.
        logger.warning(
            "change_intercept_event=thrash_count_failed_fail_closed dedup_key=%s "
            "error_class=%s",
            dedup_key,
            type(exc).__name__,
        )
        return MAX_FOLLOWUPS_PER_CHANGE


def _persist_investigation(
    *,
    event_row: dict[str, Any],
    validation: Any,  # ValidationResult; loose-typed to avoid the import cycle
    inv_result: Any,  # InvestigatorResult
    user_id_for_rls: str,
    parent_investigation_id: str | None,
    dry_run: bool = True,
) -> str:
    """INSERT a ``change_investigations`` row under RLS context.

    The ``dry_run`` argument must reflect the live-posting decision
    made for THIS investigation — pass ``True`` when no review will
    actually post to GitHub, ``False`` when it will. Defaults to
    ``True`` (safe default) for callers that haven't been updated.
    Returns the new row's UUID as a string.
    """
    from utils.auth.stateless_auth import set_rls_context
    from utils.db.connection_pool import db_pool

    findings_payload = [
        {
            "severity": f.severity,
            "confidence": f.confidence,
            "category": f.category,
            "file_path": f.file_path,
            "start_line": f.start_line,
            "end_line": f.end_line,
            "title": f.title,
            "rationale": f.rationale,
            "cited_tool_calls": list(f.cited_tool_calls),
            "will_post_inline": f.will_post_inline,
        }
        for f in validation.findings
    ]
    dropped_payload = [
        {"reason": d.reason, "raw": d.raw} for d in validation.dropped
    ]

    org_id = event_row["org_id"]

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            rls_org = set_rls_context(
                cur, conn, user_id_for_rls, log_prefix="[change_intercept:persist]"
            )
            if not rls_org or rls_org != org_id:
                raise RuntimeError(
                    f"RLS context mismatch persisting investigation: "
                    f"rls={rls_org} event={org_id}"
                )
            cur.execute(
                """INSERT INTO change_investigations (
                       change_event_id, org_id, parent_investigation_id,
                       verdict, summary, intent_alignment, intent_notes,
                       findings, dropped_findings, tool_calls,
                       tool_call_count, duration_ms, llm_model, dry_run
                   ) VALUES (
                       %s, %s, %s, %s, %s, %s, %s,
                       %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s, %s
                   ) RETURNING id""",
                (
                    event_row["id"],
                    org_id,
                    parent_investigation_id,
                    validation.verdict,
                    validation.summary,
                    validation.intent_alignment,
                    validation.intent_notes,
                    json.dumps(findings_payload),
                    json.dumps(dropped_payload),
                    json.dumps(list(inv_result.tool_calls or [])),
                    int(inv_result.tool_call_count or 0),
                    int(inv_result.duration_ms or 0),
                    inv_result.llm_model,
                    bool(dry_run),
                ),
            )
            new_id = cur.fetchone()[0]
            conn.commit()
            return str(new_id)


# ─── Prompt building helper ─────────────────────────────────────────


def _build_prompt_for_event(
    event_row: dict[str, Any],
    user_id_for_rls: str,
) -> tuple[str, str | None]:
    """Build the investigator prompt + look up the parent investigation.

    Returns ``(prompt, parent_investigation_id_or_None)``.
    """
    from services.change_intercept.prompts import (
        build_followup_prompt,
        build_initial_prompt,
    )

    snapshot = {
        "change_body": event_row.get("change_body"),
        "change_diff": event_row.get("change_diff"),
        "change_files": _ensure_list(event_row.get("change_files")),
        "change_commits": _ensure_list(event_row.get("change_commits")),
    }
    event_meta = {
        "repo": event_row.get("repo"),
        "ref": event_row.get("ref"),
        "base_ref": event_row.get("base_ref"),
        "commit_sha": event_row.get("commit_sha"),
        "actor": event_row.get("actor"),
        "target_env": event_row.get("target_env"),
    }

    if event_row.get("kind") == "code_change_followup":
        prior, prior_id = _load_prior_investigation(
            org_id=event_row["org_id"],
            dedup_key=event_row.get("dedup_key") or "",
            user_id_for_rls=user_id_for_rls,
        )
        prompt = build_followup_prompt(
            snapshot=snapshot,
            event_meta=event_meta,
            prior_investigation=prior or {},
            followup_comment=event_row.get("follow_up_comment") or "",
        )
        return prompt, prior_id

    prompt = build_initial_prompt(snapshot=snapshot, event_meta=event_meta)
    return prompt, None


def _ensure_list(value: Any) -> list[Any]:
    """psycopg2 returns JSONB as Python objects; coerce defensively."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, list) else []
        except json.JSONDecodeError:
            return []
    return []


# ─── Live review posting (Phase 1a Part 3) ──────────────────────────


def _maybe_post_live_review(
    *,
    event_row: dict[str, Any],
    validation: Any,
    rendered: Any,
    prior_investigation_id: str | None,
    investigation_id: str,
    user_id_for_rls: str,
    dry_run: bool,
    installation_id: int | None,
) -> dict[str, Any]:
    """Post the rendered review to the vendor when the install allows.

    Decision tree:

        1. ``dry_run=True`` (the install is in calibration mode) →
           return without posting; the dry-run row already exists.
        2. ``dry_run=False`` AND this is a followup → take a per-PR
           Postgres advisory lock, dismiss the prior verdict, then
           post the new one. The advisory lock serialises concurrent
           followups so we don't end up with two active reviews.
        3. ``dry_run=False`` AND this is an initial event → still
           take the advisory lock (defence in depth against rapid-fire
           opened/reopened events) and post.
        4. UPDATE the change_investigations row with the returned
           ``verdict_id`` + ``inline_comment_ids``. If the DB UPDATE
           fails AFTER GitHub returned a 2xx, we have a posted review
           with no DB record — a partial-success state. The recovery
           path attempts to immediately dismiss the just-posted review
           and logs loudly with the verdict_id so an operator can
           clean up.

    Returns a small dict the caller embeds in its status log:
        ``{"dry_run": bool, "posted": bool, "verdict_id": str | None}``.

    The dismiss_prior call DOES propagate genuine outages (5xx /
    auth errors / network failures via GitHubFetchError without 404
    or 422). When the dismiss step fails for that reason we skip the
    new post — it's safer to leave a stale review than to stack two
    blocking reviews onto a PR.
    """
    if installation_id is None or dry_run:
        return {"dry_run": True, "posted": False, "verdict_id": None}

    org_id = event_row["org_id"]
    dedup_key = event_row.get("dedup_key") or ""
    lock_token = _advisory_lock_token(org_id, dedup_key)

    try:
        from services.change_intercept.adapters.registry import get_adapter

        adapter = get_adapter(event_row.get("vendor") or "github")
        normalized_event = _rebuild_normalized_event(event_row, installation_id)

        with _per_pr_advisory_lock(lock_token):
            # Dismiss the prior verdict before posting the new one.
            # If the dismiss fails with a non-benign HTTP status we
            # bail rather than stacking blocking reviews.
            prior_posted = _load_prior_posted_verdict(
                org_id=org_id,
                dedup_key=dedup_key,
                current_investigation_id=investigation_id,
                user_id_for_rls=user_id_for_rls,
            )
            if prior_posted is not None:
                from services.change_intercept.adapters.base import PostedVerdict

                try:
                    adapter.dismiss_prior(
                        normalized_event,
                        PostedVerdict(
                            verdict_id=prior_posted["verdict_id"],
                            inline_comment_ids=prior_posted.get(
                                "inline_comment_ids"
                            )
                            or [],
                        ),
                    )
                except Exception as exc:
                    logger.warning(
                        "change_intercept_event=dismiss_prior_failed_skipping_post "
                        "investigation_id=%s prior_review_id=%s error_class=%s",
                        investigation_id,
                        prior_posted.get("verdict_id"),
                        type(exc).__name__,
                    )
                    return {
                        "dry_run": False,
                        "posted": False,
                        "verdict_id": None,
                    }

            investigation_payload = {
                "verdict_event": rendered.verdict_event,
                "body": rendered.body,
                "inline_comments": rendered.inline_comments,
                "commit_sha": event_row.get("commit_sha"),
            }
            posted = adapter.post_verdict(normalized_event, investigation_payload)

            # CRITICAL: the review is now live on GitHub. We MUST persist
            # its ids or future dismiss_prior calls can't find it. Retry
            # the UPDATE a few times before giving up. On final failure,
            # attempt to immediately dismiss the just-posted review and
            # surface the verdict_id loudly so an operator can clean up.
            updated = _persist_posted_verdict_ids_with_retry(
                investigation_id=investigation_id,
                posted_verdict_id=posted.verdict_id,
                inline_comment_ids=posted.inline_comment_ids,
                org_id=event_row["org_id"],
                user_id_for_rls=user_id_for_rls,
            )
            if not updated:
                from services.change_intercept.adapters.base import PostedVerdict

                logger.error(
                    "change_intercept_event=live_review_orphan investigation_id=%s "
                    "review_id=%s repo=%s — DB UPDATE failed after successful "
                    "GitHub post; attempting compensating dismiss",
                    investigation_id,
                    posted.verdict_id,
                    event_row.get("repo"),
                )
                try:
                    adapter.dismiss_prior(
                        normalized_event,
                        PostedVerdict(
                            verdict_id=posted.verdict_id,
                            inline_comment_ids=list(
                                posted.inline_comment_ids
                            ),
                        ),
                    )
                    return {
                        "dry_run": False,
                        "posted": False,
                        "verdict_id": None,
                    }
                except Exception as cleanup_exc:
                    logger.error(
                        "change_intercept_event=live_review_orphan_cleanup_failed "
                        "investigation_id=%s review_id=%s error_class=%s — "
                        "MANUAL CLEANUP REQUIRED",
                        investigation_id,
                        posted.verdict_id,
                        type(cleanup_exc).__name__,
                    )
                    return {
                        "dry_run": False,
                        "posted": True,
                        "verdict_id": posted.verdict_id,
                    }

        logger.info(
            "change_intercept_event=live_review_posted investigation_id=%s "
            "verdict_id=%s inline=%d",
            investigation_id,
            posted.verdict_id,
            len(posted.inline_comment_ids),
        )
        return {
            "dry_run": False,
            "posted": True,
            "verdict_id": posted.verdict_id,
        }
    except Exception as exc:
        logger.warning(
            "change_intercept_event=live_review_failed investigation_id=%s "
            "error_class=%s",
            investigation_id,
            type(exc).__name__,
        )
        return {"dry_run": False, "posted": False, "verdict_id": None}


# ─── Per-PR advisory lock helpers ───────────────────────────────────


_ADVISORY_LOCK_NAMESPACE: int = 0x_CHA9E_C5  # arbitrary namespace tag


def _advisory_lock_token(org_id: str, dedup_key: str) -> int:
    """Return a deterministic 64-bit advisory-lock token for the PR.

    Postgres advisory locks take a bigint. We hash ``(org_id, dedup_key)``
    into a 63-bit positive int (sign-bit clear to avoid negative ids on
    drivers that don't tolerate them). A stable hash means concurrent
    workers see the same lock for the same PR.
    """
    import hashlib

    digest = hashlib.blake2b(
        f"{org_id}\x00{dedup_key}".encode("utf-8"), digest_size=8
    ).digest()
    token = int.from_bytes(digest, "big") & 0x7FFFFFFFFFFFFFFF
    return token or 1  # avoid zero (some drivers treat as "no lock")


class _per_pr_advisory_lock:
    """Context manager that takes + releases a per-PR pg_advisory_lock.

    The lock is session-scoped; the connection is opened on enter and
    closed on exit, releasing the lock automatically. We use the
    blocking ``pg_advisory_lock`` (not the _try variant) because
    waiters are bounded by the LLM call duration of the current
    holder — typically seconds. The Celery task soft-time-limit caps
    a worst case.
    """

    def __init__(self, token: int) -> None:
        self._token = token
        self._conn: Any = None

    def __enter__(self) -> "_per_pr_advisory_lock":
        from utils.db.connection_pool import db_pool

        self._conn_cm = db_pool.get_admin_connection()
        self._conn = self._conn_cm.__enter__()
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_lock(%s);", (self._token,))
        except BaseException:
            # The lock acquisition raised; __exit__ won't be called by
            # the runtime since __enter__ didn't complete. Release the
            # connection back to the pool so we don't leak.
            try:
                self._conn_cm.__exit__(None, None, None)
            finally:
                self._conn = None
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._conn is not None:
                with self._conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s);", (self._token,))
        finally:
            if self._conn is not None:
                self._conn_cm.__exit__(exc_type, exc, tb)
                self._conn = None


def _persist_posted_verdict_ids_with_retry(
    *,
    investigation_id: str,
    posted_verdict_id: str,
    inline_comment_ids: list[str],
    org_id: str,
    user_id_for_rls: str,
    attempts: int = 3,
) -> bool:
    """Attempt the verdict-id UPDATE up to ``attempts`` times.

    Returns ``True`` on success, ``False`` if every attempt failed.
    Used by the live-posting path to recover from transient DB blips
    after the review has already been posted to GitHub.
    """
    import time as _time

    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            _persist_posted_verdict_ids(
                investigation_id=investigation_id,
                posted_verdict_id=posted_verdict_id,
                inline_comment_ids=inline_comment_ids,
                org_id=org_id,
                user_id_for_rls=user_id_for_rls,
            )
            return True
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "change_intercept_event=persist_posted_retry attempt=%d "
                "investigation_id=%s error_class=%s",
                attempt + 1,
                investigation_id,
                type(exc).__name__,
            )
            _time.sleep(0.5 * (attempt + 1))
    if last_exc is not None:
        logger.error(
            "change_intercept_event=persist_posted_exhausted investigation_id=%s "
            "review_id=%s error_class=%s",
            investigation_id,
            posted_verdict_id,
            type(last_exc).__name__,
        )
    return False


def _resolve_installation_id(
    event_row: dict[str, Any], user_id_for_rls: str
) -> int | None:
    """Return the installation_id for the event, falling back to a join
    on ``user_github_installations`` when the change_events row doesn't
    carry one (older rows from before installation_id became standard)."""
    direct = event_row.get("installation_id")
    if direct is not None:
        try:
            return int(direct)
        except (TypeError, ValueError):
            pass

    try:
        from utils.db.connection_pool import db_pool

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT installation_id FROM user_github_installations
                        WHERE user_id = %s AND disconnected_at IS NULL
                        ORDER BY is_primary DESC, linked_at ASC
                        LIMIT 1""",
                    (user_id_for_rls,),
                )
                row = cur.fetchone()
                return int(row[0]) if row and row[0] else None
    except Exception:
        return None


def _rebuild_normalized_event(
    event_row: dict[str, Any], installation_id: int
) -> Any:
    """Reconstruct a ``NormalizedChangeEvent`` from a persisted row.

    The adapter only reads a handful of fields out of the dataclass,
    but we instantiate it through the canonical constructor so future
    fields stay in sync.
    """
    from services.change_intercept.adapters.base import NormalizedChangeEvent

    return NormalizedChangeEvent(
        vendor=event_row.get("vendor") or "github",
        kind=event_row.get("kind") or "code_change",
        org_id=event_row["org_id"],
        installation_id=installation_id,
        external_id=event_row.get("external_id") or "",
        dedup_key=event_row.get("dedup_key") or "",
        repo=event_row.get("repo"),
        ref=event_row.get("ref"),
        base_ref=event_row.get("base_ref"),
        commit_sha=event_row.get("commit_sha"),
        actor=event_row.get("actor"),
        target_env=event_row.get("target_env"),
        action="reply" if event_row.get("kind") == "code_change_followup" else "opened",
        parent_external_id=_parent_external_id_from_event(event_row),
        follow_up_comment=event_row.get("follow_up_comment"),
    )


def _parent_external_id_from_event(event_row: dict[str, Any]) -> str | None:
    """For followups, the parent PR number comes from the dedup_key
    (``github:owner/repo:<pr_number>``)."""
    if event_row.get("kind") != "code_change_followup":
        return None
    dedup_key = event_row.get("dedup_key") or ""
    parts = dedup_key.rsplit(":", 1)
    return parts[1] if len(parts) == 2 and parts[1].isdigit() else None


def _load_prior_posted_verdict(
    *,
    org_id: str,
    dedup_key: str,
    current_investigation_id: str,
    user_id_for_rls: str,
) -> dict[str, Any] | None:
    """Find the most recent posted verdict for ``(org_id, dedup_key)``.

    Skips the current investigation (we're about to post for it, not
    dismiss it). Returns ``None`` if no prior review was posted —
    either there's no prior at all or every prior was dry-run.
    """
    from utils.auth.stateless_auth import set_rls_context
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            if not set_rls_context(
                cur,
                conn,
                user_id_for_rls,
                log_prefix="[change_intercept:prior_posted]",
            ):
                return None
            cur.execute(
                """SELECT ci.external_verdict_id, ci.inline_comment_ids
                     FROM change_investigations ci
                     JOIN change_events ce ON ce.id = ci.change_event_id
                    WHERE ce.org_id = %s
                      AND ce.dedup_key = %s
                      AND ci.id <> %s
                      AND ci.external_verdict_id IS NOT NULL
                 ORDER BY ci.investigated_at DESC
                    LIMIT 1""",
                (org_id, dedup_key, current_investigation_id),
            )
            row = cur.fetchone()
            if row is None:
                return None
            verdict_id, comment_ids = row
            return {
                "verdict_id": verdict_id,
                "inline_comment_ids": comment_ids
                if isinstance(comment_ids, list)
                else [],
            }


def _persist_posted_verdict_ids(
    *,
    investigation_id: str,
    posted_verdict_id: str,
    inline_comment_ids: list[str],
    org_id: str,
    user_id_for_rls: str,
) -> None:
    """UPDATE the change_investigations row with the vendor-native ids.

    Called after a successful ``post_verdict`` so the next
    ``dismiss_prior`` (on the next push or reply) can target the
    correct review.
    """
    from utils.auth.stateless_auth import set_rls_context
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            rls_org = set_rls_context(
                cur,
                conn,
                user_id_for_rls,
                log_prefix="[change_intercept:save_posted]",
            )
            if rls_org != org_id:
                logger.warning(
                    "change_intercept_event=save_posted_skipped reason=org_mismatch "
                    "investigation_id=%s",
                    investigation_id,
                )
                return
            cur.execute(
                """UPDATE change_investigations
                      SET external_verdict_id = %s,
                          inline_comment_ids = %s::jsonb
                    WHERE id = %s""",
                (
                    posted_verdict_id,
                    json.dumps(list(inline_comment_ids or [])),
                    investigation_id,
                ),
            )
            conn.commit()
