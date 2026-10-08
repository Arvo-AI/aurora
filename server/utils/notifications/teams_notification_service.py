"""Microsoft Teams incident and action notifications (posted as the Aurora bot)."""

from __future__ import annotations

import html
import logging
import os
import re
from typing import Any, Dict, Optional, Tuple

from connectors.teams_connector.bot_client import (
    TeamsBotError,
    send_message_to_team_channel,
    tenant_id_for_aurora_user,
)
from services.channels import prefs as channel_prefs
from utils.db.connection_pool import db_pool
from utils.auth.stateless_auth import set_rls_context
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)
FRONTEND_URL = os.getenv("FRONTEND_URL")
_PROVIDER = "teams"


def _escape(text: Any) -> str:
    return html.escape(str(text or ""), quote=True)


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def _incident_url(incident_id: str) -> str:
    return f"{FRONTEND_URL}/incidents/{incident_id}"


def _resolve_card_target(user_id: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    tenant_id = tenant_id_for_aurora_user(user_id)
    if not tenant_id:
        return None, None, None
    channel_id = channel_prefs.get_incidents_channel_id(user_id, _PROVIDER)
    if not channel_id:
        logger.error("[TeamsNotification] No incidents channel for user %s", sanitize(user_id))
        return tenant_id, None, None
    team_id = _team_id_for_channel(user_id, channel_id)
    return tenant_id, team_id, channel_id


def _team_id_for_channel(user_id: str, channel_id: str) -> Optional[str]:
    try:
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[TeamsNotification:team]")
            cur.execute(
                """SELECT team_id FROM slack_channels
                   WHERE provider = %s AND channel_id = %s LIMIT 1""",
                (_PROVIDER, channel_id),
            )
            row = cur.fetchone()
            return row[0] if row and row[0] else None
    except Exception:
        logger.debug("[TeamsNotification] team_id lookup failed", exc_info=True)
        return None


def _post_html(tenant_id: str, team_id: str, channel_id: str, title: str, body_html: str) -> bool:
    content = f"<h3>{_escape(title)}</h3>{body_html}"
    try:
        send_message_to_team_channel(
            tenant_id=tenant_id,
            team_id=team_id,
            channel_id=channel_id,
            text=content,
            text_format="html",
        )
        return True
    except TeamsBotError:
        logger.exception("[TeamsNotification] bot post failed for channel %s", sanitize(channel_id))
        return False


def send_teams_investigation_started_notification(user_id: str, incident_data: Dict[str, Any]) -> bool:
    tenant_id, team_id, channel_id = _resolve_card_target(user_id)
    if not tenant_id or not team_id or not channel_id:
        return False
    incident_id = incident_data.get("incident_id", "unknown")
    title = incident_data.get("alert_title") or "Investigation"
    severity = incident_data.get("severity") or "unknown"
    service = incident_data.get("service") or "unknown"
    source = incident_data.get("source_type") or "monitoring"
    url = _incident_url(incident_id)
    body = (
        f"<p><strong>Alert:</strong> {_escape(title)}<br/>"
        f"<strong>Severity:</strong> {_escape(severity)}<br/>"
        f"<strong>Service:</strong> {_escape(service)}<br/>"
        f"<strong>Status:</strong> In progress<br/>"
        f"Aurora is analyzing this incident from {_escape(source)}.</p>"
        f'<p><a href="{_escape(url)}">View investigation</a></p>'
    )
    return _post_html(tenant_id, team_id, channel_id, "Investigation Started", body)


def send_teams_investigation_completed_notification(
    user_id: str,
    incident_data: Dict[str, Any],
    *,
    post_primary_card: bool = True,
) -> bool:
    if not post_primary_card:
        return True
    tenant_id, team_id, channel_id = _resolve_card_target(user_id)
    if not tenant_id or not team_id or not channel_id:
        return False
    incident_id = incident_data.get("incident_id", "unknown")
    title = incident_data.get("alert_title") or "Investigation complete"
    summary = _strip_html(incident_data.get("summary") or "")[:1500]
    url = _incident_url(incident_id)
    body = f"<p><strong>{_escape(title)}</strong></p>"
    if summary:
        body += f"<p>{_escape(summary)}</p>"
    body += f'<p><a href="{_escape(url)}">View full report</a></p>'
    return _post_html(tenant_id, team_id, channel_id, "Analysis Complete", body)


def send_teams_investigation_failed_notification(
    user_id: str,
    incident_data: Dict[str, Any],
    error_message: Optional[str] = None,
) -> bool:
    tenant_id, team_id, channel_id = _resolve_card_target(user_id)
    if not tenant_id or not team_id or not channel_id:
        return False
    incident_id = incident_data.get("incident_id", "unknown")
    title = incident_data.get("alert_title") or "Investigation failed"
    url = _incident_url(incident_id)
    err = (error_message or "Unknown error")[:500]
    body = (
        f"<p><strong>{_escape(title)}</strong></p>"
        f"<p><strong>Error:</strong> {_escape(err)}</p>"
        f'<p><a href="{_escape(url)}">View incident</a></p>'
    )
    return _post_html(tenant_id, team_id, channel_id, "Investigation Failed", body)


def send_teams_action_started_notification(user_id: str, action_data: Dict[str, Any]) -> Optional[dict]:
    tenant_id, team_id, channel_id = _resolve_card_target(user_id)
    if not tenant_id or not team_id or not channel_id:
        return None
    name = action_data.get("action_name") or "Action"
    body = f"<p>Action <strong>{_escape(name)}</strong> is running.</p>"
    try:
        result = send_message_to_team_channel(
            tenant_id=tenant_id,
            team_id=team_id,
            channel_id=channel_id,
            text=f"<h3>Action Started</h3>{body}",
            text_format="html",
        )
        msg_id = result.get("id")
        if msg_id:
            return {"id": msg_id, "channel_id": channel_id, "team_id": team_id}
    except TeamsBotError:
        logger.exception("[TeamsNotification] action started post failed")
    return None


def send_teams_action_completed_notification(user_id: str, action_data: Dict[str, Any]) -> bool:
    tenant_id, team_id, channel_id = _resolve_card_target(user_id)
    if not tenant_id or not team_id or not channel_id:
        return False
    name = action_data.get("action_name") or "Action"
    status = action_data.get("status") or "unknown"
    status_label = "Completed successfully" if status == "success" else "Failed"
    err = action_data.get("error")
    body = f"<p><strong>{_escape(name)}</strong> — {_escape(status_label)}</p>"
    if err:
        body += f"<p>{_escape(str(err)[:300])}</p>"
    session_id = action_data.get("session_id")
    if session_id:
        body += f'<p><a href="{_escape(FRONTEND_URL)}/chat?sessionId={_escape(session_id)}">View session</a></p>'
    return _post_html(tenant_id, team_id, channel_id, "Action Complete", body)
