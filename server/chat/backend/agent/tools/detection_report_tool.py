"""The one tool a look into a chat report answers through.

A report read from chat is looked into by a read-only background chat
(services/detection/chat_intake.py). This is how it says what it found, and
what it would say to the people in the thread. The tool only works from inside
the session that was started for that report, so no other conversation can
close a report, open an incident or get something posted through it.

It does not post, and it does not take the look's word. A problem is confirmed
only by a look that ran something, and a reply is kept only if every statement
in it is shown by what the look's commands returned
(services/detection/chat_proof.py). Whether a kept reply reaches the channel is
then decided from the channel's level (services/detection/chat_voice.py).
"""

import json
import logging

from pydantic import BaseModel, Field

from services.detection import chat_intake_enabled, chat_proof, chat_signals, chat_voice
from services.detection.chat_reader import KIND_QUESTION, clean_asset, clean_claim
from utils.validation import is_valid_uuid
from utils.auth.stateless_auth import set_rls_context
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

_LOG = "[DetectionReportTool]"
_SUMMARY_MAX_CHARS = 1500
# How many times a look may be sent back to say less. After that its reply is dropped.
_REPLY_CHECKS = 2
_WITHHELD = "\n(Aurora wrote a reply and did not post it: not everything in it was shown by what it looked at.)"


class ConcludeChatReportArgs(BaseModel):
    report_id: str = Field(description="The report id you were given.")
    real: bool = Field(description="True only if you saw evidence of the problem yourself.")
    summary: str = Field(description="What you checked and what you saw: commands and values.")
    reply: str = Field(default="", description=(
        "What to say in the chat thread, if you were told a reply is allowed and you have "
        "something solid. Empty to stay silent."))
    same_as: str = Field(default="", description=(
        "The id of another report from the list you were given, when it is clearly the same issue."))
    asset: str = Field(default="", description="For a real problem: the bare name of the affected service or resource.")
    title: str = Field(default="", description="For a real problem: a short incident title.")
    severity: str = Field(default="medium", description="For a real problem: low, medium, high or critical.")


def _error(message: str) -> str:
    return json.dumps({"error": message})


def _team_id(user_id: str) -> str | None:
    from utils.auth.stateless_auth import get_credentials_from_db

    return (get_credentials_from_db(user_id, "slack") or {}).get("team_id")


def _said(user_id: str, org_id: str, report_id: str, result: dict) -> str:
    """Post what there is to say, and tell the look whether it was."""
    result["replied_in_chat"] = chat_voice.say(user_id, org_id, report_id)
    return json.dumps(result)


def _record(cursor, org_id: str, user_id: str, session_id: str, finding: dict):
    """Write the finding on the report. Returns (report, status, asset), or (None, None, None)
    when this session is not looking into that report. The caller commits."""
    report_id = finding["report_id"]
    report = chat_signals.report_under_verification(cursor, org_id, report_id, session_id)
    if report is None:
        return None, None, None
    related = None
    if is_valid_uuid(finding["same_as"]) and chat_signals.other_public_report(
            cursor, org_id, _team_id(user_id), report_id, finding["same_as"]):
        related = finding["same_as"]
    is_question = report["kind"] == KIND_QUESTION
    if is_question or not finding["real"]:
        status = (chat_signals.STATUS_ANSWERED if is_question and finding["reply"]
                  else chat_signals.STATUS_DISMISSED)
        chat_signals.conclude(cursor, report_id, status, finding["summary"], finding["reply"], related)
        return report, status, ""
    named = clean_asset(finding["asset"], []) or report["asset"] or ""
    # Marked confirmed first, with what is needed to open the incident:
    # if opening fails now, the next reading turn tries again.
    cursor.execute(
        """UPDATE detection_chat_signals
           SET status = %s, status_detail = %s, asset = COALESCE(NULLIF(%s, ''), asset),
               severity = %s, confirmed_by = %s, reply = NULLIF(%s, ''), related_to = %s,
               updated_at = NOW()
           WHERE id = %s""",
        (chat_signals.STATUS_CONFIRMED, finding["summary"], named, finding["severity"], user_id,
         finding["reply"], related, report_id),
    )
    return report, chat_signals.STATUS_CONFIRMED, named


_NOT_THIS_SESSION = "This session is not investigating a report with that id."


def _hold_to_evidence(user_id: str, org_id: str, session_id: str, finding: dict) -> str | None:
    """Hold the finding against what this session's commands returned.

    Returns an error for the look to act on, or None when the finding may be
    recorded. A reply that is not fully shown is sent back to be cut down, and
    dropped from *finding* once the look has had its chances.
    """
    seen = chat_proof.evidence(user_id, org_id, session_id)
    if finding["real"] and not seen:
        return _error(
            "You have not run anything that returned a result, so nothing confirms this. Look at "
            "the system with your read-only tools, then call conclude_chat_report again."
        )
    if not finding["reply"]:
        return None
    missing = chat_proof.unsupported(finding["reply"], seen, user_id)
    if not missing:
        return None
    with db_pool.get_admin_connection() as conn, conn.cursor() as cursor:
        set_rls_context(cursor, conn, user_id, org_id=org_id, log_prefix=_LOG)
        checks = chat_signals.count_reply_check(cursor, finding["report_id"])
        conn.commit()
    if checks < _REPLY_CHECKS:
        return json.dumps({
            "error": "The reply was not posted. These statements in it are not shown by the output "
                     "of the commands you ran. Call conclude_chat_report again with a reply that "
                     "states only what that output shows, or with an empty reply.",
            "not_shown": missing,
        })
    finding["reply"] = ""
    finding["summary"] = (finding["summary"] + _WITHHELD)[:_SUMMARY_MAX_CHARS + len(_WITHHELD)]
    return None


def conclude_chat_report(report_id: str, real: bool, summary: str, reply: str = "", same_as: str = "",
                         asset: str = "", title: str = "", severity: str = "medium",
                         user_id: str | None = None, session_id: str | None = None, **_kwargs) -> str:
    """Record what the look into a chat report found; open an incident if it is a real problem."""
    if not user_id or not session_id:
        return _error("No user or session context available.")
    if not chat_intake_enabled():
        return _error("Reading chat is turned off on this install.")
    summary = (summary or "").strip()[:_SUMMARY_MAX_CHARS]
    if not summary:
        return _error("summary is required: say what you checked and what you saw.")
    finding = {"report_id": report_id, "real": real, "summary": summary, "asset": asset,
               "severity": severity, "same_as": same_as, "reply": chat_voice.clean_reply(reply)}

    try:
        with db_pool.get_admin_connection() as conn, conn.cursor() as cursor:
            org_id = set_rls_context(cursor, conn, user_id, log_prefix=_LOG)
            if not org_id:
                return _error("No organization context available.")
            if chat_signals.report_under_verification(cursor, org_id, report_id, session_id) is None:
                return _error(_NOT_THIS_SESSION)
        sent_back = _hold_to_evidence(user_id, org_id, session_id, finding)
        if sent_back:
            return sent_back
        with db_pool.get_admin_connection() as conn, conn.cursor() as cursor:
            set_rls_context(cursor, conn, user_id, org_id=org_id, log_prefix=_LOG)
            report, status, named = _record(cursor, org_id, user_id, session_id, finding)
            if report is None:
                conn.rollback()
                return _error(_NOT_THIS_SESSION)
            conn.commit()
        if status != chat_signals.STATUS_CONFIRMED:
            return _said(user_id, org_id, report_id, {"status": status})

        from services.detection.chat_intake import open_incident

        incident_id = open_incident(report, user_id, org_id, named, clean_claim(title), severity, summary)
        if not incident_id:
            return _said(user_id, org_id, report_id, {
                "status": "confirmed",
                "note": "The incident could not be opened right now. Aurora will try again shortly.",
            })
        with db_pool.get_admin_connection() as conn, conn.cursor() as cursor:
            set_rls_context(cursor, conn, user_id, org_id=org_id, log_prefix=_LOG)
            cursor.execute(
                "UPDATE detection_chat_signals SET incident_id = %s WHERE id = %s",
                (incident_id, report_id),
            )
            conn.commit()
        return _said(user_id, org_id, report_id, {"status": "confirmed", "incident_id": incident_id})
    except Exception:
        logger.exception("[DetectionReportTool] Failed to conclude a chat report")
        return _error("Failed to record the result.")
