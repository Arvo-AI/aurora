"""Which Jira tickets start an RCA.

In Filters mode a ticket matches when its project, type, and any extra fields
hit a saved rule; JQL is an optional extra clause checked by the caller. In Auto
mode the judge decides. Comments never start an RCA.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from routes.jira.field_options import unique_names

logger = logging.getLogger(__name__)

# Jira project keys are a letter plus letters, digits, or underscores.
PROJECT_KEY_RE = re.compile(r"^[A-Za-z]\w{0,31}$", re.ASCII)
_ISSUE_KEY_RE = re.compile(r"^[A-Za-z]\w+-\d+$", re.ASCII)
_MAX_RCA_RULES = 50
_MAX_VALUES = 30
_MAX_JQL = 500

# Optional any-of fields. Empty means that dimension does not narrow the rule.
FILTER_FIELDS = ("priorities", "labels", "components", "statuses")


def _clean_name(value: Any, label: str) -> Tuple[Optional[str], Optional[str]]:
    if not isinstance(value, str):
        return None, f"{label} must be text"
    cleaned = " ".join(value.split())
    if not cleaned or len(cleaned) > 80:
        return None, f"Each {label} must be 1–80 characters"
    return cleaned, None


def _clean_name_list(raw: Any, label: str) -> Tuple[Optional[List[str]], Optional[str]]:
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
        cleaned, error = _clean_name(item, label)
        if error or cleaned is None:
            return None, error
        folded = cleaned.casefold()
        if folded not in seen:
            seen[folded] = cleaned
    if len(seen) > _MAX_VALUES:
        return None, f"At most {_MAX_VALUES} {label}"
    return list(seen.values()), None


def _jql_is_contained(jql: str) -> bool:
    """True when parentheses stay inside the clause we AND onto the issue key."""
    depth = 0
    for char in _unquoted_chars(jql):
        # A quoted value never reaches here, so its parentheses are just text.
        if char is None:
            return False
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            # A leading close would end our wrapper and OR the rest onto every issue.
            if depth < 0:
                return False
    return depth == 0


def _unquoted_chars(jql: str):
    """Characters outside quoted values; ``None`` last if a quote never closes."""
    quote = None
    escaped = False
    for char in jql:
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        else:
            yield char
    # An unclosed quote would let a later OR escape the issue-key wrapper.
    if quote:
        yield None


def _clean_jql(raw: Any) -> Tuple[Optional[str], Optional[str]]:
    if raw is None or raw == "":
        return "", None
    if not isinstance(raw, str):
        return None, "jql must be text"
    cleaned = " ".join(raw.split())
    if not cleaned:
        return "", None
    if len(cleaned) > _MAX_JQL:
        return None, f"jql must be at most {_MAX_JQL} characters"
    if not _jql_is_contained(cleaned):
        return None, "jql parentheses must stay balanced"
    return cleaned, None


def _filter_signature(rule: Dict[str, Any]) -> Tuple[Any, ...]:
    """Identity of the optional clauses, so same-project rules only merge when equal."""
    lists = tuple(
        tuple(sorted(name.casefold() for name in rule[field]))
        for field in FILTER_FIELDS
    )
    return lists + (rule["jql"],)


def _clean_rule(item: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not isinstance(item, dict):
        return None, "Each filter needs a projectKey and issueTypes"
    project_key = str(item.get("projectKey") or "").strip().upper()
    # Reject anything that is not a project key so a payload cannot widen the match.
    if not PROJECT_KEY_RE.fullmatch(project_key):
        return None, "projectKey must look like a Jira project key"
    issue_types, error = _clean_name_list(item.get("issueTypes"), "ticket types")
    if error or not issue_types:
        return None, error or "issueTypes must list at least one ticket type"

    rule: Dict[str, Any] = {"projectKey": project_key, "issueTypes": issue_types}
    for field in FILTER_FIELDS:
        rule[field], error = _clean_name_list(item.get(field), field)
        if error:
            return None, error
    rule["jql"], error = _clean_jql(item.get("jql"))
    if error:
        return None, error
    return rule, None


def normalize_rca_rules(raw: Any) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Validate rules. An empty list means nothing auto-triggers."""
    if not isinstance(raw, list):
        return None, "rcaRules must be a list"
    if len(raw) > _MAX_RCA_RULES:
        return None, f"At most {_MAX_RCA_RULES} filters can be configured"

    merged: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    for item in raw:
        rule, error = _clean_rule(item)
        if error or rule is None:
            return None, error
        slot = (rule["projectKey"],) + _filter_signature(rule)
        # A new project, or the same project with different limits, is its own row.
        if slot not in merged:
            merged[slot] = rule
            continue
        # Same project and same limits combine their ticket types.
        types, error = _clean_name_list(merged[slot]["issueTypes"] + rule["issueTypes"], "ticket types")
        if error:
            return None, error
        merged[slot]["issueTypes"] = types
    return list(merged.values()), None


def _folded(value: Any) -> str:
    return str(value or "").strip().casefold()


def extract_webhook_scope(issue: Dict[str, Any]) -> Dict[str, Any]:
    """Project, type, and the optional filter fields from a webhook issue."""
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    project = fields.get("project") if isinstance(fields.get("project"), dict) else {}
    project_key = str(project.get("key") or "").strip()
    # Some automation bodies send only the issue key (PROJ-123).
    issue_key = str(issue.get("key") or "")
    if not project_key:
        head, sep, tail = issue_key.rpartition("-")
        if sep and head and tail.isdigit():
            project_key = head
    issuetype = fields.get("issuetype") if isinstance(fields.get("issuetype"), dict) else {}
    priority = fields.get("priority") if isinstance(fields.get("priority"), dict) else {}
    status = fields.get("status") if isinstance(fields.get("status"), dict) else {}
    return {
        "projectKey": project_key,
        "issueType": str(issuetype.get("name") or "").strip(),
        "priority": str(priority.get("name") or "").strip(),
        "status": str(status.get("name") or "").strip(),
        "labels": unique_names(fields.get("labels"), sort=False),
        "components": unique_names(fields.get("components"), sort=False),
        "issueKey": issue_key,
    }


def _any_of(rule_values: Any, actual: Any) -> bool:
    """True when the rule leaves this dimension open, or the ticket hits one value."""
    if not isinstance(rule_values, list) or not rule_values:
        return True
    wanted = {_folded(name) for name in rule_values if isinstance(name, str)}
    if isinstance(actual, list):
        have = {_folded(name) for name in actual}
        return bool(wanted & have)
    return _folded(actual) in wanted


def rule_matches_scope(scope: Dict[str, Any], rule: Dict[str, Any]) -> bool:
    """Structured fields only. A saved JQL clause is checked separately."""
    if _folded(rule.get("projectKey")) != _folded(scope.get("projectKey")):
        return False
    if not scope.get("issueType"):
        return False
    types = rule.get("issueTypes") or []
    type_hit = any(
        _folded(name) == _folded(scope.get("issueType"))
        for name in types
        if isinstance(name, str)
    )
    if not type_hit:
        return False
    if not _any_of(rule.get("priorities"), scope.get("priority")):
        return False
    if not _any_of(rule.get("labels"), scope.get("labels")):
        return False
    if not _any_of(rule.get("components"), scope.get("components")):
        return False
    if not _any_of(rule.get("statuses"), scope.get("status")):
        return False
    return True


def match_kind(scope: Dict[str, Any], rules: Any) -> str:
    """'match' when a rule hits without JQL, 'jql' when only JQL rules hit, else 'miss'."""
    if not isinstance(rules, list):
        return "miss"
    needs_jql = False
    for rule in rules:
        if not isinstance(rule, dict) or not rule_matches_scope(scope, rule):
            continue
        # A rule with no JQL is already decisive — later JQL rules are not required.
        if str(rule.get("jql") or "").strip():
            needs_jql = True
            continue
        return "match"
    return "jql" if needs_jql else "miss"


def matching_jql_clauses(scope: Dict[str, Any], rules: Any) -> List[str]:
    clauses: List[str] = []
    if not isinstance(rules, list):
        return clauses
    for rule in rules:
        if not isinstance(rule, dict) or not rule_matches_scope(scope, rule):
            continue
        jql = str(rule.get("jql") or "").strip()
        if jql:
            clauses.append(jql)
    return clauses


def jql_matches_issue(client: Any, issue_key: str, clauses: List[str]) -> bool:
    """True when any saved clause still selects this issue.

    A 400 is a broken clause and counts as a miss. Other HTTP errors propagate
    so the webhook can retry.
    """
    import requests

    from utils.log_sanitizer import sanitize

    if not _ISSUE_KEY_RE.fullmatch(issue_key or ""):
        return False
    for clause in clauses:
        query = f'key = "{issue_key}" AND ({clause})'
        try:
            result = client.search_issues(query, fields=["key"], max_results=1)
        except requests.HTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            # Jira rejected the clause. Retrying the webhook will not fix it.
            if status == 400:
                logger.info(
                    "[JIRA][WEBHOOK] Saved RCA JQL was rejected for %s",
                    sanitize(issue_key),
                )
                continue
            raise
        issues = result.get("issues") or []
        # Any hit is not enough: a clause that breaks out of the key wrapper can return a different issue.
        if any(
            isinstance(issue, dict) and str(issue.get("key") or "").casefold() == issue_key.casefold()
            for issue in issues
        ):
            return True
    return False


# Created and edited only. A delete payload still carries the old body.
_COMMENT_WEBHOOK_EVENTS = frozenset({"comment_created", "comment_updated"})
_COMMENT_ISSUE_EVENTS = frozenset({"issue_commented", "issue_comment_edited"})


def is_comment_event(payload: Dict[str, Any]) -> bool:
    """True for comment create/edit webhooks. Those never start an RCA."""
    event = str(payload.get("webhookEvent") or "")
    event_name = str(payload.get("issue_event_type_name") or "")
    return event in _COMMENT_WEBHOOK_EVENTS or event_name in _COMMENT_ISSUE_EVENTS


def decide_webhook(payload: Dict[str, Any], rules: Any, mode: str = "filters") -> Dict[str, Any]:
    """What a webhook should do: start RCA, check JQL, ask the judge, or ignore."""
    issue = payload.get("issue") if isinstance(payload, dict) else None
    if not isinstance(issue, dict) or not issue:
        return {"action": "ignore", "reason": "no issue"}

    # A comment is not a ticket arriving. It does not start an RCA.
    if is_comment_event(payload):
        return {"action": "ignore", "reason": "comment"}

    scope = extract_webhook_scope(issue)
    # Auto replaces the filters: the judge alone decides.
    if mode == "auto":
        return {"action": "judge", "scope": scope}

    kind = match_kind(scope, rules)
    if kind == "match":
        return {"action": "rca", "scope": scope}
    if kind == "jql":
        return {
            "action": "jql",
            "scope": scope,
            "jql": matching_jql_clauses(scope, rules),
        }
    return {"action": "ignore", "reason": "filters", "scope": scope}
