"""Agent tools for interacting with Jira.

Split into READ and WRITE tiers. The READ tier (search, get_issue, add_comment)
is always registered when the user has a Jira connection. The WRITE tier
(create_issue, update_issue, link_issues) is only registered when the user's
``agent_tier`` setting is ``"write"``.
"""

import json
import logging
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from connectors.jira_connector.client import JiraClient
from connectors.jira_connector.adf_converter import text_to_adf, markdown_to_adf
from utils.auth.token_management import get_token_data

logger = logging.getLogger(__name__)


def _get_client(user_id: str) -> JiraClient:
    creds = get_token_data(user_id, "jira")
    if not creds:
        raise ValueError("Jira is not connected. Please connect Jira first.")

    auth_type = (creds.get("auth_type") or "oauth").lower()
    base_url = creds.get("base_url", "")
    cloud_id = creds.get("cloud_id") if auth_type == "oauth" else None
    token = creds.get("pat_token") if auth_type == "pat" else creds.get("access_token")
    if not token:
        raise ValueError("Jira credentials are incomplete.")

    return JiraClient(base_url, token, auth_type=auth_type, cloud_id=cloud_id)


# ---------------------------------------------------------------------------
# Pydantic arg schemas
# ---------------------------------------------------------------------------

class JiraSearchIssuesArgs(BaseModel):
    jql: str = Field(description="JQL query string (e.g. 'project = OPS AND status = Open')")
    max_results: int = Field(default=10, description="Maximum results to return (max 50)")


class JiraGetIssueArgs(BaseModel):
    issue_key: str = Field(description="Jira issue key (e.g. 'OPS-123')")


class JiraAddCommentArgs(BaseModel):
    issue_key: str = Field(description="Jira issue key to comment on")
    comment: str = Field(description="Comment text (markdown supported)")


class JiraCreateIssueArgs(BaseModel):
    project_key: str = Field(description="Jira project key (e.g. 'OPS')")
    summary: str = Field(description="Issue summary/title")
    description: str = Field(default="", description="Issue description (markdown)")
    issue_type: str = Field(default="Task", description="Issue type (Task, Bug, Story, etc.)")
    labels: Optional[List[str]] = Field(default=None, description="Labels to apply")


class JiraUpdateIssueArgs(BaseModel):
    issue_key: str = Field(description="Jira issue key to update")
    fields: Dict = Field(description="Fields to update (e.g. {'summary': 'New title', 'labels': ['urgent']})")


class JiraLinkIssuesArgs(BaseModel):
    inward_key: str = Field(description="Inward issue key")
    outward_key: str = Field(description="Outward issue key")
    link_type: str = Field(default="Relates", description="Link type (Relates, Blocks, Clones, etc.)")


# ---------------------------------------------------------------------------
# READ tier tools
# ---------------------------------------------------------------------------

def jira_search_issues(
    jql: str,
    max_results: int = 10,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Search Jira issues using JQL."""
    _ = session_id
    if not user_id:
        raise ValueError("user_id is required for Jira search")

    try:
        client = _get_client(user_id)
        result = client.search_issues(jql, max_results=min(max_results, 50))
    except ValueError:
        raise
    except Exception as exc:
        logger.exception("Jira search failed for user %s: %s", user_id, exc)
        raise ValueError("Failed to search Jira; check connection and permissions") from exc

    issues = result.get("issues", [])
    simplified = []
    for issue in issues:
        fields = issue.get("fields", {})
        simplified.append({
            "key": issue.get("key"),
            "summary": fields.get("summary"),
            "status": (fields.get("status") or {}).get("name"),
            "assignee": (fields.get("assignee") or {}).get("displayName"),
            "priority": (fields.get("priority") or {}).get("name"),
            "created": fields.get("created"),
            "updated": fields.get("updated"),
            "labels": fields.get("labels", []),
        })

    return json.dumps(
        {"status": "success", "total": result.get("total", 0), "count": len(simplified), "issues": simplified},
        ensure_ascii=False,
    )


def jira_get_issue(
    issue_key: str,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Get details of a specific Jira issue."""
    _ = session_id
    if not user_id:
        raise ValueError("user_id is required for Jira issue fetch")

    try:
        client = _get_client(user_id)
        issue = client.get_issue(issue_key)
    except ValueError:
        raise
    except Exception as exc:
        logger.exception("Jira get issue failed for user %s: %s", user_id, exc)
        raise ValueError("Failed to get Jira issue; check connection and permissions") from exc

    fields = issue.get("fields", {})
    desc_body = fields.get("description")
    description_text = ""
    if isinstance(desc_body, dict):
        for block in desc_body.get("content", []):
            for inline in block.get("content", []):
                if inline.get("type") == "text":
                    description_text += inline.get("text", "")
            description_text += "\n"
    elif isinstance(desc_body, str):
        description_text = desc_body

    result = {
        "key": issue.get("key"),
        "summary": fields.get("summary"),
        "description": description_text.strip(),
        "status": (fields.get("status") or {}).get("name"),
        "assignee": (fields.get("assignee") or {}).get("displayName"),
        "reporter": (fields.get("reporter") or {}).get("displayName"),
        "priority": (fields.get("priority") or {}).get("name"),
        "labels": fields.get("labels", []),
        "created": fields.get("created"),
        "updated": fields.get("updated"),
        "issueType": (fields.get("issuetype") or {}).get("name"),
        "project": (fields.get("project") or {}).get("key"),
    }

    comments = []
    comment_field = fields.get("comment", {})
    for c in (comment_field.get("comments") or [])[-5:]:
        comments.append({
            "author": (c.get("author") or {}).get("displayName"),
            "created": c.get("created"),
            "body": str(c.get("body", ""))[:500],
        })
    result["recentComments"] = comments

    return json.dumps({"status": "success", "issue": result}, ensure_ascii=False)


def jira_add_comment(
    issue_key: str,
    comment: str,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Add a comment to a Jira issue."""
    _ = session_id
    if not user_id:
        raise ValueError("user_id is required for Jira comment")

    try:
        client = _get_client(user_id)
        body_adf = text_to_adf(comment)
        result = client.add_comment(issue_key, body_adf)
    except ValueError:
        raise
    except Exception as exc:
        logger.exception("Jira add comment failed for user %s: %s", user_id, exc)
        raise ValueError("Failed to add Jira comment; check connection and permissions") from exc

    return json.dumps(
        {"status": "success", "commentId": result.get("id"), "issueKey": issue_key},
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# WRITE tier tools
# ---------------------------------------------------------------------------

def jira_create_issue(
    project_key: str,
    summary: str,
    description: str = "",
    issue_type: str = "Task",
    labels: Optional[List[str]] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Create a new Jira issue."""
    _ = session_id
    if not user_id:
        raise ValueError("user_id is required for Jira issue creation")

    try:
        client = _get_client(user_id)
        desc_adf = markdown_to_adf(description) if description else None
        result = client.create_issue(
            project_key=project_key,
            summary=summary,
            issue_type=issue_type,
            description_adf=desc_adf,
            labels=labels,
        )
    except ValueError:
        raise
    except Exception as exc:
        logger.exception("Jira create issue failed for user %s: %s", user_id, exc)
        raise ValueError("Failed to create Jira issue; check connection and permissions") from exc

    return json.dumps(
        {"status": "success", "key": result.get("key"), "id": result.get("id")},
        ensure_ascii=False,
    )


def jira_update_issue(
    issue_key: str,
    fields: Dict = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Update fields on a Jira issue."""
    _ = session_id
    if not user_id:
        raise ValueError("user_id is required for Jira issue update")
    if not fields:
        raise ValueError("fields dict is required")

    try:
        client = _get_client(user_id)
        client.update_issue(issue_key, fields=fields)
    except ValueError:
        raise
    except Exception as exc:
        logger.exception("Jira update issue failed for user %s: %s", user_id, exc)
        raise ValueError("Failed to update Jira issue; check connection and permissions") from exc

    return json.dumps({"status": "success", "issueKey": issue_key}, ensure_ascii=False)


def jira_link_issues(
    inward_key: str,
    outward_key: str,
    link_type: str = "Relates",
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Create a link between two Jira issues."""
    _ = session_id
    if not user_id:
        raise ValueError("user_id is required for Jira issue linking")

    try:
        client = _get_client(user_id)
        client.link_issues(inward_key, outward_key, link_type)
    except ValueError:
        raise
    except Exception as exc:
        logger.exception("Jira link issues failed for user %s: %s", user_id, exc)
        raise ValueError("Failed to link Jira issues; check connection and permissions") from exc

    return json.dumps(
        {"status": "success", "inward": inward_key, "outward": outward_key, "type": link_type},
        ensure_ascii=False,
    )
