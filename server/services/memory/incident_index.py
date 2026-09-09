"""
Incident Index — the recurrence discovery map.

One self-maintaining memory artifact per org (category="artifact",
title="Incident Index") holding a compact, id-keyed line per incident. The
recurrence agent scans it to find candidate anchors, then drills into a
specific incident via get_incident. Written deterministically at RCA completion
(append_incident_line) and groomed by the nightly memory_consolidation action.
"""

import logging
import re
from typing import Optional

from services.memory import INCIDENT_INDEX_CATEGORY, INCIDENT_INDEX_TITLE
from chat.backend.agent.tools.memory_tool import (
    append_to_memory,
    edit_memory,
    read_memory,
)

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

    This is the deterministic fallback. The richer, cause-focused synopsis is
    produced by generate_root_cause_synopsis (one LLM call) when a report is
    available; this function is what that falls back to on any failure.
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


# Prompt for the dedicated one-sentence root-cause synopsis. Kept cause-focused
# on purpose: the recurrence agent matches on the *mechanism*, not the symptom,
# so "connection pool exhaustion in payments-db" beats "High CPU on payments-api".
_SYNOPSIS_PROMPT = """You are compressing a completed incident report into ONE line for a recurrence-detection index.

Output a single sentence, at most 20 words, naming the ROOT CAUSE — the underlying mechanism and component that failed. NOT the symptom, NOT the alert name.

Good: "Connection pool exhaustion in payments-db under checkout load spike"
Good: "OOM kill of api-worker after ConfigMap raised BATCH_SIZE to 10000"
Bad (symptom only): "High CPU on payments-api"
Bad (too vague): "A production incident occurred"

If the report does not identify a cause, output the alert title verbatim.
Respond with ONLY the sentence — no quotes, no prefix, no trailing punctuation beyond a period.

ALERT TITLE: {alert_title}
SERVICE: {service}

INCIDENT REPORT:
{summary}"""


def generate_root_cause_synopsis(
    *,
    user_id: str,
    session_id: Optional[str],
    alert_title: str,
    service: str,
    summary: str,
) -> str:
    """One cheap LLM call that turns the RCA report into a cause-focused line.

    Degrades gracefully: on any failure, empty summary, or junk output, falls
    back to build_synopsis() (alert title). Never raises. Uses the same model
    and usage tracking as the report that was just written.
    """
    fallback = build_synopsis(alert_title, service, summary)

    # No report to compress — the deterministic fallback is all we have.
    if not (summary or "").strip():
        return fallback

    try:
        from chat.backend.agent.providers import create_chat_model
        from chat.backend.agent.llm import ModelConfig
        from chat.backend.agent.utils.llm_usage_tracker import tracked_invoke
        from chat.backend.agent.utils.message_content import extract_text_from_content
        from langchain_core.messages import HumanMessage

        prompt = _SYNOPSIS_PROMPT.format(
            alert_title=(alert_title or "(none)"),
            service=(service or "(unknown)"),
            # Cap the report we feed in — the tail carries the conclusion, and
            # this keeps the tiny call tiny.
            summary=summary[-6000:],
        )
        llm = create_chat_model(
            ModelConfig.INCIDENT_REPORT_SUMMARIZATION_MODEL,
            temperature=0.0,
            streaming=False,
        )
        response = tracked_invoke(
            llm,
            [HumanMessage(content=prompt)],
            user_id=user_id,
            session_id=session_id,
            model_name=ModelConfig.INCIDENT_REPORT_SUMMARIZATION_MODEL,
            request_type="incident_index_synopsis",
        )
        text = extract_text_from_content(response.content).strip()
        # Guard against empty / refusal / runaway output — fall back rather than
        # poison the index with a bad line.
        if not text or len(text) < 3:
            return fallback
        return _clip(text)
    except Exception:
        logger.exception("%s Synopsis LLM call failed; using fallback", _LOG_PREFIX)
        return fallback


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
    synopsis: Optional[str] = None,
    session_id: Optional[str] = None,
) -> bool:
    """Append one incident's line to the org's Incident Index. Best-effort.

    Idempotent by incident_id: if the index already contains a line for this
    incident (e.g. task retry), we skip rather than duplicate. Never raises —
    the caller (summarization) must proceed to notify regardless.

    If *synopsis* is provided (e.g. the LLM root-cause line), it is used as-is;
    otherwise a deterministic synopsis is derived from title/summary.
    """
    try:
        line_synopsis = _clip(synopsis) if synopsis and synopsis.strip() \
            else build_synopsis(alert_title, service, summary)
        line = format_index_line(
            incident_id=str(incident_id),
            date_iso=date_iso,
            service=service,
            status=status,
            synopsis=line_synopsis,
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


def _read_index_raw(user_id: str) -> str:
    """Untrimmed index content for read-modify-write (grooming/roll-ups).

    read_index() trims to the injection budget for prompt safety; roll-ups
    must operate on the full artifact so a trimmed-off root line isn't lost.
    Returns "" on any failure/absence.
    """
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
        return data.get("content") or ""
    except Exception:
        logger.exception("%s Failed to read raw index for user %s", _LOG_PREFIX, user_id)
        return ""


# Matches the roll-up continuation line beneath a root line, capturing the
# comma-separated recurrence ids so we can extend it idempotently.
_ROLLUP_RE = re.compile(r"^\s*↳ recurrences: (?P<ids>.*?) \((?P<n>\d+) total")


def record_recurrence(
    *,
    user_id: str,
    root_id: str,
    recurred_id: str,
    date_iso: str,
) -> bool:
    """Roll a folded incident up under its ROOT's existing index line.

    A recurrence does NOT get its own top-level line (that would create a
    sibling the recurrence agent could wrongly fold into). Instead we append
    `recurred_id` to the root line's `↳ recurrences:` continuation, keeping the
    root as the single anchor. Idempotent on recurred_id. Best-effort; never
    raises.

    If the root line isn't in the index (cold start, trimmed, or root predates
    the index), fall back to appending a standalone line for the recurrence so
    it is at least recorded and joinable next groom.
    """
    try:
        content = _read_index_raw(user_id)
        root_token = f"INC {root_id}"

        # Idempotency across the whole artifact: if this recurrence is already
        # recorded (as a roll-up id or a standalone line), do nothing.
        if content and f"INC {recurred_id}" in content:
            return True

        # Root not indexed yet — record the recurrence as its own line rather
        # than silently dropping it; grooming will cluster it later.
        if not content or root_token not in content:
            logger.info(
                "%s Root %s not in index; recording recurrence %s as standalone line",
                _LOG_PREFIX, root_id, recurred_id,
            )
            fallback_line = format_index_line(
                incident_id=str(recurred_id),
                date_iso=date_iso,
                service="",
                status="recurrence",
                synopsis=f"recurrence of {root_id}",
            )
            res = append_to_memory(
                category=INCIDENT_INDEX_CATEGORY,
                title=INCIDENT_INDEX_TITLE,
                content=fallback_line,
                user_id=user_id,
            )
            return "error" not in (res or "")

        lines = content.split("\n")
        # Find the root's top-level line and its (optional) roll-up continuation.
        root_idx = next(
            (i for i, ln in enumerate(lines)
             if ln.lstrip().startswith("- [") and root_token in ln),
            None,
        )
        if root_idx is None:
            return False

        rollup_idx = (
            root_idx + 1
            if root_idx + 1 < len(lines) and lines[root_idx + 1].lstrip().startswith("↳ recurrences:")
            else None
        )

        # Gather current recurrence ids from the existing roll-up (if any).
        ids: list = []
        if rollup_idx is not None:
            m = _ROLLUP_RE.match(lines[rollup_idx])
            if m:
                ids = [t.strip() for t in m.group("ids").split(",") if t.strip()]

        # Idempotency: already rolled up (e.g. task retry) — nothing to do.
        if str(recurred_id) in ids:
            return True

        ids.append(str(recurred_id))
        total = len(ids) + 1  # + the root itself
        indent = lines[root_idx][: len(lines[root_idx]) - len(lines[root_idx].lstrip())]
        new_rollup = (
            f"{indent}  ↳ recurrences: {', '.join(ids)} "
            f"({total} total, last {date_iso or '?'})"
        )

        # Replace the existing roll-up, or insert one right under the root line.
        if rollup_idx is not None:
            old_block = lines[root_idx] + "\n" + lines[rollup_idx]
            new_block = lines[root_idx] + "\n" + new_rollup
        else:
            old_block = lines[root_idx]
            new_block = lines[root_idx] + "\n" + new_rollup

        res = edit_memory(
            category=INCIDENT_INDEX_CATEGORY,
            title=INCIDENT_INDEX_TITLE,
            old_text=old_block,
            new_text=new_block,
            user_id=user_id,
        )
        ok = "error" not in (res or "") and "no_match" not in (res or "")
        if ok:
            logger.info(
                "%s Rolled recurrence %s under root %s (%d total)",
                _LOG_PREFIX, recurred_id, root_id, total,
            )
        return ok
    except Exception:
        logger.exception(
            "%s Failed to record recurrence %s under root %s",
            _LOG_PREFIX, recurred_id, root_id,
        )
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
