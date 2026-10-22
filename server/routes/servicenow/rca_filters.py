"""Which ServiceNow incidents start an RCA.

In Filters mode an incident matches when its priority, assignment group,
category and state hit a saved rule; an encoded query is an optional extra
clause the task confirms against the Table API. In Auto mode the judge
decides. An update to an incident Aurora already tracks only syncs status.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from utils.rca_rule_lists import check_rule_list, clean_name_list

logger = logging.getLogger(__name__)

_MAX_RCA_RULES = 50
_MAX_QUERY = 500

# ServiceNow incident priorities and states are fixed choice lists.
PRIORITY_VALUES = ("1", "2", "3", "4", "5")
PRIORITY_LABELS = {"1": "1 - Critical", "2": "2 - High", "3": "3 - Moderate", "4": "4 - Low", "5": "5 - Planning"}
STATE_VALUES = ("1", "2", "3", "6", "7", "8")
STATE_LABELS = {
    "1": "New", "2": "In Progress", "3": "On Hold", "6": "Resolved", "7": "Closed", "8": "Canceled",
}
_STATE_BY_LABEL = {label.casefold(): value for value, label in STATE_LABELS.items()}
_STATE_BY_LABEL["cancelled"] = "8"

# Any-of name lists. Empty means that dimension does not narrow the rule.
NAME_FIELDS = ("assignmentGroups", "categories")
# Any-of choice lists, stored as the ServiceNow value ("1".."5", "1".."8").
CHOICE_FIELDS = (("priorities", PRIORITY_VALUES), ("states", STATE_VALUES))
FILTER_FIELDS = NAME_FIELDS + tuple(field for field, _ in CHOICE_FIELDS)

_CONTROL_RE = re.compile(r"[\x00-\x1f]")
_DIGIT_RE = re.compile(r"[1-8]")


def _clean_choice_list(raw: Any, label: str, allowed: Tuple[str, ...]) -> Tuple[Optional[List[str]], Optional[str]]:
    """Choice values as strings, in catalog order. Labels ("2 - High") are accepted."""
    if raw is None:
        return [], None
    if not isinstance(raw, list):
        return None, f"{label} must be a list"
    chosen: set = set()
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, (int, str)):
            return None, f"{label} must be ServiceNow values"
        text = str(item).strip()
        if not text:
            continue
        match = _DIGIT_RE.search(text)
        value = match.group(0) if match else ""
        if value not in allowed:
            return None, f"{label} must be one of: {', '.join(allowed)}"
        chosen.add(value)
    return [value for value in allowed if value in chosen], None


def _clean_query(raw: Any) -> Tuple[Optional[str], Optional[str]]:
    """An encoded-query clause ANDed onto ``sys_id=<id>``.

    ``^NQ`` starts a new top-level query and a leading ``^`` would glue onto
    the sys_id term, so both are rejected. The task also checks the record the
    query returns is the one being judged, so an ``^OR`` cannot widen a match.
    """
    if raw is None or raw == "":
        return "", None
    if not isinstance(raw, str):
        return None, "query must be text"
    cleaned = raw.strip()
    if not cleaned:
        return "", None
    if len(cleaned) > _MAX_QUERY:
        return None, f"query must be at most {_MAX_QUERY} characters"
    if _CONTROL_RE.search(cleaned):
        return None, "query must not contain control characters"
    if cleaned.startswith("^") or cleaned.endswith("^") or "^NQ" in cleaned.upper():
        return None, "query must be a single encoded-query clause (no leading ^ and no ^NQ)"
    return cleaned, None


def _clean_rule(item: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not isinstance(item, dict):
        return None, "Each filter must be an object"
    rule: Dict[str, Any] = {}
    for field in NAME_FIELDS:
        rule[field], error = clean_name_list(item.get(field), field)
        if error:
            return None, error
    for field, allowed in CHOICE_FIELDS:
        rule[field], error = _clean_choice_list(item.get(field), field, allowed)
        if error:
            return None, error
    rule["query"], error = _clean_query(item.get("query"))
    if error:
        return None, error
    # An empty rule would match every incident; that must be a deliberate choice.
    if not any(rule[field] for field in FILTER_FIELDS) and not rule["query"]:
        return None, "Each filter needs at least one priority, assignment group, category, state, or query"
    return rule, None


def _signature(rule: Dict[str, Any]) -> Tuple[Any, ...]:
    lists = tuple(
        tuple(sorted(str(name).casefold() for name in rule[field]))
        for field in FILTER_FIELDS
    )
    return lists + (rule["query"],)


def normalize_rca_rules(raw: Any) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Validate rules. An empty list means nothing auto-triggers."""
    error = check_rule_list(raw, _MAX_RCA_RULES)
    if error:
        return None, error
    merged: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    for item in raw:
        rule, error = _clean_rule(item)
        if error or rule is None:
            return None, error
        # Two identical rules collapse into one row.
        merged.setdefault(_signature(rule), rule)
    return list(merged.values()), None


def _folded(value: Any) -> str:
    return str(value or "").strip().casefold()


def state_value(payload: Dict[str, Any]) -> str:
    """The numeric state ("1".."8") from a webhook payload; "" when absent."""
    raw = str(payload.get("state") or "").strip()
    if raw in STATE_LABELS:
        return raw
    label = _folded(payload.get("state_label") or raw)
    return _STATE_BY_LABEL.get(label, "")


def extract_webhook_scope(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The fields a rule can match, as the Business Rule sends them."""
    from routes.servicenow.tasks import incident_priority

    priority = incident_priority(payload)
    return {
        "number": str(payload.get("number") or "").strip(),
        "sysId": str(payload.get("sys_id") or "").strip(),
        "priority": str(priority) if priority else "",
        "assignmentGroup": str(payload.get("assignment_group") or "").strip(),
        "category": str(payload.get("category") or "").strip(),
        "state": state_value(payload),
    }


def _any_of(rule_values: Any, actual: Any) -> bool:
    """True when the rule leaves this dimension open, or the incident hits one value."""
    if not isinstance(rule_values, list) or not rule_values:
        return True
    wanted = {_folded(name) for name in rule_values if isinstance(name, (str, int))}
    return _folded(actual) in wanted


def rule_matches_scope(scope: Dict[str, Any], rule: Dict[str, Any]) -> bool:
    """Structured fields only. A saved query clause is checked separately."""
    if not isinstance(rule, dict):
        return False
    if not _any_of(rule.get("priorities"), scope.get("priority")):
        return False
    if not _any_of(rule.get("assignmentGroups"), scope.get("assignmentGroup")):
        return False
    if not _any_of(rule.get("categories"), scope.get("category")):
        return False
    if not _any_of(rule.get("states"), scope.get("state")):
        return False
    return True


def match_kind(scope: Dict[str, Any], rules: Any) -> str:
    """'match' when a rule hits without a query, 'query' when only query rules hit, else 'miss'."""
    if not isinstance(rules, list):
        return "miss"
    needs_query = False
    for rule in rules:
        if not rule_matches_scope(scope, rule):
            continue
        # A rule with no query is already decisive.
        if str(rule.get("query") or "").strip():
            needs_query = True
            continue
        return "match"
    return "query" if needs_query else "miss"


def matching_query_clauses(scope: Dict[str, Any], rules: Any) -> List[str]:
    clauses: List[str] = []
    if not isinstance(rules, list):
        return clauses
    for rule in rules:
        if not rule_matches_scope(scope, rule):
            continue
        query = str(rule.get("query") or "").strip()
        if query:
            clauses.append(query)
    return clauses


def query_matches_incident(client: Any, sys_id: str, clauses: List[str]) -> bool:
    """True when any saved clause still selects this incident.

    The query is ``sys_id=<id>^<clause>`` and the returned record must be that
    sys_id, so a clause with ``^OR`` cannot match through another incident. A
    400 is a broken clause and counts as a miss. Other errors propagate so the
    task can retry.
    """
    from routes.servicenow.servicenow_client import ServiceNowAPIError, is_sys_id

    if not is_sys_id(sys_id):
        return False
    for clause in clauses:
        try:
            rows = client.list_incidents(query=f"sys_id={sys_id}^{clause}", limit=1, fields="sys_id")
        except ServiceNowAPIError as exc:
            if exc.status_code == 400:
                logger.info("[SERVICENOW][WEBHOOK] Saved RCA query was rejected for %s", sys_id)
                continue
            raise
        if any(isinstance(row, dict) and str(row.get("sys_id") or "") == sys_id for row in rows):
            return True
    return False


def decide_webhook(payload: Dict[str, Any], rules: Any, mode: str = "filters") -> Dict[str, Any]:
    """What a new incident should do: start RCA, confirm a query, ask the judge, or ignore.

    Only incidents Aurora has not seen are decided here; the task syncs
    updates to known incidents regardless of the decision.
    """
    scope = extract_webhook_scope(payload)
    # Auto replaces the filters: the judge alone decides.
    if mode == "auto":
        return {"action": "judge", "scope": scope}
    kind = match_kind(scope, rules)
    if kind == "match":
        return {"action": "rca", "scope": scope}
    if kind == "query":
        return {"action": "query", "scope": scope, "query": matching_query_clauses(scope, rules)}
    return {"action": "ignore", "reason": "filters", "scope": scope}
