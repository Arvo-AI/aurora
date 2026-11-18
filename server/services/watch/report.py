"""Render a group of findings as an incident: title, RCA payload, rail text, summary.

Pure functions. The payload is what the RCA agent receives as the "alert"; it is
bounded so it reaches the agent whole. Much of it was written by the cluster
(log lines, event messages, object names, values from pod specs) and can carry
attacker-controlled text, so all of that is also returned as rail text for the
prompt-injection guardrail, and the payload is trimmed until the rail sees it all.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

SOURCE_TYPE = "aurora_watch"
MAX_FINDINGS = 5
MAX_EVIDENCE = 6
MAX_SUSPECTS = 3
MAX_DETAILS = 4
MAX_LINE = 300
# Everything cluster-authored in the payload must fit what the input rail checks.
RAIL_MAX_CHARS = 6000

DETECTION_NOTE = (
    "Opened by Aurora Watch, not by an alert rule: these signals regressed against this "
    "cluster's own learned baseline. Suspected changes are ranked by how the changed object "
    "relates to the failing one and how close in time the change was; confirm or refute them."
)


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clip(text: Any) -> str:
    text = str(text)
    return text if len(text) <= MAX_LINE else text[: MAX_LINE - 3] + "..."


def incident_title(group: dict[str, Any]) -> str:
    return f"Aurora Watch: {group['title']}"[:200]


def incident_service(group: dict[str, Any]) -> str:
    top = group["findings"][0]
    return top.get("name") or "unknown"


def suspect_view(suspect: dict[str, Any]) -> dict[str, Any]:
    view = {
        "object": suspect["key"],
        "action": suspect.get("action"),
        "at": _iso(suspect.get("at")),
        "seconds_before_first_symptom": suspect.get("seconds_before"),
        "relation": suspect.get("relation"),
        "details": [_clip(d) for d in (suspect.get("details") or [])[:MAX_DETAILS]],
    }
    if suspect.get("during_gap"):
        # Made while polling had stopped: when is unknown, so give the window, not its start.
        view["at"] = None
        view["made_between"] = [_iso(suspect.get("at")), _iso(suspect.get("detected_at"))]
    return view


def finding_view(finding: dict[str, Any]) -> dict[str, Any]:
    return {
        "subject": finding["subject"],
        "severity": finding["severity"],
        "summary": finding["summary"],
        "first_seen": _iso(finding.get("first_seen")),
        # When the detector last blamed this finding: a later value means it was blamed again.
        "blamed_at": _iso(finding.get("blamed_at")),
        "signals": sorted({s["kind"] for s in finding.get("symptoms", [])}),
        "evidence": [_clip(e) for e in (finding.get("evidence") or [])[:MAX_EVIDENCE]],
    }


def _suspects(group: dict[str, Any]) -> list[dict[str, Any]]:
    """The group's suspects, best first, deduplicated across its findings."""
    seen, out = set(), []
    ranked = sorted((s for f in group["findings"] for s in f.get("suspects") or []),
                    key=lambda s: -s.get("score", 0))
    for s in ranked:
        ident = (s["key"], int(s.get("at") or 0))
        if ident not in seen:
            seen.add(ident)
            out.append(s)
    return out[:MAX_SUSPECTS]


def message(group: dict[str, Any], cluster_name: str) -> str:
    """One paragraph a person can read in a notification."""
    lines = [f"{group['title']} (cluster {cluster_name}, namespace {group['namespace']})."]
    others = [f["summary"] for f in group["findings"][1:MAX_FINDINGS]]
    if others:
        lines.append("Also: " + "; ".join(others) + ".")
    suspects = _suspects(group)
    if suspects:
        s = suspects[0]
        when = (f"{s.get('seconds_before')}s before the first symptom" if s.get("seconds_before") is not None
                else "during a polling gap" if s.get("during_gap") else "")
        detail = "; ".join(s.get("details") or [])[:200]
        lines.append(f"Suspected change: {s['key']} {s.get('action')} {when}: {detail}".strip())
    else:
        lines.append("No cluster change in the last hour relates to it; the cause may be outside the cluster.")
    return " ".join(lines)


def rca_payload(group: dict[str, Any], cluster: dict[str, Any]) -> dict[str, Any]:
    """The RCA's alert payload, trimmed so all of its cluster-authored text fits the rail."""
    cluster_name = cluster.get("name") or cluster.get("cluster_id") or "unknown"
    payload = {
        "title": incident_title(group),
        "status": "firing",
        "message": message(group, cluster_name),
        "source": SOURCE_TYPE,
        "severity": group["severity"],
        "cluster": {"id": cluster.get("cluster_id"), "name": cluster_name},
        "namespace": group["namespace"],
        "first_seen": _iso(min(f["first_seen"] for f in group["findings"])),
        "findings": [finding_view(f) for f in group["findings"][:MAX_FINDINGS]],
        "suspected_changes": [suspect_view(s) for s in _suspects(group)],
        "detection": DETECTION_NOTE,
    }
    _fit_to_rail(payload)
    return payload


def _rail_parts(payload: dict[str, Any]) -> list[str]:
    """Every string in the payload that came from the cluster (names, log lines, event
    messages, spec values), which is what the prompt-injection rail must see."""
    parts = [payload["title"], payload["message"], str(payload["cluster"].get("name")), payload["namespace"]]
    for f in payload["findings"]:
        parts += [f["subject"], f["summary"], *f["evidence"]]
    for s in payload["suspected_changes"]:
        parts += [s["object"], *s["details"]]
    return parts


def _fit_to_rail(payload: dict[str, Any]) -> None:
    """Drop evidence lines, then change details, from the end until the rail can read it all."""
    while len("\n".join(_rail_parts(payload))) > RAIL_MAX_CHARS:
        longest = max(payload["findings"], key=lambda f: len(f["evidence"]), default=None)
        if longest and longest["evidence"]:
            longest["evidence"].pop()
            continue
        detailed = max(payload["suspected_changes"], key=lambda s: len(s["details"]), default=None)
        if detailed and detailed["details"]:
            detailed["details"].pop()
            continue
        payload["message"] = payload["message"][:500]
        break


def rail_text(payload: dict[str, Any]) -> str:
    """The cluster-authored text of a payload built by :func:`rca_payload`, for the input rail."""
    return "\n".join(_rail_parts(payload))[:RAIL_MAX_CHARS]
