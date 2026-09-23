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
ROOT_CAUSE_MAX_CHARS = 1200
IMPACT_MAX_CHARS = 500
MIN_SUMMARY_CHARS = 80
_PENDING = "pending"
_LOG = "[PagerDutyNote]"

_CITATION_RE = re.compile(r"\[\d+(?:,\s*\d+)*\]")
# The space a removed citation leaves before punctuation ("spiked [2].": "spiked .")
_SPACE_BEFORE_PUNCT_RE = re.compile(r" ([.,;:!?])(?=\s|$)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*((?:[^*\n]|\*(?!\*))+)\*\*|__([^_\n]+)__")
# A star glued to a word char is not emphasis (p95*2, 3*4 nodes); only a delimiter-bounded pair is
_STAR_ITALIC_RE = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+)(?<!\s)\*(?![\w*])")
_UNDERSCORE_ITALIC_RE = re.compile(r"(?<!\w)_(?!\s)([^_\n]+)(?<!\s)_(?!\w)")
# PagerDuty object ids are short alphanumerics; anything else must not reach the request path
_PD_ID_RE = re.compile(r"[A-Za-z0-9]{1,32}")
_HEADING_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+", re.MULTILINE)
_BULLET_RE = re.compile(r"^[ \t]*[*+][ \t]+", re.MULTILINE)
_RULE_RE = re.compile(r"^[-*_]{3,}$")
_LIST_ITEM_RE = re.compile(r"^(?:[-*+]|\d+[.)])\s")
# Section headings. The summarizer is asked for three paragraphs (what happened,
# root cause, impact & timeline) followed by "## Ruled Out" / "## Not Checked";
# models render the paragraph labels as "## X", "**X**" or bare "X" lines, or not at all.
_MD_HEADING_RE = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t#]*$")
_BOLD_LINE_RE = re.compile(r"^(?:\*\*|__)([^*_.|]{1,80}?)(?:\*\*|__):?$")
_KNOWN_TITLE_RE = re.compile(
    r"^(?:summary|what happened|root cause|impact|timeline|incident report|ruled out|not checked|"
    r"suggested next steps|next steps|recommendations|action items|proposed actions|remediation steps)\b",
    re.IGNORECASE,
)
_END_SECTION_RE = re.compile(
    r"^(?:ruled out|not checked|suggested next steps|next steps|recommendations|action items|"
    r"proposed actions|remediation steps)\b",
    re.IGNORECASE,
)
_ROOT_HEADING_RE = re.compile(r"root cause", re.IGNORECASE)
_IMPACT_HEADING_RE = re.compile(r"impact|timeline", re.IGNORECASE)
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
    text = _STAR_ITALIC_RE.sub(r"\1", text)
    text = _UNDERSCORE_ITALIC_RE.sub(r"\1", text)
    paragraphs = [_SPACE_BEFORE_PUNCT_RE.sub(r"\1", " ".join(p.split())) for p in re.split(r"\n\s*\n", text)]
    text = "\n\n".join(p for p in paragraphs if p)
    return _truncate(text, max_chars) if max_chars else text


def _heading_text(line: str) -> Optional[str]:
    """Lower-case title if the line is a section heading ("## X", "**X**", or a bare known title)."""
    m = _MD_HEADING_RE.match(line)
    if m:
        return m.group(1).strip("*_ :").lower()
    m = _BOLD_LINE_RE.match(line)
    if m:
        return m.group(1).strip(" :").lower()
    if len(line) <= 60 and "." not in line and _KNOWN_TITLE_RE.match(line):
        return line.strip(" :").lower()
    return None


def _sections(summary: str) -> list:
    """[(heading, [prose paragraphs])] in document order; the first entry has heading "".

    Line-based so a heading directly followed by its paragraph (no blank line)
    still splits. Rules, list items, pipe-metadata lines and stubs shorter than
    MIN_PROSE_CHARS are dropped.
    """
    sections = [["", []]]
    buffer: list = []

    def flush() -> None:
        if not buffer:
            return
        first = buffer[0]
        raw = " ".join(buffer)
        buffer.clear()
        if _LIST_ITEM_RE.match(first) or raw.count(" | ") >= 2:
            return
        text = _to_plain_text(raw, max_chars=None)
        if len(text) >= MIN_PROSE_CHARS:
            sections[-1][1].append(text)

    for line in summary.splitlines():
        line = line.strip()
        if not line or _RULE_RE.match(line):
            flush()
            continue
        heading = _heading_text(line)
        if heading is not None:
            flush()
            sections.append([heading, []])
            continue
        buffer.append(line)
    flush()
    return [(heading, paragraphs) for heading, paragraphs in sections]


def _extract_note_body(summary: Optional[str]) -> Tuple[str, str]:
    """(root cause, impact) paragraphs as plain text; impact may be "".

    Headed reports are read by section title. Unheaded ones fall back to the
    summarizer's paragraph order: the root-cause paragraph is the one opening
    with its prescribed phrase (else the 2nd), and impact is the paragraph
    after it. Nothing after "Ruled Out" / "Not Checked" / next-steps is used.
    """
    root: Optional[str] = None
    impact: Optional[str] = None
    narrative: list = []
    for heading, paragraphs in _sections(summary or ""):
        if _END_SECTION_RE.match(heading):
            break
        if root is None and _ROOT_HEADING_RE.search(heading):
            root = paragraphs[0] if paragraphs else None
        elif impact is None and _IMPACT_HEADING_RE.search(heading):
            impact = paragraphs[0] if paragraphs else None
        else:
            narrative.extend(paragraphs)

    if root is None and narrative:
        index = next((i for i, p in enumerate(narrative) if _ROOT_CAUSE_RE.match(p)), None)
        if index is None:
            index = 1 if len(narrative) >= 2 else 0
        root = narrative[index]
        if impact is None and index + 1 < len(narrative):
            impact = narrative[index + 1]

    return _truncate(root or "", ROOT_CAUSE_MAX_CHARS), _truncate(impact or "", IMPACT_MAX_CHARS)


def _pick_root_cause_paragraph(summary: Optional[str]) -> str:
    return _extract_note_body(summary)[0]


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
    root_cause, impact = _extract_note_body(incident_data.get("aurora_summary"))
    if len(root_cause) < MIN_SUMMARY_CHARS:
        return False, "summary too short to post", pd_incident_id, root_cause, impact
    return True, "", pd_incident_id, root_cause, impact


def _compose_note(root_cause: str, incident_id: str, impact: str = "") -> str:
    lines = ["Aurora RCA", "", "Root cause", root_cause, ""]
    if impact:
        lines += ["Impact", impact, ""]
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
    """Release the claim only when PagerDuty definitively answered; a 403 also disables the capability."""
    if e.status_code is None:
        # No response: the note may have landed. Keep the claim (a lost
        # note beats a duplicate one; notes cannot be deleted).
        logger.warning(
            "%s No response from PagerDuty for incident %s; claim kept to avoid a duplicate: %s",
            _LOG, incident_id, e,
        )
        return
    _release(incident_id, user_id)
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

        content = _compose_note(root_cause, incident_id, impact)

        if not _claim(incident_id, user_id):
            logger.info("%s Incident %s already has a note posted or in flight", _LOG, incident_id)
            return False

        try:
            response = client.create_note(pd_incident_id, content)
        except PagerDutyAPIError as e:
            _handle_post_error(e, incident_id, user_id, creds)
            return False

        note_id = ((response or {}).get("note") or {}).get("id") or "posted"
        _record(incident_id, user_id, note_id)
        logger.info("%s Posted note %s on PagerDuty incident %s for %s", _LOG, note_id, pd_incident_id, incident_id)
        return True
    except Exception:
        logger.exception("%s Failed to post note for incident %s", _LOG, incident_id)
        return False
