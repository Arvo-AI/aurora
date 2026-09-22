"""
PagerDuty incident note: post Aurora's root cause back onto the PagerDuty
incident that triggered the RCA, once, when the investigation completes.

Notes are permanent (PagerDuty has no edit/delete endpoint), so posting is
guarded by a claim on incidents.pagerduty_note_id: the row is marked
'pending' before the POST and set to the note id after it. The claim is
released only when PagerDuty definitively rejected the POST; when the
outcome is unknown (timeout, connection reset) it stays 'pending'. A claim
that never resolves means a lost note, never a duplicate.
"""

import json
import logging
import os
import re
from typing import Any, Dict, Optional, Tuple

from routes.pagerduty.pagerduty_helpers import PagerDutyAPIError, PagerDutyClient
from utils.auth.stateless_auth import set_rls_context
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

FRONTEND_URL = os.getenv("FRONTEND_URL")
NOTE_MAX_CHARS = 700
MIN_SUMMARY_CHARS = 80
_PENDING = "pending"
_LOG = "[PagerDutyNote]"

_CITATION_RE = re.compile(r"\s*\[\d+(?:\s*,\s*\d+)*\]")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_ITALIC_RE = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])|(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])")
_HEADING_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+", re.MULTILINE)
_BULLET_RE = re.compile(r"^[ \t]*[*+][ \t]+", re.MULTILINE)
_RULE_RE = re.compile(r"^[-*_]{3,}$")
_LIST_ITEM_RE = re.compile(r"^(?:[-*+]|\d+[.)])\s")
# Section titles that end the narrative when they appear as a paragraph (bold or bare)
_END_MARKER_RE = re.compile(
    r"^[*_\s]*(?:Ruled Out|Not Checked|Suggested Next Steps|Next Steps|Recommendations|"
    r"Action Items|Proposed Actions|Remediation Steps)\b",
    re.IGNORECASE,
)
# Opening phrases the summarizer is told to use for the root-cause paragraph
_ROOT_CAUSE_RE = re.compile(r"^(?:the )?(?:root cause|most likely cause)\b|^evidence suggests\b", re.IGNORECASE)
MIN_PROSE_CHARS = 40


def _truncate(text: str, limit: int = NOTE_MAX_CHARS) -> str:
    """Cut at the last space before limit; hard cut if that space is too early."""
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit)
    if cut < 200:
        cut = limit
    return text[:cut].rstrip() + "..."


def _to_plain_text(text: str, max_chars: Optional[int] = NOTE_MAX_CHARS) -> str:
    """Strip markdown/citations to plain text; paragraphs stay separated by one blank line."""
    if not text:
        return ""
    text = text.replace("```", "").replace("`", "")
    text = _LINK_RE.sub(r"\1 (\2)", text)
    text = _CITATION_RE.sub("", text)
    text = _HEADING_RE.sub("", text)
    text = _BULLET_RE.sub("- ", text)
    text = _BOLD_RE.sub(lambda m: m.group(1) or m.group(2), text)
    text = _ITALIC_RE.sub(lambda m: m.group(1) or m.group(2), text)
    paragraphs = [" ".join(p.split()) for p in re.split(r"\n\s*\n", text)]
    text = "\n\n".join(p for p in paragraphs if p)
    return _truncate(text, max_chars) if max_chars else text


def _narrative_paragraphs(summary: str) -> list:
    """Prose paragraphs of the report body, in order, as plain text.

    The summary is "## Incident Report" + a metadata line + "---" + 2-3
    prose paragraphs (what happened, root cause, impact) + "## Ruled Out" /
    "## Not Checked" bullet sections (summarization.py). Decoration and list
    sections are skipped; the walk stops at the first heading or end-marker
    after the narrative, so the bullet sections never leak into a note.
    """
    paragraphs = []
    for raw in re.split(r"\n\s*\n", summary):
        raw = raw.strip()
        if not raw:
            continue
        if raw.startswith("#") or _END_MARKER_RE.match(raw):
            if paragraphs:
                break
            continue
        if _RULE_RE.match(raw) or _LIST_ITEM_RE.match(raw) or raw.count(" | ") >= 2:
            continue
        text = _to_plain_text(raw, max_chars=None)
        if len(text) >= MIN_PROSE_CHARS:
            paragraphs.append(text)
    return paragraphs


def _pick_root_cause_paragraph(summary: Optional[str]) -> str:
    """The root-cause paragraph: by its opening phrase, else the 2nd prose paragraph, else the 1st."""
    paragraphs = _narrative_paragraphs(summary or "")
    if not paragraphs:
        return ""
    chosen = next((p for p in paragraphs if _ROOT_CAUSE_RE.match(p)), None)
    if chosen is None:
        chosen = paragraphs[1] if len(paragraphs) >= 2 else paragraphs[0]
    return _truncate(chosen)


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
    value = meta.get("incidentId")
    return str(value) if value else None


def _should_post(incident_data: Dict[str, Any]) -> Tuple[bool, str]:
    """Pure eligibility check. Returns (ok, reason-when-not-ok)."""
    ok, reason, _, _ = _eligibility(incident_data)
    return ok, reason


def _eligibility(incident_data: Dict[str, Any]) -> Tuple[bool, str, Optional[str], str]:
    """(ok, reason-when-not-ok, PagerDuty incident id, root-cause paragraph)."""
    if incident_data.get("source_type") != "pagerduty":
        return False, "not a PagerDuty incident", None, ""
    if incident_data.get("recurrence_of"):
        return False, f"recurrence of {incident_data['recurrence_of']}; only anchors get a note", None, ""
    if incident_data.get("pagerduty_note_id"):
        return False, "note already posted or in flight", None, ""
    pd_incident_id = _pd_incident_id(incident_data)
    if not pd_incident_id:
        return False, "no PagerDuty incident id in alert_metadata", None, ""
    root_cause = _pick_root_cause_paragraph(incident_data.get("aurora_summary"))
    if len(root_cause) < MIN_SUMMARY_CHARS:
        return False, "summary too short to post", pd_incident_id, root_cause
    return True, "", pd_incident_id, root_cause


def _compose_note(root_cause: str, incident_id: str) -> str:
    lines = ["Aurora RCA", "", root_cause, ""]
    base_url = (FRONTEND_URL or "").rstrip("/")
    if base_url:
        lines += [f"Full investigation: {base_url}/incidents/{incident_id}", ""]
    lines.append("Generated automatically by Aurora. Verify before acting.")
    return "\n".join(lines)


def _update_note_id(user_id: str, sql: str, params: tuple) -> int:
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            if not set_rls_context(cursor, conn, user_id, log_prefix=_LOG):
                # Without RLS vars the UPDATE silently matches 0 rows
                raise RuntimeError(f"cannot resolve org for user {user_id}")
            cursor.execute(sql, params)
            rowcount = cursor.rowcount
        conn.commit()
    return rowcount


def _claim(incident_id: str, user_id: str) -> bool:
    """Mark the incident 'pending' iff no note is posted or in flight."""
    try:
        return _update_note_id(
            user_id,
            "UPDATE incidents SET pagerduty_note_id = %s WHERE id = %s AND pagerduty_note_id IS NULL",
            (_PENDING, incident_id),
        ) == 1
    except Exception:
        logger.exception("%s Failed to claim incident %s", _LOG, incident_id)
        return False


def _release(incident_id: str, user_id: str) -> None:
    """Undo a claim whose POST was definitively rejected, so a later completion can retry."""
    try:
        _update_note_id(
            user_id,
            "UPDATE incidents SET pagerduty_note_id = NULL WHERE id = %s AND pagerduty_note_id = %s",
            (incident_id, _PENDING),
        )
    except Exception:
        logger.exception("%s Failed to release claim on incident %s", _LOG, incident_id)


def _record(incident_id: str, user_id: str, note_id: str) -> None:
    try:
        _update_note_id(
            user_id,
            "UPDATE incidents SET pagerduty_note_id = %s WHERE id = %s",
            (note_id, incident_id),
        )
    except Exception:
        # The note is posted; the row stays 'pending', which still blocks a duplicate.
        logger.exception("%s Failed to record note %s on incident %s", _LOG, note_id, incident_id)


def _disable_write_capability(user_id: str, creds: Dict[str, Any]) -> None:
    caps = {**(creds.get("capabilities") or {}), "can_write_incidents": False}
    try:
        store_tokens_in_db(user_id, {**creds, "capabilities": caps}, "pagerduty")
    except Exception:
        logger.exception("%s Failed to persist can_write_incidents=False for user %s", _LOG, user_id)


def _build_client(user_id: str, creds: Dict[str, Any]) -> Tuple[Optional[PagerDutyClient], Dict[str, Any]]:
    """Client for the stored credentials; refreshes an OAuth token first."""
    if creds.get("auth_type") == "oauth":
        from routes.pagerduty.oauth_utils import refresh_token_if_needed

        success, refreshed = refresh_token_if_needed(creds)
        if not success:
            logger.warning("%s OAuth token expired for user %s; note not posted", _LOG, user_id)
            return None, creds
        if refreshed:
            creds = {**creds, **refreshed}
            try:
                store_tokens_in_db(user_id, creds, "pagerduty")
            except Exception:
                logger.exception("%s Failed to persist refreshed OAuth token", _LOG)
        token_kwargs = {"oauth_token": creds.get("access_token")}
    else:
        token_kwargs = {"api_token": creds.get("api_token")}
    from_email = creds.get("external_user_email") or None
    return PagerDutyClient(from_email=from_email, **token_kwargs), creds


def send_pagerduty_incident_note(user_id: str, incident_data: Dict[str, Any]) -> bool:
    """Post the RCA root cause as a note on the originating PagerDuty incident.

    Returns True only when a note was posted. Never raises.
    """
    incident_id = incident_data.get("incident_id")
    try:
        ok, reason, pd_incident_id, root_cause = _eligibility(incident_data)
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

        content = _compose_note(root_cause, incident_id)

        if not _claim(incident_id, user_id):
            logger.info("%s Incident %s already has a note posted or in flight", _LOG, incident_id)
            return False

        try:
            response = client.create_note(pd_incident_id, content)
        except PagerDutyAPIError as e:
            if e.status_code is None:
                # No response: the note may have landed. Keep the claim (a lost
                # note beats a duplicate one; notes cannot be deleted).
                logger.warning(
                    "%s No response from PagerDuty for incident %s; claim kept to avoid a duplicate: %s",
                    _LOG, incident_id, e,
                )
                return False
            _release(incident_id, user_id)
            if e.status_code == 403:
                _disable_write_capability(user_id, creds)
                logger.warning(
                    "%s PagerDuty refused the note for incident %s (forbidden); write capability disabled",
                    _LOG, incident_id,
                )
            else:
                logger.warning("%s PagerDuty rejected the note for incident %s: %s", _LOG, incident_id, e)
            return False

        note_id = ((response or {}).get("note") or {}).get("id") or "posted"
        _record(incident_id, user_id, note_id)
        logger.info("%s Posted note %s on PagerDuty incident %s for %s", _LOG, note_id, pd_incident_id, incident_id)
        return True
    except Exception:
        logger.exception("%s Failed to post note for incident %s", _LOG, incident_id)
        return False
