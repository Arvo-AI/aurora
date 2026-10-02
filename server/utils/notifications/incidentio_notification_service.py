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
from typing import Any, Dict, NamedTuple, Optional, Tuple

from routes.incidentio.incidentio_client import IncidentioAPIError, IncidentioClient
from utils.auth.stateless_auth import set_rls_context
from utils.auth.token_management import get_token_data
from utils.db.connection_pool import db_pool
from utils.notifications.postback_claim import PostbackClaim
from utils.notifications.rca_note import MIN_SUMMARY_CHARS, compose_note_markdown, extract_note_body

logger = logging.getLogger(__name__)

FRONTEND_URL = os.getenv("FRONTEND_URL")
_LOG = "[IncidentioUpdate]"
# incident.io status categories. An alert can stay attached to an incident nobody
# will read any more; among the rest, prefer the one still being worked on.
_CLOSED_CATEGORIES = frozenset(("declined", "merged", "canceled"))
_OPEN_CATEGORY_ORDER = {"live": 0, "triage": 1, "paused": 2, "learning": 3, "closed": 4}

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


class _Source(NamedTuple):
    """What the DB knows about the Aurora incident's origin and its recurrence anchor."""
    object_id: Optional[str]          # incident.io incident id (incident events) or alert id (alert events)
    is_alert: bool
    anchor_alert_id: Optional[str]    # the anchor's incident.io object id, when this is a recurrence
    anchor_update_id: Optional[str]   # the anchor's claim: set once it posted or is posting


def _read_source(incident_data: Dict[str, Any], user_id: str) -> Optional[_Source]:
    """One query: the webhook event this incident was created from, plus the anchor's
    event and claim when the incident is a folded recurrence.

    The stored id is the incident.io incident id for incident events and the alert
    id for alert events; the event family is read off the stored payload with the
    same parser the webhook uses.
    """
    from routes.incidentio.tasks import _extract_incident_fields

    with db_pool.get_admin_connection() as conn, conn.cursor() as cursor:
        if not set_rls_context(cursor, conn, user_id, log_prefix=_LOG):
            raise RuntimeError(f"cannot resolve org for user {user_id}")
        cursor.execute(
            "SELECT a.incident_id, a.payload, anc_a.incident_id, anc.incidentio_update_id "
            "FROM incidentio_alerts a "
            "LEFT JOIN incidents anc ON anc.id = %s "
            "LEFT JOIN incidentio_alerts anc_a ON anc_a.id = anc.source_alert_id "
            "WHERE a.id = %s",
            (incident_data.get("recurrence_of"), incident_data["source_alert_id"]),
        )
        row = cursor.fetchone()
    if not row:
        return None
    payload = row[1]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            payload = {}
    fields = _extract_incident_fields(payload if isinstance(payload, dict) else {})
    object_id = row[0] or fields.get("incident_id")
    return _Source(
        object_id=str(object_id) if object_id else None,
        is_alert=bool(fields.get("is_alert")),
        anchor_alert_id=str(row[2]) if row[2] else None,
        anchor_update_id=row[3],
    )


def _target_rank(incident: Dict[str, Any]) -> Tuple[int, int, str]:
    """Sort key when an alert is attached to several open incidents: the one still being
    worked on first (live, then triage, paused, learning, closed), oldest first within a
    category. Deterministic, so the same alert always lands on the same incident."""
    category = str(incident.get("status_category") or "")
    rank = _OPEN_CATEGORY_ORDER.get(category, len(_OPEN_CATEGORY_ORDER))
    external_id = incident.get("external_id")
    return rank, (int(external_id) if isinstance(external_id, int) else 1 << 62), str(incident.get("id"))


def _target_incident(client: IncidentioClient, source: _Source) -> Tuple[Optional[str], str]:
    """(incident.io incident id to post to, reason-when-none)."""
    if not source.is_alert:
        return source.object_id, ""
    links = (client.list_incident_alerts(source.object_id) or {}).get("incident_alerts") or []
    candidates = [
        link["incident"] for link in links
        if (link.get("incident") or {}).get("id")
        and link["incident"].get("status_category") not in _CLOSED_CATEGORIES
    ]
    if not candidates:
        return None, "alert is not attached to an open incident (escalation only)"
    return str(min(candidates, key=_target_rank)["id"]), ""


def _anchor_covers_target(client: IncidentioClient, source: _Source, target: str) -> bool:
    """True when this alert-triggered recurrence's anchor already posted (or is posting)
    to the same incident.io incident, i.e. the anchor's alert is attached to it too."""
    if not source.anchor_alert_id or not source.anchor_update_id:
        return False
    links = (client.list_incident_alerts(incident_id=target) or {}).get("incident_alerts") or []
    return any(str((link.get("alert") or {}).get("id")) == source.anchor_alert_id for link in links)


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
    source = _read_source(incident_data, user_id)
    if not source or not source.object_id:
        return None, "source event has no incident.io id"
    try:
        target, reason = _target_incident(client, source)
        if target and source.is_alert and _anchor_covers_target(client, source, target):
            return None, "an earlier alert of this recurrence group already posted to that incident"
        return target, reason
    except IncidentioAPIError as e:
        return None, f"could not resolve the incident for alert {source.object_id}: {e.code}"


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
