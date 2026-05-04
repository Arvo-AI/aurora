"""Celery tasks for the change-intercept pipeline.

Handles the lifecycle: persist event -> investigate -> post verdict.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Dict, Optional

from celery_config import celery_app

logger = logging.getLogger(__name__)

MAX_FOLLOWUPS_PER_CHANGE = 5


# ------------------------------------------------------------------
# 1. Persist the incoming change event
# ------------------------------------------------------------------

@celery_app.task(
    bind=True, max_retries=3, default_retry_delay=15,
    name="change_intercept.process_change_event",
)
def process_change_event(
    self,
    vendor: str,
    payload: Dict[str, Any],
    org_id: str,
    installation_id: Optional[int] = None,
) -> None:
    """Parse, persist, and kick off an investigation for a change event."""
    from utils.db.connection_pool import db_pool
    from services.change_intercept.adapters.registry import get_adapter

    adapter = get_adapter(vendor)

    event_id = str(uuid.uuid4())
    pr = payload.get("pull_request", {})
    repo_full = payload.get("repository", {}).get("full_name", "")
    pr_number = pr.get("number")
    head_sha = pr.get("head", {}).get("sha")
    base_ref = pr.get("base", {}).get("ref")
    actor = pr.get("user", {}).get("login")
    body = pr.get("body") or ""
    dedup_key = f"{repo_full}#{pr_number}" if repo_full and pr_number else event_id

    change_diff = None
    change_files = None
    change_commits = None

    try:
        from services.change_intercept.adapters.base import NormalizedChangeEvent
        norm = NormalizedChangeEvent(
            org_id=org_id, vendor=vendor, kind="code_change",
            external_id=dedup_key, dedup_key=dedup_key,
            repo=repo_full, ref=base_ref, commit_sha=head_sha,
            actor=actor, payload=payload,
        )
        snap = adapter.fetch_snapshot(norm, installation_id=installation_id)
        change_diff = snap.change_diff
        change_files = snap.change_files
        change_commits = snap.change_commits
        body = snap.change_body or body
    except Exception as exc:
        logger.warning("[ChangeIntercept] fetch_snapshot failed: %s", exc)

    target_env = _infer_target_env(base_ref)

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO change_events
                        (id, org_id, vendor, kind, external_id, dedup_key,
                         installation_id, repo, ref, commit_sha, actor,
                         target_env, change_body, change_diff, change_files,
                         change_commits, payload)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (org_id, vendor, external_id, commit_sha, kind)
                    DO UPDATE SET
                        change_body = EXCLUDED.change_body,
                        change_diff = EXCLUDED.change_diff,
                        change_files = EXCLUDED.change_files,
                        change_commits = EXCLUDED.change_commits,
                        payload = EXCLUDED.payload,
                        received_at = now()
                    RETURNING id
                    """,
                    (
                        event_id, org_id, vendor, "code_change", dedup_key,
                        dedup_key, installation_id, repo_full or None,
                        base_ref, head_sha, actor, target_env, body,
                        change_diff,
                        json.dumps(change_files) if change_files else None,
                        json.dumps(change_commits) if change_commits else None,
                        json.dumps(payload),
                    ),
                )
                row = cur.fetchone()
                persisted_id = row[0] if row else event_id
            conn.commit()

        logger.info(
            "[ChangeIntercept] Persisted change_event %s for %s",
            persisted_id, dedup_key,
        )
        launch_investigation.delay(str(persisted_id), org_id)

    except Exception as exc:
        logger.exception("[ChangeIntercept] Failed to persist change event: %s", exc)
        raise self.retry(exc=exc)


# ------------------------------------------------------------------
# 2. Launch an investigation via the background chat engine
# ------------------------------------------------------------------

@celery_app.task(
    bind=True, max_retries=2, default_retry_delay=30,
    name="change_intercept.launch_investigation",
    time_limit=1800,
    soft_time_limit=1740,
)
def launch_investigation(self, change_event_id: str, org_id: str) -> None:
    """Kick off a change-intercept investigation for a persisted event."""
    from utils.db.connection_pool import db_pool
    from chat.background.task import (
        create_background_chat_session,
        run_background_chat,
    )
    from chat.background.rca_prompt_builder import build_change_intercept_prompt

    event_row = _fetch_change_event(change_event_id)
    if not event_row:
        logger.error("[ChangeIntercept] change_event %s not found", change_event_id)
        return

    user_id = _resolve_user_for_org(org_id)
    if not user_id:
        logger.error("[ChangeIntercept] No user found for org %s", org_id)
        return

    prompt = build_change_intercept_prompt(event_row)
    repo = event_row.get("repo") or "unknown"
    title = f"Change Risk: {repo}"

    session_id = create_background_chat_session(
        user_id=user_id,
        title=title,
        trigger_metadata={
            "source": "change_intercept",
            "vendor": event_row.get("vendor"),
            "change_event_id": change_event_id,
        },
    )

    start_ms = _now_ms()

    result = run_background_chat.apply(
        args=[user_id, session_id, prompt],
        kwargs={
            "trigger_metadata": {
                "source": "change_intercept",
                "vendor": event_row.get("vendor"),
                "change_event_id": change_event_id,
            },
            "mode": "change_intercept",
            "send_notifications": False,
        },
    ).get(timeout=1800)

    duration_ms = _now_ms() - start_ms

    _persist_investigation(
        change_event_id=change_event_id,
        org_id=org_id,
        session_id=session_id,
        duration_ms=duration_ms,
        result=result,
    )


# ------------------------------------------------------------------
# 3. Follow-up investigation (reply-to-Aurora)
# ------------------------------------------------------------------

@celery_app.task(
    bind=True, max_retries=2, default_retry_delay=30,
    name="change_intercept.launch_followup_investigation",
    time_limit=1800,
    soft_time_limit=1740,
)
def launch_followup_investigation(
    self,
    original_dedup_key: str,
    reply_body: str,
    org_id: str,
) -> None:
    """Re-investigate after an engineer replies to Aurora's review."""
    from utils.db.connection_pool import db_pool
    from chat.background.task import (
        create_background_chat_session,
        run_background_chat,
    )
    from chat.background.rca_prompt_builder import build_change_intercept_prompt

    event_row = _fetch_latest_event_by_dedup(original_dedup_key, org_id)
    if not event_row:
        logger.warning(
            "[ChangeIntercept] No event for dedup_key %s", original_dedup_key,
        )
        return

    prior = _fetch_latest_investigation(event_row["id"])
    if not prior:
        logger.warning(
            "[ChangeIntercept] No prior investigation for event %s", event_row["id"],
        )
        return

    followup_count = _count_followups(event_row["id"])
    if followup_count >= MAX_FOLLOWUPS_PER_CHANGE:
        logger.info(
            "[ChangeIntercept] Thrash guard: %d follow-ups for %s, skipping",
            followup_count, original_dedup_key,
        )
        return

    event_row["follow_up_comment"] = reply_body

    user_id = _resolve_user_for_org(org_id)
    if not user_id:
        logger.error("[ChangeIntercept] No user found for org %s", org_id)
        return

    prompt = build_change_intercept_prompt(event_row, prior_investigation=prior)
    repo = event_row.get("repo") or "unknown"

    session_id = create_background_chat_session(
        user_id=user_id,
        title=f"Change Risk Follow-up: {repo}",
        trigger_metadata={
            "source": "change_intercept",
            "vendor": event_row.get("vendor"),
            "change_event_id": str(event_row["id"]),
            "followup": True,
        },
    )

    start_ms = _now_ms()

    result = run_background_chat.apply(
        args=[user_id, session_id, prompt],
        kwargs={
            "trigger_metadata": {
                "source": "change_intercept",
                "vendor": event_row.get("vendor"),
                "change_event_id": str(event_row["id"]),
                "followup": True,
            },
            "mode": "change_intercept",
            "send_notifications": False,
        },
    ).get(timeout=1800)

    duration_ms = _now_ms() - start_ms

    _persist_investigation(
        change_event_id=str(event_row["id"]),
        org_id=org_id,
        session_id=session_id,
        duration_ms=duration_ms,
        result=result,
        parent_investigation_id=str(prior["id"]),
    )


# ------------------------------------------------------------------
# 4. Post verdict back to the vendor
# ------------------------------------------------------------------

@celery_app.task(
    bind=True, max_retries=3, default_retry_delay=10,
    name="change_intercept.post_verdict",
)
def post_verdict(self, investigation_id: str, org_id: str) -> None:
    """Post the investigation verdict back to the vendor (e.g. GitHub review)."""
    from utils.db.connection_pool import db_pool
    from services.change_intercept.adapters.registry import get_adapter

    inv = _fetch_investigation(investigation_id)
    if not inv:
        logger.error("[ChangeIntercept] Investigation %s not found", investigation_id)
        return

    event = _fetch_change_event(str(inv["change_event_id"]))
    if not event:
        logger.error(
            "[ChangeIntercept] Event %s not found for investigation %s",
            inv["change_event_id"], investigation_id,
        )
        return

    adapter = get_adapter(event["vendor"])

    # Dismiss any prior Aurora review on this change
    if inv.get("parent_investigation_id"):
        parent = _fetch_investigation(str(inv["parent_investigation_id"]))
        if parent and parent.get("external_verdict_id"):
            try:
                adapter.dismiss_prior(event, parent["external_verdict_id"])
            except NotImplementedError:
                logger.debug("[ChangeIntercept] dismiss_prior not yet implemented")
            except Exception as exc:
                logger.warning("[ChangeIntercept] dismiss_prior failed: %s", exc)

    try:
        external_id = adapter.post_verdict(event, inv)
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE change_investigations SET external_verdict_id = %s WHERE id = %s",
                    (external_id, investigation_id),
                )
            conn.commit()
        logger.info(
            "[ChangeIntercept] Posted verdict %s for investigation %s",
            inv["verdict"], investigation_id,
        )
    except NotImplementedError:
        logger.info("[ChangeIntercept] post_verdict not yet implemented; skipping")
    except Exception as exc:
        logger.exception("[ChangeIntercept] post_verdict failed: %s", exc)
        raise self.retry(exc=exc)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _now_ms() -> int:
    return int(time.time() * 1000)


_TARGET_ENV_PATTERNS = [
    ("production", ("main", "master")),
    ("staging", ("staging", "stag")),
    ("development", ("dev", "develop")),
]


def _infer_target_env(base_ref: str | None) -> str | None:
    if not base_ref:
        return None
    ref_lower = base_ref.lower()
    for env, prefixes in _TARGET_ENV_PATTERNS:
        for prefix in prefixes:
            if ref_lower == prefix or ref_lower.startswith(prefix):
                return env
    return None


def _fetch_change_event(event_id: str) -> Dict[str, Any] | None:
    from utils.db.connection_pool import db_pool
    from psycopg2.extras import RealDictCursor

    with db_pool.get_admin_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM change_events WHERE id = %s", (event_id,))
            return cur.fetchone()


def _fetch_latest_event_by_dedup(
    dedup_key: str, org_id: str,
) -> Dict[str, Any] | None:
    from utils.db.connection_pool import db_pool
    from psycopg2.extras import RealDictCursor

    with db_pool.get_admin_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM change_events "
                "WHERE dedup_key = %s AND org_id = %s "
                "ORDER BY received_at DESC LIMIT 1",
                (dedup_key, org_id),
            )
            return cur.fetchone()


def _fetch_latest_investigation(change_event_id: str) -> Dict[str, Any] | None:
    from utils.db.connection_pool import db_pool
    from psycopg2.extras import RealDictCursor

    with db_pool.get_admin_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM change_investigations "
                "WHERE change_event_id = %s "
                "ORDER BY investigated_at DESC LIMIT 1",
                (change_event_id,),
            )
            return cur.fetchone()


def _fetch_investigation(investigation_id: str) -> Dict[str, Any] | None:
    from utils.db.connection_pool import db_pool
    from psycopg2.extras import RealDictCursor

    with db_pool.get_admin_connection() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM change_investigations WHERE id = %s",
                (investigation_id,),
            )
            return cur.fetchone()


def _count_followups(change_event_id: str) -> int:
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM change_investigations "
                "WHERE change_event_id = %s AND parent_investigation_id IS NOT NULL",
                (change_event_id,),
            )
            row = cur.fetchone()
            return row[0] if row else 0


def _resolve_user_for_org(org_id: str) -> str | None:
    """Find any active user in the org to run the investigation under.

    Change-intercept is org-scoped, not user-scoped. We pick the first
    admin-ish user for the org so that ``run_background_chat`` has a
    user_id to set RLS context with.
    """
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM users WHERE org_id = %s LIMIT 1",
                (org_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None


def _persist_investigation(
    *,
    change_event_id: str,
    org_id: str,
    session_id: str,
    duration_ms: int,
    result: Dict[str, Any],
    parent_investigation_id: str | None = None,
) -> None:
    """Extract the verdict from the LLM output and persist it."""
    from utils.db.connection_pool import db_pool
    from services.change_intercept.verdict_validator import validate_verdict

    llm_output = _extract_llm_output(session_id)
    tool_calls = result.get("tool_calls") or []

    try:
        verdict_data = validate_verdict(llm_output)
    except ValueError as exc:
        logger.warning(
            "[ChangeIntercept] Verdict parse failed for event %s: %s — defaulting to approve",
            change_event_id, exc,
        )
        verdict_data = {
            "verdict": "approve",
            "rationale": "Investigation completed but verdict could not be parsed.",
            "intent_alignment": None,
            "intent_notes": None,
            "cited_findings": [],
        }

    investigation_id = str(uuid.uuid4())

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO change_investigations
                    (id, change_event_id, org_id, parent_investigation_id,
                     verdict, rationale, intent_alignment, intent_notes,
                     cited_findings, tool_calls, tool_call_count,
                     duration_ms, llm_model, chat_session_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    investigation_id, change_event_id, org_id,
                    parent_investigation_id,
                    verdict_data["verdict"],
                    verdict_data["rationale"],
                    verdict_data.get("intent_alignment"),
                    verdict_data.get("intent_notes"),
                    json.dumps(verdict_data.get("cited_findings", [])),
                    json.dumps(tool_calls),
                    len(tool_calls),
                    duration_ms,
                    "default",
                    session_id,
                ),
            )
        conn.commit()

    logger.info(
        "[ChangeIntercept] Investigation %s: verdict=%s for event %s",
        investigation_id, verdict_data["verdict"], change_event_id,
    )

    post_verdict.delay(investigation_id, org_id)


def _extract_llm_output(session_id: str) -> str:
    """Read the LLM's final assistant message from the chat session."""
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT content FROM chat_messages
                WHERE session_id = %s AND role = 'assistant'
                ORDER BY created_at DESC LIMIT 1
                """,
                (session_id,),
            )
            row = cur.fetchone()
            return row[0] if row else ""
