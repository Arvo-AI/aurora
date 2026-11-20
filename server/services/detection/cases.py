"""Open a case when a proven check starts failing.

The case is an ordinary incident with an ordinary RCA, so the right channel, the
thread and the proposed fix all come from the path Aurora already has. What this
module adds is the opening line: which check fired, what it saw, and why that
check exists, so nobody has to ask what made Aurora look.
"""

import json
import logging
from typing import Any, Dict, Optional

from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

# One shared failure trips many checks. Fires on an asset that already has a
# fresh open case join it instead of opening another.
JOIN_OPEN_CASE_MINUTES = 60
_TITLE_MAX_CHARS = 200
_TITLE_NAME_CHARS = 90
_TITLE_ASSET_CHARS = 80

_ORIGIN_WORDS = {
    "incident": "it was written after incident {ref}",
    "miss": "Aurora missed an earlier problem here ({ref})",
    "graph": "it is a default check for this kind of asset",
    "explore": "Aurora noticed something here while exploring",
    "gap": "nothing else was monitoring this",
    "person": "{ref} asked Aurora to watch this",
}


def origin_sentence(origin: Dict[str, Any]) -> str:
    """Why the check exists, in words a person in a Slack thread can read."""
    kind = (origin or {}).get("kind")
    ref = ((origin or {}).get("ref") or "").strip() or "no reference recorded"
    template = _ORIGIN_WORDS.get(kind)
    return template.format(ref=ref) if template else "its origin was not recorded"


_OP_WORDS = {
    "lt": "is below", "lte": "is at most", "gt": "is above", "gte": "is at least",
    "eq": "is", "ne": "is not", "contains": "contains", "not_contains": "does not contain",
    "empty": "is empty", "not_empty": "is not empty",
}


def healthy_condition(assertion: Dict[str, Any]) -> str:
    """The assertion as a sentence fragment, e.g. 'number of output lines is at least 2'."""
    subject = "number of output lines" if assertion.get("expect") == "lines" else "output"
    op = assertion.get("op")
    words = _OP_WORDS.get(op, str(op))
    if op in ("empty", "not_empty"):
        return "%s %s" % (subject, words)
    return "%s %s %s" % (subject, words, assertion.get("value"))


def case_title(check: Dict[str, Any]) -> str:
    # The title is the one field the Slack routing prompt always carries.
    # Name and asset are cut separately: one slice over the whole title would let
    # a long name push the asset, the part routing needs most, off the end.
    name, asset = check["name"][:_TITLE_NAME_CHARS], check["asset"][:_TITLE_ASSET_CHARS]
    return "Aurora check: %s failing on %s" % (name, asset)


def case_description(check: Dict[str, Any], observed: str, reported: str = "") -> str:
    """*reported* is what a person said in chat about the same asset, when that is why
    an unproven check's failure is being raised."""
    assertion = check["assertion"]
    healthy = healthy_condition(assertion)
    opening = (
        "One of Aurora's own checks is failing, and a person reported a problem with the same "
        "asset in chat. They said: %s" % reported
        if reported else
        "One of Aurora's own checks started failing. No person or alert reported this."
    )
    return "\n".join((
        opening,
        "",
        "Check: %s (revision %s)" % (check["name"], check["revision"]),
        "Asset: %s" % check["asset"],
        "What it predicts: %s" % check["prediction"],
        "Command: %s" % check["command"],
        "Healthy when: %s" % healthy,
        "Value seen: %s" % (observed or "(empty)"),
        "Why this check exists: %s." % origin_sentence(check["origin"]),
        "",
        "First confirm whether this is a real problem. In your conclusion, say that Aurora's "
        "check found it, the value it saw, and why the check exists.",
    ))


def find_open_case(cursor, org_id: str, asset: str) -> Optional[str]:
    """A fresh, still-open incident on *asset*, or None."""
    cursor.execute(
        """SELECT id FROM incidents
           WHERE org_id = %s AND status = 'investigating'
             AND lower(alert_service) = lower(%s)
             AND created_at >= NOW() - %s * INTERVAL '1 minute'
           ORDER BY created_at DESC LIMIT 1""",
        (org_id, asset, JOIN_OPEN_CASE_MINUTES),
    )
    row = cursor.fetchone()
    return str(row[0]) if row else None


def open_case(check: Dict[str, Any], observed: str, user_id: str, *, org_id: str,
              fire_id: str, reported: str = "") -> Optional[str]:
    """Create the incident and start its RCA. Returns the incident id, or None.

    None means nothing was opened (rate limit, dispatch failure); the caller
    leaves the fire unmarked so the next tick tries again.

    *org_id* is the check's org: the incident is refused rather than created
    anywhere else. *fire_id* makes the incident's identity stable, so a retry
    after a crash between creating it and linking it finds the same incident.
    """
    from chat.backend.agent.tools.trigger_rca_tool import trigger_rca

    try:
        raw = trigger_rca(
            issue_description=case_description(check, observed, reported),
            title=case_title(check),
            service=check["asset"],
            severity="medium",
            user_id=user_id,
            expected_org_id=org_id,
            dedupe_key="detection-fire:%s" % fire_id,
        )
        result = json.loads(raw)
    except Exception:
        logger.exception("[DetectionCases] Failed to open a case for check %s", sanitize(check["name"]))
        return None
    if not isinstance(result, dict) or not result.get("incident_id"):
        logger.warning("[DetectionCases] Case not opened for check %s: %s",
                       sanitize(check["name"]), sanitize(str(result)[:200]))
        return None
    return str(result["incident_id"])
