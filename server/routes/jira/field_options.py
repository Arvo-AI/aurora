"""Jira returns these catalogs as strings or {name} objects, and repeats each
status under every issue type. Collapse that to one name per value.
"""

from __future__ import annotations

from typing import Any, List

# Saved filters reject longer names, so a longer label cannot be selected.
_MAX_NAME_LEN = 80


def unique_names(items: Any) -> List[str]:
    """One name per value. Jira sends either a string or a {name} object."""
    if not isinstance(items, list):
        return []
    seen: dict[str, str] = {}
    for item in items:
        if isinstance(item, str):
            name = " ".join(item.split())
        elif isinstance(item, dict):
            name = " ".join(str(item.get("name") or "").split())
        else:
            continue
        folded = name.casefold()
        # Blank or over-long names cannot be stored on a filter.
        if not name or len(name) > _MAX_NAME_LEN or folded in seen:
            continue
        seen[folded] = name
    return sorted(seen.values(), key=str.casefold)


def status_names(payload: Any) -> List[str]:
    """One status name. Jira lists the same status once per issue type."""
    if not isinstance(payload, list):
        return []
    flat: List[Any] = []
    for issue_type in payload:
        # Each entry is an issue type; its statuses are the selectable values.
        if isinstance(issue_type, dict):
            flat.extend(issue_type.get("statuses") or [])
    return unique_names(flat)
