"""Continue an investigation that stopped before it finished.

An RCA stopped by its time limit, the stall watchdog or an error ends with a
partial report and keeps why in ``incidents.rca_unfinished_reason``. A user can
then ask for more: the investigation runs again in the same chat session, with
everything it gathered so far, a fresh time budget, and the instruction to pick
up where it stopped. When it ends the report is regenerated over all the
evidence and the incident's cards are updated in place.

Each continuation spends a full investigation budget, so it only ever starts
from a user's request, and an incident can be continued a bounded number of
times.
"""

import json
import logging
from typing import Any, Dict, List, Optional

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from utils.auth.stateless_auth import set_rls_context
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

CONTINUE_SOURCE = "continue_investigation"
MAX_CONTINUATIONS = 3

# Why the previous run stopped, as the continuation prompt states it.
_STOPPED_BECAUSE = {
    "time_limit": "it reached its time limit",
    "stalled": "it stopped making progress",
    "error": "it hit an error",
    "interrupted": "the worker running it stopped",
}

CONTINUE_PROMPT = (
    "Continue this investigation. It stopped before it finished ({why}); the evidence "
    "gathered so far is above, and this is the partial report written from it:\n\n"
    "{report}\n\n"
    "- Do not repeat checks that already returned results.\n"
    "- Start with the areas listed as Not Checked and the open hypotheses.\n"
    "- Confirm or rule out the root cause, then write your conclusions."
)
# The report rides in the prompt, so it survives context trimming; cap it.
_MAX_REPORT_CHARS = 8000


class ContinueRefused(Exception):
    """The investigation cannot be continued now; the message says why."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def is_continuation(trigger_metadata: Optional[Dict[str, Any]]) -> bool:
    return (trigger_metadata or {}).get("source") == CONTINUE_SOURCE


def continuation_prompt(reason: Optional[str], report: Optional[str] = None) -> str:
    report = (report or "").strip() or "(no report was written)"
    if len(report) > _MAX_REPORT_CHARS:
        report = report[:_MAX_REPORT_CHARS] + "\n[report truncated]"
    return CONTINUE_PROMPT.format(
        why=_STOPPED_BECAUSE.get(reason or "", "it was interrupted"), report=report,
    )


def continuations_left(used: Optional[int]) -> int:
    return max(0, MAX_CONTINUATIONS - int(used or 0))


def continue_investigation(incident_id: str, requested_by: str) -> Dict[str, Any]:
    """Start a continuation of *incident_id*'s unfinished RCA for *requested_by*.

    The caller has already checked that *requested_by* may write incidents in
    the incident's org; RLS scopes the row to that org. Runs as the incident's
    owner, like the original RCA, so it uses the same connectors and session.
    Raises ContinueRefused when the investigation cannot be continued.
    """
    # A refusal is raised after the connection is released: raised inside, the
    # pool would log it as a database error.
    refusal: Optional[ContinueRefused] = None
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            row = None
            if set_rls_context(cursor, conn, requested_by, log_prefix="[ContinueRCA]"):
                # The row lock makes two clicks start one continuation, not two.
                cursor.execute(
                    """SELECT user_id, aurora_chat_session_id, aurora_status, status,
                              rca_unfinished_reason, rca_continuations, aurora_summary
                       FROM incidents WHERE id = %s FOR UPDATE""",
                    (incident_id,),
                )
                row = cursor.fetchone()
            if row:
                owner_id, session_id, aurora_status, status, reason, used, report = row
                refusal = _refusal(status, aurora_status, reason, session_id, used)
            else:
                refusal = ContinueRefused("Incident not found", 404)
            if refusal is None:
                cursor.execute(
                    """UPDATE incidents
                       SET aurora_status = 'running', rca_continuations = rca_continuations + 1,
                           updated_at = NOW()
                       WHERE id = %s""",
                    (incident_id,),
                )
        if refusal is None:
            conn.commit()
        else:
            conn.rollback()
    if refusal is not None:
        raise refusal

    from chat.background.task import run_background_chat

    try:
        run_background_chat.delay(
            user_id=owner_id,
            session_id=str(session_id),
            initial_message=continuation_prompt(reason, report),
            trigger_metadata={"source": CONTINUE_SOURCE, "requested_by": requested_by},
            incident_id=incident_id,
            # The started card would announce a new investigation; the finished
            # report updates the existing card instead (see task.py).
            send_notifications=False,
            mode="ask",
        )
    except Exception:
        # Nothing was queued: put the incident back as it was.
        _undo_claim(incident_id, requested_by)
        raise
    logger.info("[ContinueRCA] Continuing incident %s (continuation %s)", incident_id, int(used or 0) + 1)
    return {"continuationsLeft": continuations_left(int(used or 0) + 1)}


# Tool calls that coordinate rather than gather evidence.
_NOT_EVIDENCE = frozenset({"dispatch_subagent", "write_findings", "load_skill", "trigger_rca"})
# The most recent steps replayed into a continuation; older ones are in the report.
_MAX_REPLAYED_STEPS = 150


def history_from_execution_steps(incident_id: str, user_id: str) -> List[BaseMessage]:
    """The investigation's tool calls and results, as agent messages.

    An interrupted run never saves its own context, but each tool call is
    recorded in execution_steps as it starts and ends, sub-agents' included.
    Replaying them lets a continuation see what was already checked and what
    it returned. Returns [] when nothing can be read.
    """
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                if not set_rls_context(cursor, conn, user_id, log_prefix="[ContinueRCA]"):
                    return []
                cursor.execute(
                    """SELECT id, tool_name, tool_call_id, tool_input, tool_output
                       FROM (SELECT * FROM execution_steps
                             WHERE incident_id = %s AND tool_output IS NOT NULL
                             ORDER BY step_index DESC LIMIT %s) recent
                       ORDER BY step_index""",
                    (incident_id, _MAX_REPLAYED_STEPS),
                )
                rows = cursor.fetchall()
    except Exception:
        logger.exception("[ContinueRCA] Could not read execution steps for incident %s", incident_id)
        return []

    messages: List[BaseMessage] = []
    seen = set()
    for step_id, tool_name, tool_call_id, tool_input, tool_output in rows:
        if tool_name in _NOT_EVIDENCE or not (tool_output or "").strip():
            continue
        call_id = tool_call_id or f"step_{step_id}"
        if call_id in seen:
            continue
        seen.add(call_id)
        args = tool_input
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                pass
        if not isinstance(args, dict):
            args = {"input": args}
        messages.append(AIMessage(content="", tool_calls=[{"id": call_id, "name": tool_name, "args": args}]))
        messages.append(ToolMessage(content=tool_output, tool_call_id=call_id, name=tool_name))
    return messages


def _refusal(status, aurora_status, reason, session_id, used) -> Optional[ContinueRefused]:
    """Why this incident cannot be continued now, or None when it can."""
    if status == "merged":
        return ContinueRefused("This incident was merged into another investigation")
    if aurora_status in ("running", "summarizing"):
        return ContinueRefused("The investigation is already running")
    if not reason or not session_id:
        return ContinueRefused("The investigation already finished")
    if continuations_left(used) <= 0:
        return ContinueRefused(f"This investigation was already continued {MAX_CONTINUATIONS} times")
    return None


def _undo_claim(incident_id: str, requested_by: str) -> None:
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, requested_by, log_prefix="[ContinueRCA]")
                cursor.execute(
                    """UPDATE incidents
                       SET aurora_status = 'complete',
                           rca_continuations = GREATEST(rca_continuations - 1, 0)
                       WHERE id = %s AND aurora_status = 'running'""",
                    (incident_id,),
                )
            conn.commit()
    except Exception:
        logger.exception("[ContinueRCA] Could not restore incident %s after a failed enqueue", incident_id)
