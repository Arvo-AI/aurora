"""
incident.io RCA post-back: post Aurora's root cause back onto the incident.io
object that triggered the RCA, once, when the investigation completes.

An incident-attached RCA posts an *incident update*, which goes through
incident.io's own notification flow (incident channel, followers, app
notifications). An alert that only escalated has no incident timeline, so the
root cause goes on the alert itself as an *alert note*, which incident.io shows
in the alert timeline and in the alert's Slack pulse thread. Either way
responders see the cause where they were already paged. An incident-triggered
RCA with no incident id has nowhere to post and is skipped.

Recurrences post too, unlike PagerDuty notes: an incident.io object always maps
onto the same Aurora incident (the ingest upserts on it), so a folded
recurrence is always a different incident.io incident or alert with its own
responders. The one exception is an alert storm: when several alerts of one
recurrence group are attached to the same incident.io incident, only the
first RCA is posted there.

Posting is guarded by a claim on incidents.incidentio_update_id (see
postback_claim). Incident updates also carry an idempotency key, so a repeat
that does reach incident.io is a no-op there as well; /v1/alert_notes has no
such key, which leaves the claim as the only guard for notes.
"""

import json
import logging
import os
from typing import Any, Dict, NamedTuple, Optional, Tuple

from routes.incidentio.incidentio_client import IncidentioAPIError, IncidentioClient
from utils.auth.stateless_auth import set_rls_context
from utils.auth.token_management import get_token_data
from utils.db.connection_pool import db_pool
from utils.notifications.postback_claim import PENDING, PostbackClaim
from utils.notifications.rca_note import MIN_SUMMARY_CHARS, compose_note_markdown, extract_note_body

logger = logging.getLogger(__name__)

FRONTEND_URL = os.getenv("FRONTEND_URL")
_LOG = "[IncidentioUpdate]"
# declined/merged/canceled are dead ends, so they don't count as an attachment and
# the RCA falls through to an alert note. A closed incident still has a timeline,
# so it stays a target — just the last choice after live, triage, paused, learning.
_CLOSED_CATEGORIES = frozenset(("declined", "merged", "canceled"))
_OPEN_CATEGORY_ORDER = {"live": 0, "triage": 1, "paused": 2, "learning": 3, "closed": 4}
# Note claims share incidentio_update_id with incident updates. The prefix marks
# both the in-flight token and the recorded id, so a note is never read as
# coverage of an incident timeline.
_NOTE_ID_PREFIX = "note:"
_NOTE_PENDING = f"{_NOTE_ID_PREFIX}pending"

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


class _Target(NamedTuple):
    """Where the root cause goes: an incident.io incident timeline, or an alert's own notes."""
    object_id: str
    is_alert_note: bool


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


def _attached_incident(client: IncidentioClient, alert_id: str) -> Optional[str]:
    """The open incident.io incident this alert is attached to, or None when it only escalated."""
    links = (client.list_incident_alerts(alert_id) or {}).get("incident_alerts") or []
    candidates = [
        link["incident"] for link in links
        if (link.get("incident") or {}).get("id")
        and link["incident"].get("status_category") not in _CLOSED_CATEGORIES
    ]
    if not candidates:
        return None
    return str(min(candidates, key=_target_rank)["id"])


def _anchor_covers_target(client: IncidentioClient, source: _Source, target: str) -> bool:
    """True when this alert-triggered recurrence's anchor already posted (or is posting)
    to the same incident.io incident, i.e. the anchor's alert is attached to it too."""
    if not source.anchor_alert_id or not source.anchor_update_id:
        return False
    # A note, posted or still in flight, reached no incident timeline — including
    # this one, even once the anchor's alert gets attached to it.
    if source.anchor_update_id.startswith(_NOTE_ID_PREFIX):
        return False
    links = (client.list_incident_alerts(incident_id=target) or {}).get("incident_alerts") or []
    return any(str((link.get("alert") or {}).get("id")) == source.anchor_alert_id for link in links)


def _handle_post_error(e: IncidentioAPIError, incident_id: str, user_id: str, *, is_alert_note: bool) -> None:
    """Release the claim only when incident.io definitively rejected the POST (4xx)."""
    if e.status_code is None or e.status_code >= 500:
        # No response, or a 5xx a gateway may have returned after incident.io
        # stored the post-back: the outcome is unknown. Keep the claim.
        logger.warning(
            "%s No definitive answer from incident.io for incident %s; claim kept to avoid a duplicate: %s",
            _LOG, incident_id, e.code,
        )
        return
    # Release the same token we claimed; a note's token is not bare 'pending'
    _claims.release(incident_id, user_id, _NOTE_PENDING if is_alert_note else PENDING)
    if e.status_code in (401, 403):
        # Name the permission the key is missing so the fix is obvious from the log alone.
        needed = (
            "the 'alerts.edit' scope to post alert notes (optional: incident updates still post without it)"
            if is_alert_note
            else "the 'Create incident updates' permission for RCA post-back"
        )
        logger.warning(
            "%s incident.io refused the post-back for incident %s (%s): the API key needs %s",
            _LOG, incident_id, e.code, needed,
        )
    else:
        logger.warning(
            "%s incident.io rejected the post-back for incident %s: HTTP %s", _LOG, incident_id, e.status_code
        )


def _resolve_target(client: IncidentioClient, incident_data: Dict[str, Any], user_id: str) -> Tuple[Optional[_Target], str]:
    """(where to post this Aurora incident's root cause, reason-when-nowhere)."""
    source = _read_source(incident_data, user_id)
    if not source or not source.object_id:
        return None, "source event has no incident.io id"
    # An incident event already names its incident: post the update straight to it.
    if not source.is_alert:
        return _Target(source.object_id, is_alert_note=False), ""
    try:
        attached = _attached_incident(client, source.object_id)
        # An alert storm folded onto one incident.io incident: the anchor's update covers it.
        if attached and _anchor_covers_target(client, source, attached):
            return None, "an earlier alert of this recurrence group already posted to that incident"
        if attached:
            return _Target(attached, is_alert_note=False), ""
        # Escalation-only alert: no incident timeline, so the note goes on the alert itself.
        # No recurrence folding needed — the ingest upserts one Aurora incident per alert, so
        # the per-incident claim is already 1:1 with the alert whose timeline the note lands on.
        return _Target(source.object_id, is_alert_note=True), ""
    except IncidentioAPIError as e:
        return None, f"could not resolve the incident for alert {source.object_id}: {e.code}"


def send_incidentio_incident_update(user_id: str, incident_data: Dict[str, Any]) -> bool:
    """Post the RCA root cause onto the originating incident.io incident or alert.

    Returns True only when something was posted. Never raises.
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

        # Claimed before the POST: /v1/alert_notes has no idempotency key, so for notes
        # this claim is the only thing standing between a retry and a duplicate.
        # Notes use a tagged token so 'pending' cannot be read as an incident update.
        token = _NOTE_PENDING if target.is_alert_note else PENDING
        if not _claims.claim(incident_id, user_id, token):
            logger.info("%s Incident %s already has a post-back posted or in flight", _LOG, incident_id)
            return False

        try:
            if target.is_alert_note:
                response = client.post_alert_note(target.object_id, content)
                note_id = ((response or {}).get("alert_note") or {}).get("id") or "posted"
                # Tagged: a recurrence must not read this as coverage of an incident timeline
                posted_id = f"{_NOTE_ID_PREFIX}{note_id}"
            else:
                response = client.post_incident_update(
                    target.object_id, content, idempotency_key=f"aurora-rca-{incident_id}"
                )
                posted_id = ((response or {}).get("incident_update") or {}).get("id") or "posted"
        except IncidentioAPIError as e:
            _handle_post_error(e, incident_id, user_id, is_alert_note=target.is_alert_note)
            return False

        _claims.record(incident_id, user_id, posted_id)
        kind, where = ("note", "alert") if target.is_alert_note else ("update", "incident")
        logger.info(
            "%s Posted %s %s on incident.io %s %s for %s",
            _LOG, kind, posted_id, where, target.object_id, incident_id,
        )
        return True
    except Exception:
        logger.exception("%s Failed to post the RCA for incident %s", _LOG, incident_id)
        return False
