"""
Centralized notification dispatcher.

All notification events (investigation start/complete, action start/complete) route
through this module. It handles preference checking, recipient resolution, and dispatch
to enabled channels.

Investigation notifications dispatch to: email, Slack, Google Chat.
Action notifications dispatch to: email, Slack.

Callers (task.py, summarization.py) make a single one-line call and remain unaware
of which channels are enabled.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from utils.auth.stateless_auth import (
    get_credentials_from_db,
    get_org_id_for_user,
    get_org_preference,
    set_rls_context,
)
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

_UNKNOWN_ACTION = "Unknown Action"


def _get_org_email_recipients(org_id: str, user_id: str) -> list:
    """Fetch verified and enabled email recipients for an org."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix="[Dispatcher:Emails]")
                cursor.execute(
                    """
                    SELECT email FROM rca_notification_emails
                    WHERE org_id = %s AND is_verified = TRUE AND is_enabled = TRUE
                    ORDER BY verified_at ASC
                    """,
                    (org_id,),
                )
                return [row[0] for row in cursor.fetchall()]
    except Exception:
        logger.exception(f"[Dispatcher] Failed to fetch email recipients for org {org_id}")
        return []


def _has_slack_connected(user_id: str) -> bool:
    """Check if user has a valid Slack client."""
    try:
        from connectors.slack_connector.client import get_slack_client_for_user
        return get_slack_client_for_user(user_id) is not None
    except Exception:
        return False


def _has_google_chat_connected(user_id: str) -> bool:
    """Check if user's org has Google Chat connected with a service account."""
    try:
        from connectors.google_chat_connector.client import get_chat_app_client
        config = get_credentials_from_db(user_id, "google_chat")
        if not config or not config.get("incidents_space_name"):
            return False
        return get_chat_app_client() is not None
    except Exception:
        return False


def _get_incident_data(incident_id: str, user_id: str) -> Optional[Dict[str, Any]]:
    """Fetch incident data from database."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix="[Dispatcher:GetIncident]")
                cursor.execute(
                    """
                    SELECT id, user_id, source_type, status, severity, alert_title,
                           alert_service, aurora_status, aurora_summary, started_at,
                           analyzed_at, created_at, slack_message_ts, google_chat_message_name
                    FROM incidents
                    WHERE id = %s
                    """,
                    (incident_id,),
                )
                result = cursor.fetchone()
                if result:
                    return {
                        'incident_id': str(result[0]),
                        'user_id': result[1],
                        'source_type': result[2],
                        'status': result[3],
                        'severity': result[4] or 'unknown',
                        'alert_title': result[5] or 'Unknown Alert',
                        'service': result[6] or 'unknown',
                        'aurora_status': result[7],
                        'aurora_summary': result[8],
                        'started_at': result[9],
                        'analyzed_at': result[10],
                        'created_at': result[11],
                        'slack_message_ts': result[12],
                        'google_chat_message_name': result[13],
                    }
        return None
    except Exception:
        logger.exception(f"[Dispatcher] Error fetching incident {incident_id}")
        return None


def _enrich_incident_summary(incident_data: Dict[str, Any], session_id: str, user_id: str) -> None:
    """If aurora_summary is missing, extract it from the chat session's last message."""
    if incident_data.get('aurora_summary'):
        return
    try:
        from routes.slack.slack_events_helpers import extract_summary_section
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix="[Dispatcher:EnrichSummary]")
                cursor.execute(
                    "SELECT messages FROM chat_sessions WHERE id = %s",
                    (session_id,),
                )
                row = cursor.fetchone()
                if not row or not row[0]:
                    return
                messages = row[0]
                if isinstance(messages, str):
                    messages = json.loads(messages)
                for msg in reversed(messages):
                    if msg.get('sender') not in ('bot', 'assistant'):
                        continue
                    last_message = msg.get('text') or msg.get('content')
                    if last_message:
                        incident_data['aurora_summary'] = extract_summary_section(last_message)
                    break
    except Exception as e:
        logger.warning(f"[Dispatcher] Failed to enrich summary: {e}")


def _fetch_action_run_details(run_id: str, user_id: str) -> tuple:
    """Fetch action name and started_at from action_runs. Returns (name, started_at)."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[Dispatcher:ActionData]")
                cur.execute(
                    """SELECT a.name, ar.started_at
                       FROM action_runs ar
                       JOIN actions a ON a.id = ar.action_id
                       WHERE ar.id = %s""",
                    (run_id,),
                )
                row = cur.fetchone()
                if not row:
                    return _UNKNOWN_ACTION, None
                started = row[1]
                if started and not started.tzinfo:
                    started = started.replace(tzinfo=timezone.utc)
                elif started:
                    started = started.astimezone(timezone.utc)
                return row[0], started
    except Exception as e:
        logger.warning("[Dispatcher] Failed to fetch action run details: %s", e)
    return _UNKNOWN_ACTION, None


def _fetch_last_bot_message(session_id: str, user_id: str) -> Optional[str]:
    """Fetch the last bot message text from a chat session."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[Dispatcher:ActionResult]")
                cur.execute(
                    "SELECT messages FROM chat_sessions WHERE id = %s",
                    (session_id,),
                )
                row = cur.fetchone()
                if not row or not row[0]:
                    return None
                messages = row[0] if isinstance(row[0], list) else json.loads(row[0])
                for msg in reversed(messages):
                    if msg.get('sender') == 'bot' and msg.get('text'):
                        return msg['text']
    except Exception as e:
        logger.warning("[Dispatcher] Failed to fetch action result summary: %s", e)
    return None


def _get_action_data(user_id: str, trigger_metadata: Dict[str, Any], session_id: str,
                     status: str = 'running', error_message: Optional[str] = None) -> Dict[str, Any]:
    """Build action data dict from trigger metadata and optional DB lookup."""
    run_id = trigger_metadata.get('run_id')
    action_name = _UNKNOWN_ACTION
    result_summary = None
    started_at = None

    if run_id:
        action_name, started_at = _fetch_action_run_details(run_id, user_id)

    if session_id and status == 'success' and not error_message:
        last_bot_text = _fetch_last_bot_message(session_id, user_id)
        if last_bot_text:
            result_summary = _summarize_action_result(action_name, last_bot_text, user_id)

    return {
        'action_name': action_name,
        'result_summary': result_summary,
        'run_id': run_id,
        'status': status,
        'error': error_message,
        'started_at': started_at,
        'completed_at': datetime.now(timezone.utc) if status != 'running' else None,
        'session_id': session_id,
    }


def _summarize_action_result(action_name: str, bot_response: str, user_id: str) -> Optional[str]:
    """Summarize an action's output into key results using a lightweight LLM call."""
    try:
        from langchain_core.messages import HumanMessage
        from chat.backend.agent.providers import create_chat_model
        from chat.backend.agent.llm import ModelConfig
        from chat.backend.agent.utils.llm_usage_tracker import tracked_invoke

        truncated_response = bot_response[:4000]

        prompt = f"""Summarize the outcome of action "{action_name}" in ONE short sentence. State counts and status, not specifics.

Example: "Found 5 critical vulnerabilities across 3 services" or "2 active EC2 instances in us-east-1, both healthy"

Response:
{truncated_response}

One-line summary:"""

        llm = create_chat_model(
            ModelConfig.INCIDENT_REPORT_SUMMARIZATION_MODEL,
            temperature=0.1,
            streaming=False,
            request_timeout=25,
        )

        response = tracked_invoke(
            llm,
            [HumanMessage(content=prompt)],
            user_id=user_id,
            session_id=None,
            model_name=ModelConfig.INCIDENT_REPORT_SUMMARIZATION_MODEL,
            request_type="action_result_summary",
        )

        if response and response.content:
            content = response.content
            if isinstance(content, list):
                content = " ".join(
                    str(p.get("text") or "") if isinstance(p, dict) else str(p)
                    for p in content
                    if not (isinstance(p, dict) and p.get("type") in ("thinking", "reasoning"))
                )
            summary = " ".join(str(content).split())
            if summary:
                return summary[:300]
        return None

    except Exception as e:
        logger.warning("[Dispatcher] Failed to summarize action result: %s", e)
        return None


def _store_start_message_info(run_id: str, msg_info: Dict[str, str], user_id: str) -> None:
    """Store the Slack started message ts/channel in action_runs.trigger_context."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[Dispatcher:StoreStartMsg]")
                cur.execute(
                    """UPDATE action_runs
                       SET trigger_context = COALESCE(trigger_context, '{}'::jsonb)
                           || %s::jsonb
                       WHERE id = %s""",
                    (json.dumps({
                        'slack_start_message_ts': msg_info['ts'],
                        'slack_start_message_channel': msg_info['channel_id'],
                    }), run_id),
                )
            conn.commit()
    except Exception as e:
        logger.warning("[Dispatcher] Failed to store start message info: %s", e)


def _get_start_message_info(run_id: str, user_id: str) -> Optional[Dict[str, str]]:
    """Retrieve the stored Slack started message ts/channel from action_runs."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[Dispatcher:GetStartMsg]")
                cur.execute(
                    "SELECT trigger_context FROM action_runs WHERE id = %s",
                    (run_id,),
                )
                row = cur.fetchone()
                if row and row[0]:
                    ctx = row[0] if isinstance(row[0], dict) else json.loads(row[0])
                    ts = ctx.get('slack_start_message_ts')
                    channel = ctx.get('slack_start_message_channel')
                    if ts:
                        return {'ts': ts, 'channel_id': channel}
        return None
    except Exception as e:
        logger.warning("[Dispatcher] Failed to get start message info: %s", e)
        return None


def _send_emails(org_id: str, user_id: str, send_fn_name: str, payload: Any) -> None:
    """Send notification emails to all verified recipients for the org."""
    recipients = _get_org_email_recipients(org_id, user_id)
    if not recipients:
        return
    from utils.notifications.email_service import get_email_service
    email_service = get_email_service()
    send_fn = getattr(email_service, send_fn_name)
    for recipient in recipients:
        try:
            send_fn(recipient, payload)
        except Exception as e:
            logger.warning(f"[Dispatcher] Email dispatch failed: {e}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def notify_investigation_started(user_id: str, incident_id: str) -> None:
    """Send notifications for investigation started event across all enabled channels."""
    try:
        org_id = get_org_id_for_user(user_id)
        if not org_id:
            return

        incident_data = _get_incident_data(incident_id, user_id)
        if not incident_data:
            logger.error(f"[Dispatcher] Incident {incident_id} not found for started notification")
            return

        # --- Email ---
        email_general = bool(get_org_preference(org_id, 'rca_email_notifications', default=False))
        email_start = bool(get_org_preference(org_id, 'rca_email_start_notifications', default=False))
        if email_general and email_start:
            _send_emails(org_id, user_id, 'send_investigation_started_email', incident_data)

        # --- Slack ---
        slack_enabled = bool(get_org_preference(org_id, 'slack_investigation_start_notifications', default=True))
        if slack_enabled and _has_slack_connected(user_id):
            try:
                from utils.notifications.slack_notification_service import (
                    send_slack_investigation_started_notification,
                )
                send_slack_investigation_started_notification(user_id, incident_data)
            except Exception:
                logger.exception("[Dispatcher] Slack started notification failed")

        # --- Google Chat ---
        google_chat_enabled = bool(get_org_preference(org_id, 'google_chat_investigation_notifications', default=True))
        if google_chat_enabled and _has_google_chat_connected(user_id):
            try:
                from utils.notifications.google_chat_notification_service import (
                    send_google_chat_investigation_started_notification,
                )
                send_google_chat_investigation_started_notification(user_id, incident_data)
            except Exception:
                logger.exception("[Dispatcher] Google Chat started notification failed")

    except Exception:
        logger.exception("[Dispatcher] Error in notify_investigation_started")


def notify_investigation_completed(user_id: str, incident_id: str, session_id: Optional[str] = None) -> None:
    """Send notifications for investigation completed event across all enabled channels."""
    try:
        org_id = get_org_id_for_user(user_id)
        if not org_id:
            return

        incident_data = _get_incident_data(incident_id, user_id)
        if not incident_data:
            logger.error(f"[Dispatcher] Incident {incident_id} not found for completed notification")
            return

        if session_id:
            _enrich_incident_summary(incident_data, session_id, user_id)

        # --- Email ---
        email_enabled = bool(get_org_preference(org_id, 'rca_email_notifications', default=False))
        if email_enabled:
            _send_emails(org_id, user_id, 'send_investigation_completed_email', incident_data)

        # --- Slack ---
        slack_enabled = bool(get_org_preference(org_id, 'slack_investigation_complete_notifications', default=True))
        if slack_enabled and _has_slack_connected(user_id):
            try:
                from utils.notifications.slack_notification_service import (
                    send_slack_investigation_completed_notification,
                )
                send_slack_investigation_completed_notification(user_id, incident_data)
            except Exception:
                logger.exception("[Dispatcher] Slack completed notification failed")

        # --- Google Chat ---
        google_chat_enabled = bool(get_org_preference(org_id, 'google_chat_investigation_notifications', default=True))
        if google_chat_enabled and _has_google_chat_connected(user_id):
            try:
                from utils.notifications.google_chat_notification_service import (
                    send_google_chat_investigation_completed_notification,
                )
                send_google_chat_investigation_completed_notification(user_id, incident_data)
            except Exception:
                logger.exception("[Dispatcher] Google Chat completed notification failed")

    except Exception:
        logger.exception("[Dispatcher] Error in notify_investigation_completed")


def notify_investigation_failed(user_id: str, incident_id: str, error_message: Optional[str] = None) -> None:
    """Send notifications for investigation failed event across all enabled channels."""
    try:
        org_id = get_org_id_for_user(user_id)
        if not org_id:
            return

        incident_data = _get_incident_data(incident_id, user_id)
        if not incident_data:
            logger.error(f"[Dispatcher] Incident {incident_id} not found for failed notification")
            return

        # --- Slack ---
        slack_enabled = bool(get_org_preference(org_id, 'slack_investigation_complete_notifications', default=True))
        if slack_enabled and _has_slack_connected(user_id):
            try:
                from utils.notifications.slack_notification_service import (
                    send_slack_investigation_failed_notification,
                )
                send_slack_investigation_failed_notification(user_id, incident_data, error_message=error_message)
            except Exception:
                logger.exception("[Dispatcher] Slack failed notification failed")

    except Exception:
        logger.exception("[Dispatcher] Error in notify_investigation_failed")


def notify_action_started(user_id: str, trigger_metadata: Dict[str, Any], session_id: str) -> None:
    """Send notifications for action started event to email and Slack."""
    try:
        org_id = get_org_id_for_user(user_id)
        if not org_id:
            return

        action_data = _get_action_data(user_id, trigger_metadata, session_id, status='running')

        # --- Email ---
        email_enabled = bool(get_org_preference(org_id, 'action_email_start_notifications', default=False))
        if email_enabled:
            _send_emails(org_id, user_id, 'send_action_started_email', action_data)

        # --- Slack ---
        slack_enabled = bool(get_org_preference(org_id, 'slack_action_start_notifications', default=True))
        if slack_enabled and _has_slack_connected(user_id):
            try:
                from utils.notifications.slack_notification_service import (
                    send_slack_action_started_notification,
                )
                msg_info = send_slack_action_started_notification(user_id, action_data)
                # Store the message ts so we can delete it when the action completes
                if msg_info and trigger_metadata.get('run_id'):
                    _store_start_message_info(trigger_metadata['run_id'], msg_info, user_id)
            except Exception:
                logger.exception("[Dispatcher] Slack action started notification failed")

    except Exception:
        logger.exception("[Dispatcher] Error in notify_action_started")


def notify_action_completed(user_id: str, trigger_metadata: Dict[str, Any], session_id: str,
                            status: str = 'success', error_message: Optional[str] = None) -> None:
    """Send notifications for action completed event to email and Slack."""
    try:
        org_id = get_org_id_for_user(user_id)
        if not org_id:
            return

        action_data = _get_action_data(user_id, trigger_metadata, session_id, status=status, error_message=error_message)

        # Retrieve the stored start message info for deletion
        run_id = trigger_metadata.get('run_id')
        if run_id:
            start_msg_info = _get_start_message_info(run_id, user_id)
            if start_msg_info:
                action_data['start_message_ts'] = start_msg_info['ts']
                action_data['start_message_channel'] = start_msg_info.get('channel_id')

        # --- Email ---
        email_enabled = bool(get_org_preference(org_id, 'action_email_notifications', default=False))
        if email_enabled:
            _send_emails(org_id, user_id, 'send_action_completed_email', action_data)

        # --- Slack ---
        slack_enabled = bool(get_org_preference(org_id, 'slack_action_complete_notifications', default=True))
        if slack_enabled and _has_slack_connected(user_id):
            try:
                from utils.notifications.slack_notification_service import (
                    send_slack_action_completed_notification,
                )
                send_slack_action_completed_notification(user_id, action_data)
            except Exception:
                logger.exception("[Dispatcher] Slack action completed notification failed")

    except Exception:
        logger.exception("[Dispatcher] Error in notify_action_completed")
