"""
Incident Index — the recurrence discovery map.

One self-maintaining memory artifact per org (category=incident_index,
title="Incident Index") holding a compact, id-keyed line per incident. The
recurrence agent scans it to find candidate anchors, then drills into a
specific incident via get_incident. Written deterministically at RCA completion
(append_incident_line) and groomed by the nightly memory_consolidation action.

Design: docs/design/recurrence-incident-index.md
"""

import logging
import re
from typing import Optional

from services.memory import INCIDENT_INDEX_CATEGORY, INCIDENT_INDEX_TITLE
from chat.backend.agent.tools.memory_tool import append_to_memory, read_memory

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[IncidentIndex]"

# One synopsis line must stay short so 100s of incidents fit the recurrence
# agent's input block. Titles/summaries are trimmed to this before formatting.
_MAX_SYNOPSIS_CHARS = 160

# Hard ceiling on how much of the index we ever inject into a prompt. The
# groomer keeps it well under this; the cap is belt-and-braces so an un-groomed
# index (e.g. collector ran but consolidation hasn't) can't blow the context
# window. ~24k chars ≈ 6k tokens.
INDEX_INJECTION_CHAR_BUDGET = 24000


def build_synopsis(alert_title: str, service: str, summary: str) -> str:
    """Derive a compact one-clause synopsis from data already on hand.

    No extra LLM call: prefer the alert title (already human-authored and
    short); fall back to the first sentence of the RCA summary; then to a
    generic label. Always trimmed to _MAX_SYNOPSIS_CHARS.
    """
    # Alert title is the cheapest, most reliable discriminator — use it first.
    title = (alert_title or "").strip()
    if title:
        return _clip(title)

    # No title — pull the first sentence of the RCA summary as a stand-in.
    text = (summary or "").strip()
    if text:
        first = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0]
        return _clip(first)

    # Nothing usable — keep the line present but generic (still id-keyed).
    return _clip(service or "incident")


def format_index_line(
    *,
    incident_id: str,
    date_iso: str,
    service: str,
    status: str,
    synopsis: str,
) -> str:
    """Canonical one-line index entry. The incident_id is the join key back to
    the DB (get_incident / recurrence_fold), so it MUST be present and first."""
    svc = (service or "unknown").strip() or "unknown"
    st = (status or "resolved").strip() or "resolved"
    date = (date_iso or "").strip() or "?"
    return f"- [INC {incident_id} | {date} | {svc} | {st}] {synopsis}"


def append_incident_line(
    *,
    user_id: str,
    incident_id: str,
    date_iso: str,
    service: str,
    status: str,
    alert_title: str,
    summary: str,
    session_id: Optional[str] = None,
) -> bool:
    """Append one incident's line to the org's Incident Index. Best-effort.

    Idempotent by incident_id: if the index already contains a line for this
    incident (e.g. task retry), we skip rather than duplicate. Never raises —
    the caller (summarization) must proceed to notify regardless.
    """
    try:
        synopsis = build_synopsis(alert_title, service, summary)
        line = format_index_line(
            incident_id=str(incident_id),
            date_iso=date_iso,
            service=service,
            status=status,
            synopsis=synopsis,
        )

        # Idempotency: don't re-append if this incident_id is already indexed.
        # Read is cheap (single artifact) and avoids duplicate lines on retry.
        existing = read_memory(
            category=INCIDENT_INDEX_CATEGORY,
            title=INCIDENT_INDEX_TITLE,
            user_id=user_id,
        )
        if existing and f"INC {incident_id}" in existing:
            logger.info(
                "%s Incident %s already in index; skipping append",
                _LOG_PREFIX, incident_id,
            )
            return True

        result = append_to_memory(
            category=INCIDENT_INDEX_CATEGORY,
            title=INCIDENT_INDEX_TITLE,
            content=line,
            description="Compact, id-keyed map of recent incidents for recurrence detection.",
            user_id=user_id,
            session_id=session_id,
        )
        logger.info("%s Appended incident %s to index", _LOG_PREFIX, incident_id)
        return "error" not in (result or "")
    except Exception:
        # Index maintenance must never break the notify path.
        logger.exception("%s Failed to append incident %s", _LOG_PREFIX, incident_id)
        return False


def read_index(user_id: str) -> str:
    """Return the raw Incident Index content (empty string if none/absent),
    trimmed to INDEX_INJECTION_CHAR_BUDGET so callers can inject it safely."""
    try:
        import json

        raw = read_memory(
            category=INCIDENT_INDEX_CATEGORY,
            title=INCIDENT_INDEX_TITLE,
            user_id=user_id,
        )
        if not raw:
            return ""
        data = json.loads(raw)
        if data.get("status") != "ok":
            return ""
        content = data.get("content") or ""
        # Keep the most recent lines: appends go to the end, so trim from the
        # front when over budget (grooming normally keeps this well under).
        if len(content) > INDEX_INJECTION_CHAR_BUDGET:
            content = content[-INDEX_INJECTION_CHAR_BUDGET:]
            # Drop a partial leading line after trimming.
            nl = content.find("\n")
            if nl != -1:
                content = content[nl + 1:]
        return content
    except Exception:
        logger.exception("%s Failed to read index for user %s", _LOG_PREFIX, user_id)
        return ""


def _clip(text: str) -> str:
    """Collapse whitespace and clip to the synopsis budget."""
    collapsed = " ".join((text or "").split())
    if len(collapsed) > _MAX_SYNOPSIS_CHARS:
        return collapsed[: _MAX_SYNOPSIS_CHARS - 1].rstrip() + "…"
    return collapsed
