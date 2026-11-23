"""The one tool a chat-report investigation answers through.

A report read from chat is checked by a read-only background investigation
(services/detection/chat_intake.py). This is how it says what it found. The
tool only works from inside the session that was started for that report, so
no other conversation can close a report or open an incident through it.
"""

import json
import logging

from pydantic import BaseModel, Field

from services.detection import chat_intake_enabled, chat_signals
from services.detection.chat_reader import clean_asset, clean_claim
from utils.auth.stateless_auth import set_rls_context
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

_SUMMARY_MAX_CHARS = 1500


class ConcludeChatReportArgs(BaseModel):
    report_id: str = Field(description="The report id you were given.")
    real: bool = Field(description="True only if you saw evidence of the problem yourself.")
    summary: str = Field(description="What you checked and what you saw: commands and values.")
    asset: str = Field(default="", description="For a real problem: the bare name of the affected service or resource.")
    title: str = Field(default="", description="For a real problem: a short incident title.")
    severity: str = Field(default="medium", description="For a real problem: low, medium, high or critical.")


def _error(message: str) -> str:
    return json.dumps({"error": message})


def conclude_chat_report(report_id: str, real: bool, summary: str, asset: str = "", title: str = "",
                         severity: str = "medium", user_id: str | None = None,
                         session_id: str | None = None, **_kwargs) -> str:
    """Record what the investigation of a chat report found; open an incident if it is real."""
    if not user_id or not session_id:
        return _error("No user or session context available.")
    if not chat_intake_enabled():
        return _error("Reading chat for problem reports is turned off on this install.")
    summary = (summary or "").strip()[:_SUMMARY_MAX_CHARS]
    if not summary:
        return _error("summary is required: say what you checked and what you saw.")

    try:
        with db_pool.get_admin_connection() as conn, conn.cursor() as cursor:
            org_id = set_rls_context(cursor, conn, user_id, log_prefix="[DetectionReportTool]")
            if not org_id:
                return _error("No organization context available.")
            report = chat_signals.report_under_verification(cursor, org_id, report_id, session_id)
            if report is None:
                conn.rollback()
                return _error("This session is not investigating a report with that id.")
            if not real:
                chat_signals.conclude(cursor, report_id, chat_signals.STATUS_DISMISSED, summary)
                conn.commit()
                return json.dumps({"status": "dismissed"})
            named = clean_asset(asset, []) or report["asset"] or ""
            # Marked confirmed first, with what is needed to open the incident:
            # if opening fails now, the next reading turn tries again.
            cursor.execute(
                """UPDATE detection_chat_signals
                   SET status = %s, status_detail = %s, asset = COALESCE(NULLIF(%s, ''), asset),
                       severity = %s, confirmed_by = %s, updated_at = NOW()
                   WHERE id = %s""",
                (chat_signals.STATUS_CONFIRMED, summary, named, severity, user_id, report_id),
            )
            conn.commit()

        from services.detection.chat_intake import open_incident

        incident_id = open_incident(report, user_id, org_id, named, clean_claim(title), severity, summary)
        if not incident_id:
            return json.dumps({
                "status": "confirmed",
                "note": "The incident could not be opened right now. Aurora will try again shortly.",
            })
        with db_pool.get_admin_connection() as conn, conn.cursor() as cursor:
            set_rls_context(cursor, conn, user_id, org_id=org_id, log_prefix="[DetectionReportTool]")
            cursor.execute(
                "UPDATE detection_chat_signals SET incident_id = %s WHERE id = %s",
                (incident_id, report_id),
            )
            conn.commit()
        return json.dumps({"status": "confirmed", "incident_id": incident_id})
    except Exception:
        logger.exception("[DetectionReportTool] Failed to conclude a chat report")
        return _error("Failed to record the result.")
