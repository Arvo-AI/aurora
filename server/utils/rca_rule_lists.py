"""Name-list cleaning shared by the Jira and ServiceNow RCA filter rules."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

MAX_VALUES = 30
MAX_NAME = 80


def clean_name(value: Any, label: str, max_name: int = MAX_NAME) -> Tuple[Optional[str], Optional[str]]:
    if not isinstance(value, str):
        return None, f"{label} must be text"
    cleaned = " ".join(value.split())
    if not cleaned or len(cleaned) > max_name:
        return None, f"Each {label} must be 1–{max_name} characters"
    return cleaned, None


def clean_name_list(raw: Any, label: str, max_values: int = MAX_VALUES) -> Tuple[Optional[List[str]], Optional[str]]:
    """Dedupe a list of names. Missing means no constraint, not an error."""
    if raw is None:
        return [], None
    if not isinstance(raw, list):
        return None, f"{label} must be a list"
    seen: Dict[str, str] = {}
    for item in raw:
        # A blank entry is an empty chip, not a filter value.
        if isinstance(item, str) and not item.strip():
            continue
        cleaned, error = clean_name(item, label)
        if error or cleaned is None:
            return None, error
        folded = cleaned.casefold()
        if folded not in seen:
            seen[folded] = cleaned
    if len(seen) > max_values:
        return None, f"At most {max_values} {label}"
    return list(seen.values()), None


def check_rule_list(raw: Any, max_rules: int) -> Optional[str]:
    """The error for a rules payload that is not a bounded list, else None."""
    if not isinstance(raw, list):
        return "rcaRules must be a list"
    if len(raw) > max_rules:
        return f"At most {max_rules} filters can be configured"
    return None
