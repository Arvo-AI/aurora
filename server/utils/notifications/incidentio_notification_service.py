"""
incident.io RCA post-back: one root cause on the incident timeline, or as an
alert note when the alert never attached. Grouped alerts share one pulse
thread: the first RCA is the group note, later alerts get a short correlation
note, and the group note is edited when a later RCA adds a new finding.
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
    """Where the root cause goes: an incident timeline, one alert, or an alert group."""
    object_id: str
    is_alert_note: bool
    # Set when the full RCA goes on the group's shared thread.
    alert_group_id: Optional[str] = None
    # A later alert in that group: short note on the alert, and maybe an edit of the group note.
    correlation: bool = False
    group_note_id: Optional[str] = None
    anchor_incident_id: Optional[str] = None


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


def _alert_group_ids(client: IncidentioClient, alert_id: str) -> list:
    """Sorted group ids for this alert. Empty when it was never grouped."""
    payload = client.get_alert(alert_id) or {}
    alert = payload.get("alert") if isinstance(payload, dict) else None
    # Some responses are the alert object itself rather than {"alert": ...}.
    if not isinstance(alert, dict):
        alert = payload if isinstance(payload, dict) else {}
    raw = alert.get("alert_group_ids") or []
    if not isinstance(raw, list):
        return []
    return sorted({str(group_id) for group_id in raw if group_id})


def _anchor_covers_group(client: IncidentioClient, source: _Source, group_ids: set) -> bool:
    """True when the anchor already posted (or is posting) a note on a shared group.

    An incident-update claim does not cover the group's pulse thread.
    """
    if not source.anchor_alert_id or not source.anchor_update_id:
        return False
    if not str(source.anchor_update_id).startswith(_NOTE_ID_PREFIX):
        return False
    try:
        anchor_groups = set(_alert_group_ids(client, source.anchor_alert_id))
    except IncidentioAPIError:
        # Can't confirm the shared thread, so don't drop this RCA.
        return False
    return bool(group_ids & anchor_groups)


def _recorded_note_id(claim: Optional[str]) -> Optional[str]:
    """Alert-note id stored on the anchor, or None while that post is still in flight."""
    if not claim or not str(claim).startswith(_NOTE_ID_PREFIX):
        return None
    note_id = str(claim)[len(_NOTE_ID_PREFIX):]
    # "pending" and "posted" are claim tokens, not incident.io ids.
    if not note_id or note_id in ("pending", "posted"):
        return None
    return note_id


def _investigation_link(incident_id: Optional[str]) -> str:
    base = (FRONTEND_URL or "").rstrip("/")
    return f"{base}/incidents/{incident_id}" if base and incident_id else ""


def _correlation_note(incident_id: str, anchor_incident_id: Optional[str]) -> str:
    """Short note on a later alert: same issue, the full RCA lives on the group."""
    lines = [
        "**Correlated alert**",
        "",
        "Same issue as an earlier alert in this group. The root cause is on the group note.",
        "",
    ]
    own = _investigation_link(incident_id)
    if own:
        lines.append(f"- [Open this investigation]({own})")
    earlier = _investigation_link(anchor_incident_id)
    if earlier:
        lines.append(f"- [Open the first investigation]({earlier})")
    if own or earlier:
        lines.append("")
    lines.append("_Generated automatically by Aurora. Verify before acting._")
    return "\n".join(lines)


def _contains_finding(note: str, text: str) -> bool:
    if not text:
        return True
    folded = " ".join(note.split()).casefold()
    # The posted note swaps "*" for a look-alike so markdown doesn't italicise it.
    for variant in (text, text.replace("*", "\u2217")):
        if " ".join(variant.split()).casefold() in folded:
            return True
    return False


def _note_content(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    note = payload.get("alert_note")
    if not isinstance(note, dict):
        return ""
    content = note.get("content")
    return content if isinstance(content, str) else ""


def _append_finding(content: str, root_cause: str, impact: str, incident_id: str) -> str:
    lines = ["**Also found**", "", root_cause.replace("*", "\u2217"), ""]
    if impact:
        lines += ["**Impact**", "", impact.replace("*", "\u2217"), ""]
    link = _investigation_link(incident_id)
    if link:
        lines.append(f"- [Open this investigation]({link})")
    addition = "\n".join(lines)
    return f"{content.rstrip()}\n\n{addition}" if content.strip() else addition


def _extend_group_note(
    client: IncidentioClient, note_id: str, root_cause: str, impact: str, incident_id: str
) -> None:
    """Add this RCA to the group note when the first post didn't already say it.

    A lost race with another writer is retried once. Failure here does not
    undo the correlation note already posted on this alert.
    """
    try:
        content = _note_content(client.get_alert_note(note_id))
        # Same root cause and impact: leave the group note alone.
        if _contains_finding(content, root_cause) and _contains_finding(content, impact):
            return
        client.update_alert_note(note_id, _append_finding(content, root_cause, impact, incident_id))
        refreshed = _note_content(client.get_alert_note(note_id))
        # Another writer replaced the note between the read and the write.
        if not _contains_finding(refreshed, root_cause):
            client.update_alert_note(
                note_id, _append_finding(refreshed, root_cause, impact, incident_id)
            )
    except IncidentioAPIError:
        logger.warning("%s Could not add a later finding to group note %s", _LOG, note_id)


def _note_target(
    client: IncidentioClient, source: _Source, anchor_incident_id: Optional[str]
) -> Tuple[Optional[_Target], str]:
    """Where an unattached alert's note goes."""
    try:
        group_ids = _alert_group_ids(client, source.object_id)
    except IncidentioAPIError:
        # Group lookup failed: still post on the alert, which is the ungrouped path.
        logger.warning(
            "%s Could not read alert groups for %s; posting the note on the alert",
            _LOG, source.object_id,
        )
        return _Target(source.object_id, is_alert_note=True), ""
    # No group: each alert has its own timeline, so each recurrence posts its own note.
    if not group_ids:
        return _Target(source.object_id, is_alert_note=True), ""
    # Same pulse thread as the anchor: point at it, and extend the group note only if this RCA is new.
    if _anchor_covers_group(client, source, set(group_ids)):
        return _Target(
            source.object_id,
            is_alert_note=True,
            correlation=True,
            group_note_id=_recorded_note_id(source.anchor_update_id),
            anchor_incident_id=anchor_incident_id,
        ), ""
    # First RCA for the group. The first id is stable when an alert sits in several groups.
    return _Target(group_ids[0], is_alert_note=True, alert_group_id=group_ids[0]), ""


def _remote_id(response: Optional[Dict[str, Any]], key: str) -> str:
    """Id incident.io returned, or 'posted' when the body has none."""
    return ((response or {}).get(key) or {}).get("id") or "posted"


def _post_root_cause(
    client: IncidentioClient, target: _Target, content: str, incident_id: str, user_id: str
) -> Optional[str]:
    """Claim value to record, or None when the POST failed (the claim is already settled)."""
    try:
        # Alert notes have no idempotency key, and the id must stay distinguishable
        # from an incident update in the shared claim column.
        if target.is_alert_note:
            # A group note lands once on the shared pulse thread; otherwise on this alert.
            if target.alert_group_id:
                response = client.post_alert_note(content, alert_group_id=target.alert_group_id)
            else:
                response = client.post_alert_note(content, alert_id=target.object_id)
            note_id = _remote_id(response, "alert_note")
            return f"{_NOTE_ID_PREFIX}{note_id}"
        # Incident update: the idempotency key makes a repeat a no-op on incident.io's side
        response = client.post_incident_update(
            target.object_id, content, idempotency_key=f"aurora-rca-{incident_id}"
        )
        return _remote_id(response, "incident_update")
    except IncidentioAPIError as e:
        _handle_post_error(e, incident_id, user_id, is_alert_note=target.is_alert_note)
        return None


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
            "the Create and manage on call ressources permission to post alert notes"
            if is_alert_note
            else "the Edit incidents permission for RCA post-back"
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
        # No incident timeline. A grouped alert shares one pulse thread with the storm.
        return _note_target(client, source, incident_data.get("recurrence_of"))
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

        # A later alert in the group gets a pointer, not a second copy of the full RCA.
        content = (
            _correlation_note(incident_id, target.anchor_incident_id)
            if target.correlation
            else compose_note_markdown(root_cause, incident_id, impact, FRONTEND_URL)
        )

        # Claimed before the POST: /v1/alert_notes has no idempotency key, so for notes
        # this claim is the only thing standing between a retry and a duplicate.
        # Notes use a tagged token so 'pending' cannot be read as an incident update.
        token = _NOTE_PENDING if target.is_alert_note else PENDING
        if not _claims.claim(incident_id, user_id, token):
            logger.info("%s Incident %s already has a post-back posted or in flight", _LOG, incident_id)
            return False

        posted_id = _post_root_cause(client, target, content, incident_id, user_id)
        # None: incident.io rejected the POST, or the outcome is unknown and the claim stays
        if not posted_id:
            return False

        _claims.record(incident_id, user_id, posted_id)
        # The correlation note is already stored. Editing the group note is best-effort.
        if target.group_note_id:
            _extend_group_note(client, target.group_note_id, root_cause, impact, incident_id)
        if target.alert_group_id:
            kind, where = "note", "alert group"
        elif target.is_alert_note:
            kind, where = "note", "alert"
        else:
            kind, where = "update", "incident"
        logger.info(
            "%s Posted %s %s on incident.io %s %s for %s",
            _LOG, kind, posted_id, where, target.object_id, incident_id,
        )
        return True
    except Exception:
        logger.exception("%s Failed to post the RCA for incident %s", _LOG, incident_id)
        return False
