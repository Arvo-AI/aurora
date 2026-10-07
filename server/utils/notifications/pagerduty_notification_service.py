"""
PagerDuty incident note: post Aurora's root cause back onto the PagerDuty
incident that triggered the RCA, once, when the investigation completes.

Notes are permanent (PagerDuty has no edit/delete endpoint), so posting is
guarded by a claim on incidents.pagerduty_note_id (see postback_claim): a
lost note is possible, a duplicate is not.
"""

import json
import logging
import os
import re
from typing import Any, Dict, Optional, Tuple

from routes.pagerduty.pagerduty_helpers import PagerDutyAPIError, PagerDutyClient
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.notifications.postback_claim import PostbackClaim
from utils.notifications.rca_note import MIN_SUMMARY_CHARS, compose_note, extract_note_body

logger = logging.getLogger(__name__)

FRONTEND_URL = os.getenv("FRONTEND_URL")
_LOG = "[PagerDutyNote]"
_claims = PostbackClaim("pagerduty_note_id", _LOG)

# PagerDuty object ids are short alphanumerics; anything else must not reach the request path
_PD_ID_RE = re.compile(r"[A-Za-z0-9]{1,32}")


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


def send_pagerduty_incident_note(user_id: str, incident_data: Dict[str, Any]) -> bool:
    """Post the RCA root cause as a note on the originating PagerDuty incident.

    Returns True only when a note was posted. Never raises.
    """
    incident_id = incident_data.get("incident_id")
    try:
        ok, reason, pd_incident_id, root_cause, impact = _eligibility(incident_data)
        if not ok:
            logger.info("%s Skipping incident %s: %s", _LOG, incident_id, reason)
            return False

        creds = get_token_data(user_id, "pagerduty")
        if not creds:
            logger.info("%s Skipping incident %s: PagerDuty not connected", _LOG, incident_id)
            return False
        if (creds.get("capabilities") or {}).get("can_write_incidents") is not True:
            logger.info("%s Skipping incident %s: credentials cannot write incidents", _LOG, incident_id)
            return False

        client, creds = _build_client(user_id, creds)
        if client is None:
            return False

        content = compose_note(root_cause, incident_id, impact, FRONTEND_URL)

        if not _claims.claim(incident_id, user_id):
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
    except Exception:
        logger.exception("%s Failed to post note for incident %s", _LOG, incident_id)
        return False
