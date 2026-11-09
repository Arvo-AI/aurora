"""
PagerDuty incident note: post Aurora's root cause back onto the PagerDuty
incident that triggered the RCA, once, when the investigation completes.

Notes are permanent (PagerDuty has no edit/delete endpoint), so posting is
guarded by a claim on incidents.pagerduty_note_id (see postback_claim): a
lost note is possible, a duplicate is not.

A recurrence (an incident the recurrence agent folded into an earlier anchor)
gets a short pointer note instead of the full RCA: which earlier incident it
repeats, that incident's root cause in a line or two, and a link to it.
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from routes.pagerduty.pagerduty_helpers import PagerDutyAPIError, PagerDutyClient
from utils.auth.stateless_auth import set_rls_context
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.db.connection_pool import db_pool
from utils.notifications.postback_claim import PostbackClaim
from utils.notifications.rca_note import (
    MIN_SUMMARY_CHARS,
    compose_note,
    extract_next_steps,
    extract_note_body,
    to_plain_text,
    truncate,
)

logger = logging.getLogger(__name__)

FRONTEND_URL = os.getenv("FRONTEND_URL")
_LOG = "[PagerDutyNote]"
_claims = PostbackClaim("pagerduty_note_id", _LOG)

# PagerDuty object ids are short alphanumerics; anything else must not reach the request path
_PD_ID_RE = re.compile(r"[A-Za-z0-9]{1,32}")

# Recurrence note: the anchor's root cause is cut to its first sentences
RECURRENCE_ROOT_CAUSE_SENTENCES = 2
RECURRENCE_ROOT_CAUSE_MAX_CHARS = 320
ANCHOR_TITLE_MAX_CHARS = 120
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")


def _pd_incident_id(incident_data: Dict[str, Any]) -> Optional[str]:
    """PagerDuty incident id (PXXXXXX) from alert_metadata; source_alert_id is the number."""
    meta = incident_data.get("alert_metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (ValueError, TypeError):
            return None
    if not isinstance(meta, dict):
        return None
    value = str(meta.get("incidentId") or "")
    return value if _PD_ID_RE.fullmatch(value) else None


def _should_post(incident_data: Dict[str, Any]) -> Tuple[bool, str]:
    """Pure eligibility check. Returns (ok, reason-when-not-ok)."""
    ok, reason, *_ = _eligibility(incident_data)
    return ok, reason


def _eligibility(incident_data: Dict[str, Any]) -> Tuple[bool, str, Optional[str], str, str]:
    """(ok, reason-when-not-ok, PagerDuty incident id, root cause, impact)."""
    if incident_data.get("source_type") != "pagerduty":
        return False, "not a PagerDuty incident", None, "", ""
    if incident_data.get("recurrence_of"):
        return False, f"recurrence of {incident_data['recurrence_of']}; only anchors get a note", None, "", ""
    if incident_data.get("pagerduty_note_id"):
        return False, "note already posted or in flight", None, "", ""
    pd_incident_id = _pd_incident_id(incident_data)
    if not pd_incident_id:
        return False, "no PagerDuty incident id in alert_metadata", None, "", ""
    root_cause, impact = extract_note_body(incident_data.get("aurora_summary"))
    if len(root_cause) < MIN_SUMMARY_CHARS:
        return False, "summary too short to post", pd_incident_id, root_cause, impact
    return True, "", pd_incident_id, root_cause, impact


def _disable_write_capability(user_id: str, creds: Dict[str, Any]) -> None:
    caps = {**(creds.get("capabilities") or {}), "can_write_incidents": False}
    try:
        store_tokens_in_db(user_id, {**creds, "capabilities": caps}, "pagerduty")
    except Exception:
        logger.exception("%s Failed to persist can_write_incidents=False for user %s", _LOG, user_id)


def _build_client(user_id: str, creds: Dict[str, Any]) -> Tuple[Optional[PagerDutyClient], Dict[str, Any]]:
    """Client for the stored credentials; refreshes an OAuth token first."""
    if creds.get("auth_type") == "oauth":
        from routes.pagerduty.oauth_utils import refresh_and_store_if_needed

        success, creds = refresh_and_store_if_needed(user_id, creds)
        if not success:
            logger.warning("%s OAuth token expired for user %s; note not posted", _LOG, user_id)
            return None, creds
        token_kwargs = {"oauth_token": creds.get("access_token")}
    else:
        token_kwargs = {"api_token": creds.get("api_token")}
    from_email = creds.get("external_user_email") or None
    return PagerDutyClient(from_email=from_email, **token_kwargs), creds


def _handle_post_error(e: PagerDutyAPIError, incident_id: str, user_id: str, creds: Dict[str, Any]) -> None:
    """Release the claim only when PagerDuty definitively rejected the POST (4xx); a 403 also disables the capability."""
    if e.status_code is None or e.status_code >= 500:
        # No response, or a 5xx that a gateway may have returned after PagerDuty
        # stored the note: the outcome is unknown. Keep the claim (a lost note
        # beats a duplicate one; notes cannot be deleted).
        logger.warning(
            "%s No definitive answer from PagerDuty for incident %s; claim kept to avoid a duplicate: %s",
            _LOG, incident_id, e,
        )
        return
    _claims.release(incident_id, user_id)
    if e.status_code == 403:
        _disable_write_capability(user_id, creds)
        logger.warning(
            "%s PagerDuty refused the note for incident %s (forbidden); write capability disabled",
            _LOG, incident_id,
        )
    else:
        logger.warning("%s PagerDuty rejected the note for incident %s: %s", _LOG, incident_id, e)


def _recurrence_eligibility(incident_data: Dict[str, Any]) -> Tuple[bool, str, Optional[str]]:
    """Pure checks for a folded incident: (ok, reason-when-not-ok, PagerDuty incident id).

    The note goes on the folded incident's own PagerDuty incident, never the anchor's.
    """
    if incident_data.get("source_type") != "pagerduty":
        return False, "not a PagerDuty incident", None
    if incident_data.get("pagerduty_note_id"):
        return False, "note already posted or in flight", None
    pd_incident_id = _pd_incident_id(incident_data)
    if not pd_incident_id:
        return False, "no PagerDuty incident id in alert_metadata", None
    return True, "", pd_incident_id


def _fetch_anchor(user_id: str, anchor_id: str, incident_id: str, pd_incident_id: str) -> Optional[Dict[str, Any]]:
    """The anchor's title, first-fire time and RCA, plus whether another Aurora
    incident already put a note on this PagerDuty incident (a re-triggered page
    folded onto its own anchor shares the anchor's PagerDuty incident)."""
    with db_pool.get_admin_connection() as conn, conn.cursor() as cursor:
        if not set_rls_context(cursor, conn, user_id, log_prefix=_LOG):
            return None
        cursor.execute(
            """SELECT a.alert_title, a.aurora_summary, COALESCE(a.alert_fired_at, a.started_at),
                      EXISTS (SELECT 1 FROM incidents o
                              WHERE o.id <> %s AND o.source_type = 'pagerduty'
                                AND o.pagerduty_note_id IS NOT NULL
                                AND o.alert_metadata->>'incidentId' = %s)
               FROM incidents a WHERE a.id = %s""",
            (incident_id, pd_incident_id, anchor_id),
        )
        row = cursor.fetchone()
    if not row:
        return None
    return {"title": row[0], "summary": row[1], "fired_at": row[2], "pd_incident_has_note": bool(row[3])}


def _short_root_cause(summary: Optional[str]) -> str:
    """The anchor's root-cause paragraph cut to its first sentences."""
    # extract_note_body already drops stubs under MIN_PROSE_CHARS; a short factual cause stays.
    root_cause, _ = extract_note_body(summary)
    if not root_cause:
        return ""
    sentences = _SENTENCE_END_RE.split(root_cause)
    return truncate(" ".join(sentences[:RECURRENCE_ROOT_CAUSE_SENTENCES]), RECURRENCE_ROOT_CAUSE_MAX_CHARS)


def compose_recurrence_note(
    anchor_id: str,
    anchor_title: Optional[str],
    anchor_fired_at: Optional[datetime],
    root_cause: str,
    base_url: Optional[str] = None,
) -> str:
    """Plain-text note for a folded incident: what it repeats, the root cause then, and a link."""
    title = truncate(to_plain_text(anchor_title or "", max_chars=None).replace("\n", " "), ANCHOR_TITLE_MAX_CHARS)
    if isinstance(anchor_fired_at, datetime) and anchor_fired_at.tzinfo is not None:
        # The column is naive UTC; an aware value from a direct caller is converted to match the label.
        anchor_fired_at = anchor_fired_at.astimezone(timezone.utc)
    when = f" ({anchor_fired_at:%Y-%m-%d %H:%M} UTC)" if isinstance(anchor_fired_at, datetime) else ""
    lines = [
        "Aurora RCA: recurrence",
        "",
        f'Recurrence of "{title or "an earlier incident"}"{when}.',
        f"Root cause then: {root_cause}" if root_cause else "Root cause then: not determined.",
        "",
    ]
    base_url = (base_url or "").rstrip("/")
    if base_url:
        lines += [f"Earlier investigation: {base_url}/incidents/{anchor_id}", ""]
    lines.append("Generated automatically by Aurora. Verify before acting.")
    return "\n".join(lines)


def _credentials_client(user_id: str, incident_id: str) -> Tuple[Optional[PagerDutyClient], Dict[str, Any]]:
    """A client for credentials that may write incidents, else (None, creds)."""
    creds = get_token_data(user_id, "pagerduty")
    if not creds:
        logger.info("%s Skipping incident %s: PagerDuty not connected", _LOG, incident_id)
        return None, {}
    if (creds.get("capabilities") or {}).get("can_write_incidents") is not True:
        logger.info("%s Skipping incident %s: credentials cannot write incidents", _LOG, incident_id)
        return None, creds
    return _build_client(user_id, creds)


def _claim_and_post(
    user_id: str, incident_id: str, pd_incident_id: str, content: str,
    client: PagerDutyClient, creds: Dict[str, Any], shared: bool = False,
) -> bool:
    """Claim the incident's note slot, post, record the note id. One note per Aurora incident;
    with shared=True also one per PagerDuty incident (folded recurrences can share one)."""
    claimed = (
        _claims.claim_shared(incident_id, user_id, pd_incident_id) if shared
        else _claims.claim(incident_id, user_id)
    )
    if not claimed:
        logger.info("%s Incident %s already has a note posted or in flight", _LOG, incident_id)
        return False
    try:
        response = client.create_note(pd_incident_id, content)
    except PagerDutyAPIError as e:
        _handle_post_error(e, incident_id, user_id, creds)
        return False
    note_id = ((response or {}).get("note") or {}).get("id") or "posted"
    _claims.record(incident_id, user_id, note_id)
    logger.info("%s Posted note %s on PagerDuty incident %s for %s", _LOG, note_id, pd_incident_id, incident_id)
    return True


def _send_recurrence_note(user_id: str, incident_data: Dict[str, Any]) -> bool:
    """Short note on a folded incident's own PagerDuty incident, pointing at its anchor."""
    incident_id = incident_data.get("incident_id")
    anchor_id = incident_data["recurrence_of"]
    ok, reason, pd_incident_id = _recurrence_eligibility(incident_data)
    if not ok:
        logger.info("%s Skipping recurrence %s: %s", _LOG, incident_id, reason)
        return False

    client, creds = _credentials_client(user_id, incident_id)
    if client is None:
        return False

    anchor = _fetch_anchor(user_id, anchor_id, incident_id, pd_incident_id)
    if anchor is None:
        logger.info("%s Skipping recurrence %s: anchor %s not found", _LOG, incident_id, anchor_id)
        return False
    if anchor["pd_incident_has_note"]:
        logger.info(
            "%s Skipping recurrence %s: PagerDuty incident %s already has an Aurora note",
            _LOG, incident_id, pd_incident_id,
        )
        return False

    content = compose_recurrence_note(
        anchor_id, anchor["title"] or incident_data.get("anchor_alert_title"),
        anchor["fired_at"], _short_root_cause(anchor["summary"]), FRONTEND_URL,
    )
    return _claim_and_post(user_id, incident_id, pd_incident_id, content, client, creds, shared=True)


def send_pagerduty_incident_note(user_id: str, incident_data: Dict[str, Any]) -> bool:
    """Post the RCA root cause as a note on the originating PagerDuty incident.

    A folded recurrence gets the short recurrence note instead. Returns True
    only when a note was posted. Never raises.
    """
    incident_id = incident_data.get("incident_id")
    try:
        if incident_data.get("recurrence_of"):
            return _send_recurrence_note(user_id, incident_data)

        ok, reason, pd_incident_id, root_cause, impact = _eligibility(incident_data)
        if not ok:
            logger.info("%s Skipping incident %s: %s", _LOG, incident_id, reason)
            return False

        client, creds = _credentials_client(user_id, incident_id)
        if client is None:
            return False

        next_steps = extract_next_steps(incident_data.get("aurora_summary"))
        content = compose_note(root_cause, incident_id, impact, FRONTEND_URL, next_steps)
        return _claim_and_post(user_id, incident_id, pd_incident_id, content, client, creds)
    except Exception:
        logger.exception("%s Failed to post note for incident %s", _LOG, incident_id)
        return False
