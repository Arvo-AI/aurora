"""Actions executor -- dispatches action runs as background chat sessions."""
import json
import logging
import os
import uuid
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from utils.db.connection_pool import db_pool
from utils.auth.stateless_auth import set_rls_context

logger = logging.getLogger(__name__)

_VALID_TRIGGER_TYPES = frozenset(("manual", "on_incident"))
_VALID_MODES = frozenset(("agent", "ask"))


def dispatch_action(
    action_id: str,
    user_id: str,
    trigger_context: Optional[Dict[str, Any]] = None,
) -> str:
    """Dispatch an action as a background chat session. Returns run_id."""
    trigger_context = trigger_context or {}

    with db_pool.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, org_id, name, instructions, mode FROM actions WHERE id = %s",
                (action_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Action {action_id} not found")
            action = {
                "id": str(row[0]),
                "org_id": row[1],
                "name": row[2],
                "instructions": row[3],
                "mode": row[4],
            }

    org_id = action["org_id"]

    from chat.background.task import is_background_chat_allowed
    if not is_background_chat_allowed(user_id):
        run_id = _create_run(
            action_id, org_id, user_id,
            incident_id=trigger_context.get("incident_id"),
            trigger_context=trigger_context,
            status="error",
            error="Rate limited - too many background chats in the last 5 minutes",
        )
        raise ValueError("Rate limited")

    if trigger_context.get("incident_id"):
        trigger_context["incident"] = _load_incident_context(
            trigger_context["incident_id"], user_id
        )

    full_prompt, rail_text = build_action_prompt(action, trigger_context)

    run_id = _create_run(
        action_id, org_id, user_id,
        incident_id=trigger_context.get("incident_id"),
        trigger_context=trigger_context,
        status="pending",
    )

    from chat.background.task import (
        create_background_chat_session,
        run_background_chat,
    )

    trigger_meta = {
        "source": "action",
        "action_id": action_id,
        "run_id": run_id,
    }

    session_id = create_background_chat_session(
        user_id=user_id,
        title=f"Action: {action['name']}",
        trigger_metadata=trigger_meta,
    )

    _update_run(run_id, user_id, chat_session_id=session_id, status="running")

    try:
        run_background_chat.delay(
            user_id=user_id,
            session_id=session_id,
            initial_message=full_prompt,
            trigger_metadata=trigger_meta,
            mode=action["mode"],
            rail_text=rail_text,
            send_notifications=False,
            incident_id=None,
        )
    except Exception as e:
        _update_run(run_id, user_id, status="error", error=f"Failed to enqueue: {e}")
        raise

    logger.info("[Actions] Dispatched action %s as run %s (session %s)", action_id, run_id, session_id)
    return run_id


def build_action_prompt(
    action: Dict[str, Any],
    trigger_context: Dict[str, Any],
) -> Tuple[str, str]:
    """Build (full_prompt, rail_text) for an action run."""
    rail_text = action["instructions"]

    parts = [
        f'You are executing an Aurora Action called "{action["name"]}".',
        "",
        "## Your Instructions",
        action["instructions"],
    ]

    ctx = _format_trigger_context(trigger_context)
    if ctx:
        parts += ["", "## Context", ctx]

    parts += [
        "",
        "## Guidelines",
        "- Use your available tools (Terraform/IaC, GitHub, Datadog, cloud CLI, terminal) to complete the task.",
        "- If you need to make infrastructure changes, open a PR rather than applying directly.",
        "- Report what you did and any issues encountered.",
    ]

    return "\n".join(parts), rail_text


def update_action_run_status(
    run_id: str,
    status: str,
    user_id: str,
    error_message: Optional[str] = None,
) -> None:
    """Update an action_runs row. Safe to call from Celery workers."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[Actions]")
                cur.execute(
                    "UPDATE action_runs SET status = %s, completed_at = %s, error = %s WHERE id = %s",
                    (status, datetime.utcnow(), error_message, run_id),
                )
                conn.commit()
    except Exception:
        logger.exception("[Actions] Failed to update run %s status to %s", run_id, status)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _create_run(
    action_id: str,
    org_id: str,
    user_id: str,
    incident_id: Optional[str] = None,
    trigger_context: Optional[Dict[str, Any]] = None,
    status: str = "pending",
    error: Optional[str] = None,
) -> str:
    run_id = str(uuid.uuid4())
    with db_pool.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO action_runs (id, action_id, org_id, user_id, incident_id,
                   status, trigger_context, error, completed_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    run_id, action_id, org_id, user_id, incident_id,
                    status, json.dumps(trigger_context or {}), error,
                    datetime.utcnow() if status in ("error", "success") else None,
                ),
            )
            conn.commit()
    return run_id


def _update_run(
    run_id: str,
    user_id: str,
    chat_session_id: Optional[str] = None,
    status: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    sets, vals = [], []
    if chat_session_id is not None:
        sets.append("chat_session_id = %s")
        vals.append(chat_session_id)
    if status is not None:
        sets.append("status = %s")
        vals.append(status)
    if error is not None:
        sets.append("error = %s")
        vals.append(error)
        sets.append("completed_at = %s")
        vals.append(datetime.utcnow())
    if not sets:
        return
    vals.append(run_id)
    with db_pool.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE action_runs SET {', '.join(sets)} WHERE id = %s", vals)
            conn.commit()


def _load_incident_context(incident_id: str, user_id: str) -> Dict[str, Any]:
    """Load incident data for prompt context."""
    try:
        with db_pool.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT alert_title, severity, source_type, alert_service,
                              alert_environment, aurora_summary
                       FROM incidents WHERE id = %s""",
                    (incident_id,),
                )
                row = cur.fetchone()
                if not row:
                    return {"incident_id": incident_id}
                return {
                    "incident_id": incident_id,
                    "title": row[0],
                    "severity": row[1],
                    "source": row[2],
                    "service": row[3],
                    "environment": row[4],
                    "summary": (row[5] or "")[:2000],
                }
    except Exception:
        logger.exception("[Actions] Failed to load incident context for %s", incident_id)
        return {"incident_id": incident_id}


def _format_trigger_context(trigger_context: Dict[str, Any]) -> str:
    parts = []
    if trigger_context.get("trigger_label"):
        parts.append(f"Triggered by: {trigger_context['trigger_label']}")

    incident = trigger_context.get("incident")
    if incident:
        parts.append(f"Incident: {incident.get('title', 'Unknown')}")
        if incident.get("severity"):
            parts.append(f"Severity: {incident['severity']}")
        if incident.get("source"):
            parts.append(f"Source: {incident['source']}")
        if incident.get("service"):
            parts.append(f"Service: {incident['service']}")
        if incident.get("environment"):
            parts.append(f"Environment: {incident['environment']}")
        if incident.get("summary"):
            parts.append(f"\nRCA Summary:\n{incident['summary']}")
        frontend_url = os.getenv("FRONTEND_URL", "").rstrip("/")
        if frontend_url and incident.get("incident_id"):
            parts.append(f"\nIncident URL: {frontend_url}/incidents/{incident['incident_id']}")

    return "\n".join(parts)
