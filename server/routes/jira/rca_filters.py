"""Programmatic filters for which Jira tickets start an RCA.

A ticket matches when its project, type, and any extra fields hit a saved
rule. JQL is an optional extra clause checked by the caller. Comments never
start an RCA — in auto mode they are handed to the filter agent instead.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Jira project keys are a letter plus letters, digits, or underscores.
PROJECT_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
_ISSUE_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]+-\d+$")
_MAX_RCA_RULES = 50
_MAX_VALUES = 30
_MAX_JQL = 500

# Optional any-of fields. Empty means that dimension does not narrow the rule.
FILTER_FIELDS = ("priorities", "labels", "components", "statuses")
FILTER_MODES = ("auto", "manual")


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
    quote = None
    escaped = False
    for char in jql:
        # A parenthesis inside a quoted value is text, not a group we have to balance.
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            # A leading close would end our wrapper and OR the rest onto every issue.
            if depth < 0:
                return False
    # An unclosed quote would let a later OR escape the issue-key wrapper.
    return depth == 0 and quote is None


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


def _merge_issue_types(
    existing: Optional[List[str]],
    types_raw: list,
) -> Tuple[Optional[List[str]], Optional[str]]:
    seen: Dict[str, str] = {}
    if existing:
        for name in existing:
            seen[name.casefold()] = name
    for type_name in types_raw:
        cleaned, error = _clean_name(type_name, "issue type")
        if error or cleaned is None:
            return None, error
        folded = cleaned.casefold()
        if folded not in seen:
            seen[folded] = cleaned
    if len(seen) > _MAX_VALUES:
        return None, f"At most {_MAX_VALUES} ticket types per rule"
    return list(seen.values()), None


def normalize_rca_rules(raw: Any) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Validate rules. An empty list means nothing auto-triggers."""
    if not isinstance(raw, list):
        return None, "rcaRules must be a list"
    if len(raw) > _MAX_RCA_RULES:
        return None, f"At most {_MAX_RCA_RULES} filters can be configured"

    merged: List[Dict[str, Any]] = []
    index_by_slot: Dict[Tuple[Any, ...], int] = {}
    for item in raw:
        if not isinstance(item, dict):
            return None, "Each filter needs a projectKey and issueTypes"
        project_key = str(item.get("projectKey") or "").strip().upper()
        # Reject anything that is not a project key so a payload cannot widen the match.
        if not PROJECT_KEY_RE.fullmatch(project_key):
            return None, "projectKey must look like a Jira project key"
        types_raw = item.get("issueTypes")
        if not isinstance(types_raw, list) or not types_raw:
            return None, "issueTypes must list at least one ticket type"

        filters: Dict[str, Any] = {}
        for field in FILTER_FIELDS:
            names, error = _clean_name_list(item.get(field), field)
            if error or names is None:
                return None, error
            filters[field] = names
        jql, error = _clean_jql(item.get("jql"))
        if error or jql is None:
            return None, error
        filters["jql"] = jql

        slot = (project_key,) + _filter_signature(filters)
        # Same project and same extra clauses combine their ticket types.
        existing_types = merged[index_by_slot[slot]]["issueTypes"] if slot in index_by_slot else None
        issue_types, error = _merge_issue_types(existing_types, types_raw)
        if error or issue_types is None:
            return None, error
        rule = {"projectKey": project_key, "issueTypes": issue_types, **filters}
        if slot in index_by_slot:
            merged[index_by_slot[slot]] = rule
        else:
            index_by_slot[slot] = len(merged)
            merged.append(rule)
    return merged, None


def _folded(value: Any) -> str:
    return str(value or "").strip().casefold()


def _field_names(value: Any) -> List[str]:
    """Names from a Jira field that is either a string list or a list of objects."""
    if not isinstance(value, list):
        return []
    names: List[str] = []
    for item in value:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = str(item.get("name") or "").strip()
        else:
            name = ""
        if name:
            names.append(name)
    return names


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
        "labels": _field_names(fields.get("labels")),
        "components": _field_names(fields.get("components")),
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


def rca_rule_matches(scope: Any, rules: Any) -> bool:
    """True when a saved rule's structured fields match this ticket."""
    if not isinstance(scope, dict) or not isinstance(rules, list):
        return False
    return any(
        isinstance(rule, dict) and rule_matches_scope(scope, rule)
        for rule in rules
    )


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
# The filing prompt asks the agent to start the Jira comment with this.
AURORA_COMMENT_MARKER = "Aurora RCA"


def is_comment_event(payload: Dict[str, Any]) -> bool:
    """True for comment create/edit webhooks, which tune filters and never start an RCA."""
    event = str(payload.get("webhookEvent") or "")
    event_name = str(payload.get("issue_event_type_name") or "")
    return event in _COMMENT_WEBHOOK_EVENTS or event_name in _COMMENT_ISSUE_EVENTS


def comment_author_ids(payload: Dict[str, Any]) -> set:
    """Ids Jira puts on a comment author. Cloud uses accountId; Data Center uses key or name."""
    comment = payload.get("comment") if isinstance(payload, dict) else None
    author = comment.get("author") if isinstance(comment, dict) else None
    if not isinstance(author, dict):
        return set()
    ids = set()
    for key in ("accountId", "key", "name"):
        value = str(author.get(key) or "").strip()
        if value:
            ids.add(value)
    return ids


def project_has_rule(scope: Dict[str, Any], rules: Any) -> bool:
    """True when some saved filter already covers this project."""
    if not isinstance(scope, dict) or not isinstance(rules, list):
        return False
    project = _folded(scope.get("projectKey"))
    if not project:
        return False
    return any(
        isinstance(rule, dict) and _folded(rule.get("projectKey")) == project
        for rule in rules
    )


def extract_comment_text(payload: Dict[str, Any]) -> str:
    """Plain text of a comment event. Issue create/update returns empty."""
    # An issue payload is an investigation candidate, not an instruction.
    if not is_comment_event(payload):
        return ""
    comment = payload.get("comment")
    if not isinstance(comment, dict):
        return ""
    body = comment.get("body")
    if isinstance(body, str):
        return body.strip()
    if isinstance(body, dict):
        from connectors.jira_connector.adf_converter import adf_to_plain_text
        return adf_to_plain_text(body).strip()
    return ""


def _comment_is_usable(text: str) -> bool:
    # "ok" and emoji are not a filter instruction, so don't spend an agent call.
    return sum(char.isalnum() for char in text) >= 3


def decide_webhook(
    payload: Dict[str, Any],
    mode: str,
    rules: Any,
    *,
    connected_account_id: str = "",
) -> Dict[str, Any]:
    """What a webhook should do: update filters, start RCA, check JQL, or ignore."""
    issue = payload.get("issue") if isinstance(payload, dict) else None
    if not isinstance(issue, dict) or not issue:
        return {"action": "ignore", "reason": "no issue"}

    # Comments edit the filters in auto mode. They never start an RCA.
    if is_comment_event(payload):
        text = extract_comment_text(payload)
        scope = extract_webhook_scope(issue)
        # Aurora's own writeup, a short reply, or a project with no filter is not an instruction.
        own_writeup = AURORA_COMMENT_MARKER in text
        own_account = bool(connected_account_id) and connected_account_id in comment_author_ids(payload)
        if (
            mode == "auto"
            and _comment_is_usable(text)
            and not own_writeup
            and not own_account
            and project_has_rule(scope, rules)
        ):
            return {"action": "filters", "scope": scope}
        return {"action": "ignore", "reason": "comment"}

    scope = extract_webhook_scope(issue)
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
