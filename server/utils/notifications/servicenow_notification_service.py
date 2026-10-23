"""
ServiceNow write-back.

Work note: post Aurora's root cause back onto the ServiceNow incident that
triggered the RCA, once, when the investigation completes. Work notes are
permanent journal entries, so posting is guarded by a claim on
incidents.servicenow_note_id (see postback_claim): a lost note is possible,
a duplicate is not.

Resolution sync: when an Aurora incident that came from ServiceNow is marked
resolved, resolve the ServiceNow incident too (opt-in, "full" write scope).
Setting the state is idempotent, so it needs no claim.
"""

import json
import logging
import os
from typing import Any, Dict, Optional, Tuple

from routes.servicenow.servicenow_client import ServiceNowAPIError, ServiceNowClient, is_sys_id
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.notifications.postback_claim import PostbackClaim
from utils.notifications.rca_note import MIN_SUMMARY_CHARS, compose_note, extract_note_body

logger = logging.getLogger(__name__)

FRONTEND_URL = os.getenv("FRONTEND_URL")
_LOG = "[ServiceNowNote]"
_claims = PostbackClaim("servicenow_note_id", _LOG)


def _sys_id(incident_data: Dict[str, Any]) -> Optional[str]:
    """The ServiceNow record sys_id from alert_metadata; source_alert_id is only a hash."""
    meta = incident_data.get("alert_metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (ValueError, TypeError):
            return None
    if not isinstance(meta, dict):
        return None
    value = str(meta.get("sysId") or "")
    return value if is_sys_id(value) else None


def _should_post(incident_data: Dict[str, Any]) -> Tuple[bool, str]:
    ok, reason, *_ = _eligibility(incident_data)
    return ok, reason


def _eligibility(incident_data: Dict[str, Any]) -> Tuple[bool, str, Optional[str], str, str]:
    """(ok, reason-when-not-ok, sys_id, root cause, impact)."""
    if incident_data.get("source_type") != "servicenow":
        return False, "not a ServiceNow incident", None, "", ""
    if incident_data.get("recurrence_of"):
        return False, f"recurrence of {incident_data['recurrence_of']}; only anchors get a note", None, "", ""
    if incident_data.get("servicenow_note_id"):
        return False, "note already posted or in flight", None, "", ""
    sys_id = _sys_id(incident_data)
    if not sys_id:
        return False, "no ServiceNow sys_id in alert_metadata", None, "", ""
    root_cause, impact = extract_note_body(incident_data.get("aurora_summary"))
    if len(root_cause) < MIN_SUMMARY_CHARS:
        return False, "summary too short to post", sys_id, root_cause, impact
    return True, "", sys_id, root_cause, impact


def disable_write_capability(user_id: str, creds: Dict[str, Any]) -> None:
    caps = {**(creds.get("capabilities") or {}), "can_write_incidents": False}
    try:
        store_tokens_in_db(user_id, {**creds, "capabilities": caps}, "servicenow")
    except Exception:
        logger.exception("%s Failed to persist can_write_incidents=False for user %s", _LOG, user_id)


def _handle_post_error(e: ServiceNowAPIError, incident_id: str, user_id: str, creds: Dict[str, Any]) -> None:
    """Release the claim only when ServiceNow definitively rejected the PATCH (4xx); a 403 also disables the capability."""
    if e.status_code is None or e.status_code >= 500:
        # No response, or a 5xx a gateway may have returned after the note was
        # written: the outcome is unknown. Keep the claim (a lost note beats a duplicate).
        logger.warning(
            "%s No definitive answer from ServiceNow for incident %s; claim kept to avoid a duplicate: %s",
            _LOG, incident_id, e,
        )
        return
    _claims.release(incident_id, user_id)
    if e.status_code == 403:
        disable_write_capability(user_id, creds)
        logger.warning(
            "%s ServiceNow refused the work note for incident %s (forbidden); write capability disabled",
            _LOG, incident_id,
        )
    else:
        logger.warning("%s ServiceNow rejected the work note for incident %s: %s", _LOG, incident_id, e)


def send_servicenow_work_note(user_id: str, incident_data: Dict[str, Any]) -> bool:
    """Post the RCA root cause as a work note on the originating ServiceNow incident.

    Returns True only when a note was posted. Never raises.
    """
    incident_id = incident_data.get("incident_id")
    try:
        ok, reason, sys_id, root_cause, impact = _eligibility(incident_data)
        if not ok:
            logger.info("%s Skipping incident %s: %s", _LOG, incident_id, reason)
            return False

        creds = get_token_data(user_id, "servicenow")
        if not creds:
            logger.info("%s Skipping incident %s: ServiceNow not connected", _LOG, incident_id)
            return False
        if (creds.get("capabilities") or {}).get("can_write_incidents") is False:
            logger.info("%s Skipping incident %s: credentials cannot write incidents", _LOG, incident_id)
            return False

        try:
            client = ServiceNowClient.from_credentials(creds)
        except ValueError as exc:
            logger.warning("%s Skipping incident %s: stored instance URL invalid: %s", _LOG, incident_id, exc)
            return False

        content = compose_note(root_cause, incident_id, impact, FRONTEND_URL)

        if not _claims.claim(incident_id, user_id):
            logger.info("%s Incident %s already has a note posted or in flight", _LOG, incident_id)
            return False

        try:
            response = client.add_work_note(sys_id, content)
        except ServiceNowAPIError as e:
            _handle_post_error(e, incident_id, user_id, creds)
            return False
        except ValueError as e:
            _claims.release(incident_id, user_id)
            logger.warning("%s Invalid sys_id for incident %s: %s", _LOG, incident_id, e)
            return False

        note_id = str((response or {}).get("sys_updated_on") or "posted")[:64]
        _claims.record(incident_id, user_id, note_id)
        logger.info("%s Posted work note on ServiceNow incident %s for %s", _LOG, sys_id, incident_id)
        return True
    except Exception:
        logger.exception("%s Failed to post work note for incident %s", _LOG, incident_id)
        return False


# ── resolution sync (Aurora → ServiceNow) ────────────────────────────


def _load_incident(user_id: str, incident_id: str) -> Optional[Dict[str, Any]]:
    from utils.auth.stateless_auth import set_rls_context
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn, conn.cursor() as cursor:
        set_rls_context(cursor, conn, user_id, log_prefix=_LOG)
        cursor.execute(
            "SELECT source_type, alert_metadata, aurora_summary FROM incidents WHERE id = %s",
            (incident_id,),
        )
        row = cursor.fetchone()
    if not row:
        return None
    return {"source_type": row[0], "alert_metadata": row[1], "aurora_summary": row[2]}


def sync_servicenow_resolution(user_id: str, incident_id: str) -> bool:
    """Resolve the originating ServiceNow incident after the Aurora incident was resolved.

    Runs only when the org turned on write-back, chose the "full" write scope
    and enabled resolution sync. Returns True only when ServiceNow accepted the
    state change. Never raises.
    """
    try:
        from routes.servicenow.tasks import get_resolve_sync, get_write_mode, work_notes_enabled

        incident = _load_incident(user_id, incident_id)
        if not incident or incident.get("source_type") != "servicenow":
            return False
        if not work_notes_enabled(user_id) or get_write_mode(user_id) != "full":
            logger.info("%s Resolution sync skipped for %s: write-back or full scope is off", _LOG, incident_id)
            return False
        resolve = get_resolve_sync(user_id)
        if not resolve["enabled"]:
            logger.info("%s Resolution sync skipped for %s: not enabled", _LOG, incident_id)
            return False
        sys_id = _sys_id(incident)
        if not sys_id:
            logger.info("%s Resolution sync skipped for %s: no sys_id in alert_metadata", _LOG, incident_id)
            return False

        creds = get_token_data(user_id, "servicenow")
        if not creds:
            return False
        if (creds.get("capabilities") or {}).get("can_write_incidents") is False:
            logger.info("%s Resolution sync skipped for %s: credentials cannot write incidents", _LOG, incident_id)
            return False
        try:
            client = ServiceNowClient.from_credentials(creds)
        except ValueError as exc:
            logger.warning("%s Resolution sync skipped for %s: stored instance URL invalid: %s", _LOG, incident_id, exc)
            return False

        root_cause, impact = extract_note_body(incident.get("aurora_summary"))
        if len(root_cause) >= MIN_SUMMARY_CHARS:
            close_notes = compose_note(root_cause, incident_id, impact, FRONTEND_URL)
        else:
            link = f"{FRONTEND_URL.rstrip('/')}/incidents/{incident_id}" if FRONTEND_URL else incident_id
            close_notes = f"Resolved in Aurora. Investigation: {link}"

        try:
            client.update_incident(sys_id, {
                "state": "6",
                "close_code": resolve["closeCode"],
                "close_notes": close_notes[:4000],
            })
        except ServiceNowAPIError as e:
            if e.status_code == 403:
                disable_write_capability(user_id, creds)
                logger.warning("%s ServiceNow refused to resolve %s (forbidden); write capability disabled", _LOG, sys_id)
            else:
                logger.warning("%s ServiceNow did not resolve %s: %s", _LOG, sys_id, e)
            return False
        logger.info("%s Resolved ServiceNow incident %s for %s", _LOG, sys_id, incident_id)
        return True
    except Exception:
        logger.exception("%s Resolution sync failed for incident %s", _LOG, incident_id)
        return False
