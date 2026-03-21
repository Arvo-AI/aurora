"""Structured JQL query builders for Jira searches."""

from __future__ import annotations

from typing import List, Optional


def _escape_jql_value(value: str) -> str:
    """Escape special JQL characters in a value."""
    for ch in ('"', "'", "\\"):
        value = value.replace(ch, f"\\{ch}")
    return value


def build_incident_search_jql(
    service: Optional[str] = None,
    component: Optional[str] = None,
    labels: Optional[List[str]] = None,
    text: Optional[str] = None,
    project: Optional[str] = None,
    max_days_back: int = 90,
) -> str:
    """Build a JQL query to find incident-related issues.

    Restricts to structured fields (service/component/labels) to avoid noisy
    full-text queries.
    """
    clauses: List[str] = []

    if project:
        clauses.append(f'project = "{_escape_jql_value(project)}"')

    if service:
        clauses.append(
            f'(summary ~ "{_escape_jql_value(service)}" OR labels = "{_escape_jql_value(service)}")'
        )

    if component:
        clauses.append(f'component = "{_escape_jql_value(component)}"')

    if labels:
        label_clauses = " AND ".join(
            f'labels = "{_escape_jql_value(label)}"' for label in labels
        )
        clauses.append(f"({label_clauses})")

    if text:
        clauses.append(f'text ~ "{_escape_jql_value(text)}"')

    if max_days_back:
        max_days_back = max(max_days_back, 0)
        clauses.append(f"created >= -{max_days_back}d")

    query = " AND ".join(clauses)
    return f"{query} ORDER BY created DESC" if clauses else "ORDER BY created DESC"


def build_recent_issues_jql(
    project: str,
    days_back: int = 30,
    status: Optional[str] = None,
) -> str:
    """Build a JQL query for recent issues in a project."""
    clauses = [
        f'project = "{_escape_jql_value(project)}"',
        f"created >= -{max(days_back, 0)}d",
    ]
    if status:
        clauses.append(f'status = "{_escape_jql_value(status)}"')
    query = " AND ".join(clauses)
    return f"{query} ORDER BY created DESC"
