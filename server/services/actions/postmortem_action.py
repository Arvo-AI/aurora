"""
Built-in Postmortem Generation Action

Dispatches the "Generate Postmortem" action as a background agent session
with access to postmortem tools, Slack tools, and all other connected tools.
"""

import logging
from typing import Optional

from utils.db.connection_pool import db_pool
from utils.auth.stateless_auth import set_rls_context

logger = logging.getLogger(__name__)


def _get_action_instructions(user_id: str) -> tuple:
    """Load instructions and action_id from the DB system action. Returns (instructions, action_id, org_id)."""
    try:
        with db_pool.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT a.instructions, a.id, a.org_id FROM actions a
                       JOIN users u ON u.org_id = a.org_id
                       WHERE u.id = %s AND a.system_key = 'generate_postmortem'""",
                    (user_id,),
                )
                row = cur.fetchone()
                if row and row[0]:
                    return row[0], str(row[1]), row[2]
    except Exception as e:
        logger.warning("[PostmortemAction] Failed to load action from DB, using default: %s", e)
    return DEFAULT_POSTMORTEM_INSTRUCTIONS, None, None

DEFAULT_POSTMORTEM_INSTRUCTIONS = """**Step 1: Read existing postmortem (if regenerating)**
Call get_postmortem to check if there is a prior version. If one exists, use it as a baseline
so the new version does not diverge too far from what the team has already reviewed.

**Step 2: Gather human context (if communication platforms connected)**
If Slack tools are available:
- Call list_slack_channels to discover relevant channels (look for incident, on-call, or service-specific channels).
- Call get_channel_history on the most relevant channels using the incident's time window (started_at to resolved/now).
- Look for: deployment decisions, fatigue/pressure indicators, communication gaps, handoff confusion, and resolution steps taken by humans.
- If you find interesting threads, use get_thread_replies to get full context.

**Step 3: Write the postmortem**
Generate a structured markdown document with these sections:
- **Summary**: 2-3 sentence description of what happened
- **Timeline**: Key events with timestamps (use format: **HH:MM UTC** - Description)
- **Root Cause**: Technical root cause based on RCA data
- **Impact**: What was affected — services, users, SLAs
- **Contributing Factors**: Human and process factors discovered from conversations (deployment pressure, alert fatigue, communication gaps, etc.)
- **Resolution**: How the incident was resolved or mitigated
- **Action Items**: Concrete follow-ups as checkboxes (- [ ] item)
- **Lessons Learned**: What can prevent similar incidents

Incorporate conversation context alongside RCA data. Professional tone, no speculation beyond what data supports.

**Step 4: Save**
Call save_postmortem with the final markdown document."""


def dispatch_postmortem_action(
    user_id: str,
    incident_id: str,
    custom_instructions: Optional[str] = None,
) -> str:
    """Dispatch the postmortem generation as a background agent session.

    Args:
        user_id: The user who owns the incident
        incident_id: The incident to generate a postmortem for
        custom_instructions: Optional user-customized instructions (layered on top of defaults)

    Returns:
        run_id of the dispatched action

    Raises:
        ValueError: If rate limited or generation already in progress
    """
    # Check for active generation (row with NULL content = in progress)
    with db_pool.get_connection() as conn:
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[PostmortemAction]")
            cur.execute(
                """SELECT id FROM postmortems
                   WHERE incident_id = %s AND content IS NULL""",
                (incident_id,),
            )
            if cur.fetchone():
                raise ValueError("Postmortem generation already running for this incident")

    instructions, action_id, org_id = _get_action_instructions(user_id)
    if custom_instructions:
        instructions = f"{instructions}\n\n## Additional Instructions (user-customized)\n{custom_instructions}"

    # Load incident context for the prompt
    incident_context = _load_incident_for_prompt(incident_id, user_id)
    if not incident_context:
        raise ValueError("Incident not found or not accessible")

    # Build the full prompt
    prompt = _build_action_prompt(instructions, incident_context)

    # Dispatch via background chat with postmortem tools
    from chat.background.task import (
        is_background_chat_allowed,
        create_background_chat_session,
        run_background_chat,
    )

    if not is_background_chat_allowed(user_id):
        raise ValueError("Rate limited - too many background chats in the last 5 minutes")

    trigger_meta = {
        "source": "postmortem_generation",
        "incident_id": incident_id,
    }

    # Record an action_run for tracking
    run_id = None
    if action_id:
        from services.actions.executor import _create_run, _update_run
        run_id = _create_run(
            action_id=action_id,
            org_id=org_id,
            user_id=user_id,
            incident_id=incident_id,
            trigger_context=trigger_meta,
            status="running",
        )
        trigger_meta["source"] = "action"
        trigger_meta["run_id"] = run_id
        trigger_meta["action_id"] = action_id

    session_id = create_background_chat_session(
        user_id=user_id,
        title=f"Generate Postmortem: {incident_context.get('title', 'Incident')[:50]}",
        trigger_metadata=trigger_meta,
    )

    # Pre-create the postmortem row so the GET endpoint can detect "generating" state
    _reserve_postmortem_row(user_id, incident_id, session_id)

    if run_id:
        _update_run(run_id, user_id, chat_session_id=session_id)

    run_background_chat.delay(
        user_id=user_id,
        session_id=session_id,
        initial_message=prompt,
        trigger_metadata=trigger_meta,
        mode="agent",
        rail_text=instructions,
        send_notifications=False,
        incident_id=None,
    )

    logger.info("[PostmortemAction] Dispatched postmortem generation (session created)")
    return session_id


def _load_incident_for_prompt(incident_id: str, user_id: str) -> Optional[dict]:
    """Load incident data for the postmortem prompt."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[PostmortemAction]")
                cur.execute(
                    """SELECT alert_title, alert_service, severity, aurora_summary,
                              started_at, analyzed_at, resolved_at, source_type,
                              alert_environment
                       FROM incidents WHERE id = %s""",
                    (incident_id,),
                )
                row = cur.fetchone()
                if not row:
                    return None

                return {
                    "incident_id": incident_id,
                    "title": row[0] or "Unknown Incident",
                    "service": row[1] or "unknown",
                    "severity": row[2] or "unknown",
                    "summary": (row[3] or "")[:4000],
                    "started_at": row[4].isoformat() if row[4] else None,
                    "analyzed_at": row[5].isoformat() if row[5] else None,
                    "resolved_at": row[6].isoformat() if row[6] else None,
                    "source_type": row[7] or "unknown",
                    "environment": row[8] or "unknown",
                }
    except Exception:
        logger.exception("[PostmortemAction] Failed to load incident for postmortem")
        return None


def _build_action_prompt(instructions: str, incident: dict) -> str:
    """Build the full prompt for the postmortem generation agent session."""
    import os

    parts = [
        'You are executing the Aurora built-in action "Generate Postmortem".',
        "",
        "## Incident Context",
        f"- **Incident ID:** {incident['incident_id']}",
        f"- **Title:** {incident['title']}",
        f"- **Service:** {incident['service']}",
        f"- **Severity:** {incident['severity']}",
        f"- **Source:** {incident['source_type']}",
        f"- **Environment:** {incident['environment']}",
    ]

    if incident.get("started_at"):
        parts.append(f"- **Started At:** {incident['started_at']}")
    if incident.get("resolved_at"):
        parts.append(f"- **Resolved At:** {incident['resolved_at']}")
    if incident.get("analyzed_at"):
        parts.append(f"- **RCA Completed At:** {incident['analyzed_at']}")

    if incident.get("summary"):
        parts += ["", "## RCA Summary", incident["summary"]]

    frontend_url = os.getenv("FRONTEND_URL", "").rstrip("/")
    if frontend_url:
        parts.append(f"\nIncident URL: {frontend_url}/incidents/{incident['incident_id']}")

    parts += [
        "",
        "## Your Instructions",
        instructions,
        "",
        "## Guidelines",
        "- Use your available tools (get_postmortem, save_postmortem, Slack tools) to complete the task.",
        "- The incident_id for tool calls is: " + incident["incident_id"],
        "- Write professionally. Do not speculate beyond what the data supports.",
        "- If Slack tools are not available, proceed with only the RCA data.",
    ]

    return "\n".join(parts)


def _reserve_postmortem_row(user_id: str, incident_id: str, session_id: str) -> None:
    """Pre-create a postmortem row with NULL content to signal 'generating' state.

    If a postmortem already exists (regeneration), snapshots the current content
    as a version before nulling it, so the GET endpoint returns 202.
    The save_postmortem tool will later fill in the content via ON CONFLICT UPDATE.
    """
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT org_id FROM users WHERE id = %s", (user_id,))
                row = cur.fetchone()
                org_id = row[0] if row else None
                if not org_id:
                    logger.warning("[PostmortemAction] No org_id for user %s, skipping reserve", user_id)
                    return

                set_rls_context(cur, conn, user_id, log_prefix="[PostmortemAction:reserve]")

                cur.execute(
                    """INSERT INTO postmortems (incident_id, user_id, org_id, content, generation_session_id)
                       VALUES (%s, %s, %s, NULL, %s)
                       ON CONFLICT (incident_id)
                       DO UPDATE SET content = NULL,
                                     generation_session_id = EXCLUDED.generation_session_id,
                                     updated_at = CURRENT_TIMESTAMP""",
                    (incident_id, user_id, org_id, session_id),
                )
                conn.commit()
    except Exception:
        logger.exception("[PostmortemAction] Failed to reserve postmortem row")
