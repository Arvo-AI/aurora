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


# Subset of fields the task reads from ``change_events`` — keeps the
# query specific and the worker memory profile predictable.
_CHANGE_EVENT_COLUMNS: tuple[str, ...] = (
    "id",
    "org_id",
    "vendor",
    "kind",
    "dedup_key",
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
        # ─── 1. Load the change_events row ───────────────────────
        event_row = _load_change_event(change_event_id, user_id_for_rls)
        if event_row is None:
            logger.warning(
                "change_intercept_event=launch_investigation status=event_not_found "
                "change_event_id=%s",
                change_event_id,
            )
            return {"status": "event_not_found"}

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

        # ─── 6. Persist change_investigations row ────────────────
        investigation_id = _persist_investigation(
            event_row=event_row,
            validation=validation,
            inv_result=inv_result,
            user_id_for_rls=user_id_for_rls,
            parent_investigation_id=prior_investigation_id,
        )

        duration_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            "change_intercept_event=launch_investigation status=done "
            "change_event_id=%s investigation_id=%s verdict=%s "
            "findings=%d dropped=%d inline=%d duration_ms=%d "
            "downgraded=%s",
            change_event_id,
            investigation_id,
            validation.verdict,
            len(validation.findings),
            len(validation.dropped),
            sum(1 for f in validation.findings if f.will_post_inline),
            duration_ms,
            validation.downgraded_to_approve,
        )

        return {
            "status": "ok",
            "investigation_id": investigation_id,
            "verdict": validation.verdict,
            "findings_count": len(validation.findings),
            "inline_count": sum(1 for f in validation.findings if f.will_post_inline),
            "downgraded": validation.downgraded_to_approve,
            "dry_run": True,
            "review_event": rendered.verdict_event,
        }
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
        # Standard Celery retry — investigation failures usually mean
        # a transient downstream issue (LLM provider blip, DB stall).
        raise self.retry(exc=exc)


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


def _persist_investigation(
    *,
    event_row: dict[str, Any],
    validation: Any,  # ValidationResult; loose-typed to avoid the import cycle
    inv_result: Any,  # InvestigatorResult
    user_id_for_rls: str,
    parent_investigation_id: str | None,
) -> str:
    """INSERT a ``change_investigations`` row under RLS context.

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
                       %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s, TRUE
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
