"""Which investigations may reach channels outside Aurora.

An investigation from a source in ``NO_OUTBOUND_SOURCES`` keeps every result
inside Aurora: background/task.py skips on_incident actions and Jira filing
(start/complete notifications are already off via ``send_notifications=False``),
and cloud_tools drops the tools below, which write to a third-party system.
"""

from typing import Any, Dict, List, Optional

NO_OUTBOUND_SOURCES = frozenset({"pipeline"})

OUTBOUND_WRITE_TOOLS = frozenset({
    "jira_add_comment",
    "jira_create_issue",
    "jira_update_issue",
    "jira_link_issues",
    "servicenow_add_work_note",
    "servicenow_create_incident",
    "servicenow_update_incident",
    "sharepoint_create_page",
    "post_slack_message",
    "post_teams_message",
    "send_hpa_vpa_recommendation",
    "github_commit",
    "cloudflare_action",
})


def allows_outbound(trigger_metadata: Optional[Dict[str, Any]]) -> bool:
    return (trigger_metadata or {}).get("source") not in NO_OUTBOUND_SOURCES


def source_allows_outbound(source: Optional[str]) -> bool:
    return (source or "") not in NO_OUTBOUND_SOURCES


def drop_outbound_tools(tools: List[Any], trigger_source: Optional[str]) -> List[Any]:
    """Remove third-party write tools when the investigation's source forbids them."""
    if source_allows_outbound(trigger_source):
        return tools
    return [t for t in tools if getattr(t, "name", "") not in OUTBOUND_WRITE_TOOLS]
