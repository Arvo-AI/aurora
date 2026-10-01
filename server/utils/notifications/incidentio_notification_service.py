"""
incident.io incident update: post Aurora's root cause back onto the
incident.io incident that triggered the RCA, once, when the investigation
completes.

The update goes through incident.io's own notification flow (incident channel,
followers, app notifications), so responders see the cause where they were
already paged instead of in a separate Aurora ping. Alert-triggered RCAs are
posted to the incident the alert is attached to; an alert that only escalated
has no incident timeline and is skipped.

Recurrences post too, unlike PagerDuty notes: an incident.io object always maps
onto the same Aurora incident (the ingest upserts on it), so a folded
recurrence is always a different incident.io incident or alert with its own
responders. The one exception is an alert storm: when several alerts of one
recurrence group are attached to the same incident.io incident, only the
first RCA is posted there.

Posting is guarded by a claim on incidents.incidentio_update_id (see
postback_claim) and the POST carries an idempotency key, so a repeat that
does reach incident.io is a no-op there as well.
"""

import json
import logging
import os
from typing import Any, Dict, Optional, Tuple

from routes.incidentio.incidentio_client import IncidentioAPIError, IncidentioClient
from utils.auth.stateless_auth import set_rls_context
from utils.auth.token_management import get_token_data
from utils.db.connection_pool import db_pool
from utils.notifications.postback_claim import PostbackClaim
from utils.notifications.rca_note import MIN_SUMMARY_CHARS, compose_note_markdown, extract_note_body

logger = logging.getLogger(__name__)

FRONTEND_URL = os.getenv("FRONTEND_URL")
_LOG = "[IncidentioUpdate]"
# An alert can stay attached to an incident nobody will read any more
_CLOSED_CATEGORIES = frozenset(("declined", "merged", "canceled"))

_claims = PostbackClaim("incidentio_update_id", _LOG)


def _eligibility(incident_data: Dict[str, Any]) -> Tuple[bool, str, str, str]:
    """(ok, reason-when-not-ok, root cause, impact). Pure: no DB, no network."""
    if incident_data.get("source_type") != "incidentio":
        return False, "not an incident.io incident", "", ""
    if incident_data.get("incidentio_update_id"):
        return False, "update already posted or in flight", "", ""
    if not incident_data.get("source_alert_id"):
        return False, "no source event to resolve the incident.io incident from", "", ""
    root_cause, impact = extract_note_body(incident_data.get("aurora_summary"))
    if len(root_cause) < MIN_SUMMARY_CHARS:
        return False, "summary too short to post", root_cause, impact
    return True, "", root_cause, impact


def _source_event(source_alert_id: Any, user_id: str) -> Tuple[Optional[str], bool]:
    """(incident.io object id, is_alert) from the webhook event that created the incident.

    The stored id is the incident.io incident id for incident events and the
    alert id for alert events; the event family is read off the stored payload
    with the same parser the webhook uses.
    """
    from routes.incidentio.tasks import _extract_incident_fields

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            if not set_rls_context(cursor, conn, user_id, log_prefix=_LOG):
                raise RuntimeError(f"cannot resolve org for user {user_id}")
            cursor.execute(
                "SELECT incident_id, payload FROM incidentio_alerts WHERE id = %s",
                (source_alert_id,),
            )
            row = cursor.fetchone()
    if not row:
        return None, False
    payload = row[1]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            payload = {}
    fields = _extract_incident_fields(payload if isinstance(payload, dict) else {})
    object_id = row[0] or fields.get("incident_id")
    return (str(object_id) if object_id else None), bool(fields.get("is_alert"))


def _target_incident(client: IncidentioClient, object_id: str, is_alert: bool) -> Tuple[Optional[str], str]:
    """(incident.io incident id to post to, reason-when-none)."""
    if not is_alert:
        return object_id, ""
    links = (client.list_incident_alerts(object_id) or {}).get("incident_alerts") or []
    for link in links:
        incident = link.get("incident") or {}
        if incident.get("id") and incident.get("status_category") not in _CLOSED_CATEGORIES:
            return str(incident["id"]), ""
    return None, "alert is not attached to an open incident (escalation only)"


def _anchor_covers_target(
    client: IncidentioClient, incident_data: Dict[str, Any], target: str, user_id: str
) -> bool:
    """True when this alert-triggered recurrence's anchor already posted (or is posting)
    to the same incident.io incident, i.e. the anchor's alert is attached to it too."""
    anchor_id = incident_data.get("recurrence_of")
    if not anchor_id:
        return False
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            if not set_rls_context(cursor, conn, user_id, log_prefix=_LOG):
                raise RuntimeError(f"cannot resolve org for user {user_id}")
            cursor.execute(
                "SELECT a.incident_id, i.incidentio_update_id FROM incidents i "
                "JOIN incidentio_alerts a ON a.id = i.source_alert_id WHERE i.id = %s",
                (anchor_id,),
            )
            row = cursor.fetchone()
    if not row or not row[0] or not row[1]:
        return False
    anchor_alert_id = str(row[0])
    links = (client.list_incident_alerts(incident_id=target) or {}).get("incident_alerts") or []
    return any(str((link.get("alert") or {}).get("id")) == anchor_alert_id for link in links)


def _handle_post_error(e: IncidentioAPIError, incident_id: str, user_id: str) -> None:
    """Release the claim only when incident.io definitively rejected the POST (4xx)."""
    if e.status_code is None or e.status_code >= 500:
        # No response, or a 5xx a gateway may have returned after incident.io
        # stored the update: the outcome is unknown. Keep the claim.
        logger.warning(
            "%s No definitive answer from incident.io for incident %s; claim kept to avoid a duplicate: %s",
            _LOG, incident_id, e.code,
        )
        return
    _claims.release(incident_id, user_id)
    if e.status_code in (401, 403):
        logger.warning(
            "%s incident.io refused the update for incident %s (%s): the API key needs the "
            "'Create incident updates' permission for RCA post-back",
            _LOG, incident_id, e.code,
        )
    else:
        logger.warning("%s incident.io rejected the update for incident %s: HTTP %s", _LOG, incident_id, e.status_code)


def _resolve_target(client: IncidentioClient, incident_data: Dict[str, Any], user_id: str) -> Tuple[Optional[str], str]:
    """(incident.io incident id to post to, reason-when-none) for this Aurora incident."""
    object_id, is_alert = _source_event(incident_data["source_alert_id"], user_id)
    if not object_id:
        return None, "source event has no incident.io id"
    try:
        target, reason = _target_incident(client, object_id, is_alert)
        if target and is_alert and _anchor_covers_target(client, incident_data, target, user_id):
            return None, "an earlier alert of this recurrence group already posted to that incident"
        return target, reason
    except IncidentioAPIError as e:
        return None, f"could not resolve the incident for alert {object_id}: {e.code}"


def send_incidentio_incident_update(user_id: str, incident_data: Dict[str, Any]) -> bool:
    """Post the RCA root cause as an update on the originating incident.io incident.

    Returns True only when an update was posted. Never raises.
    """
    incident_id = incident_data.get("incident_id")
    try:
        ok, reason, root_cause, impact = _eligibility(incident_data)
        if not ok:
            logger.info("%s Skipping incident %s: %s", _LOG, incident_id, reason)
            return False

        creds = get_token_data(user_id, "incidentio")
        if not creds or not creds.get("api_key"):
            logger.info("%s Skipping incident %s: incident.io not connected", _LOG, incident_id)
            return False
        client = IncidentioClient(creds["api_key"])

        target, reason = _resolve_target(client, incident_data, user_id)
        if not target:
            logger.info("%s Skipping incident %s: %s", _LOG, incident_id, reason)
            return False

        content = compose_note_markdown(root_cause, incident_id, impact, FRONTEND_URL)

        if not _claims.claim(incident_id, user_id):
            logger.info("%s Incident %s already has an update posted or in flight", _LOG, incident_id)
            return False

        try:
            response = client.post_incident_update(
                target, content, idempotency_key=f"aurora-rca-{incident_id}"
            )
        except IncidentioAPIError as e:
            _handle_post_error(e, incident_id, user_id)
            return False

        update_id = ((response or {}).get("incident_update") or {}).get("id") or "posted"
        _claims.record(incident_id, user_id, update_id)
        logger.info("%s Posted update %s on incident.io incident %s for %s", _LOG, update_id, target, incident_id)
        return True
    except Exception:
        logger.exception("%s Failed to post update for incident %s", _LOG, incident_id)
        return False
