"""Pure-function CQL query builders for Confluence search."""

from __future__ import annotations

import re
from typing import List, Optional


_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_HEX_RE = re.compile(r"\b0x[0-9a-fA-F]{6,}\b")
_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)
_BASE64_RE = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")

# CQL labels commonly associated with incident / operations docs
_INCIDENT_LABELS = (
    "postmortem",
    "rca",
    "runbook",
    "incident",
    "outage",
    "sev1",
    "sev2",
)


def escape_cql_text(text: str) -> str:
    """Escape a string for safe inclusion in a CQL ``text ~ "..."`` clause."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def clean_error_message(error: str) -> str:
    """Strip volatile tokens from an error string so CQL matches are stable."""
    cleaned = _TIMESTAMP_RE.sub("", error)
    cleaned = _UUID_RE.sub("", cleaned)
    cleaned = _IP_RE.sub("", cleaned)
    cleaned = _HEX_RE.sub("", cleaned)
    cleaned = _BASE64_RE.sub("", cleaned)
    # collapse whitespace
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # keep only the first 200 chars for a reasonable CQL clause
    return cleaned[:200]


def build_similar_incidents_cql(
    keywords: List[str],
    service_name: Optional[str] = None,
    error_message: Optional[str] = None,
    spaces: Optional[List[str]] = None,
    labels: Optional[List[str]] = None,
    days_back: int = 365,
) -> str:
    """Build a CQL query to find pages that look like previous incidents.

    Combines a ``text ~`` full-text clause with optional label, space, and
    date filters.  Results are ordered by last-modified descending.
    """
    clauses: List[str] = []

    # Full-text clause — merge keywords, service, and cleaned error snippet
    search_parts: List[str] = list(keywords or [])
    if service_name:
        search_parts.append(service_name)
    if error_message:
        cleaned = clean_error_message(error_message)
        if cleaned:
            search_parts.append(cleaned)

    if search_parts:
        combined = " ".join(search_parts)
        clauses.append(f'text ~ "{escape_cql_text(combined)}"')

    # Labels filter
    target_labels = list(labels) if labels else list(_INCIDENT_LABELS)
    label_csv = ", ".join(f'"{escape_cql_text(l)}"' for l in target_labels)
    clauses.append(f"label in ({label_csv})")

    # Space filter
    if spaces:
        space_csv = ", ".join(f'"{escape_cql_text(s)}"' for s in spaces)
        clauses.append(f"space in ({space_csv})")

    # Page type only
    clauses.append('type = "page"')

    # Recency filter
    if days_back > 0:
        clauses.append(f'created >= now("-{days_back}d")')

    cql = " AND ".join(clauses) + " ORDER BY lastmodified DESC"
    return cql


def build_runbook_search_cql(
    service_name: str,
    operation: Optional[str] = None,
    spaces: Optional[List[str]] = None,
) -> str:
    """Build a CQL query to locate runbooks for a given service."""
    search_parts = [service_name]
    if operation:
        search_parts.append(operation)
    search_parts.append("runbook")

    combined = " ".join(search_parts)
    clauses: List[str] = [f'text ~ "{escape_cql_text(combined)}"']

    runbook_labels = ("runbook", "playbook", "sop", "procedure")
    label_csv = ", ".join(f'"{escape_cql_text(l)}"' for l in runbook_labels)
    clauses.append(f"label in ({label_csv})")

    if spaces:
        space_csv = ", ".join(f'"{escape_cql_text(s)}"' for s in spaces)
        clauses.append(f"space in ({space_csv})")

    clauses.append('type = "page"')
    cql = " AND ".join(clauses) + " ORDER BY lastmodified DESC"
    return cql
