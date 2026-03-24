"""Agent tools for interacting with Jira."""

import json
import logging
import time
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from connectors.jira_connector.client import JiraClient
from connectors.jira_connector.adf_converter import markdown_to_adf
from utils.auth.token_management import get_token_data, store_tokens_in_db

logger = logging.getLogger(__name__)


def _refresh_oauth_token(user_id: str, creds: dict) -> Optional[dict]:
    """Attempt to refresh an expired OAuth access token and persist the result."""
    refresh_token = creds.get("refresh_token")
    if not refresh_token:
        return None
    try:
        from connectors.atlassian_auth.auth import refresh_access_token
        token_data = refresh_access_token(refresh_token)
    except Exception as exc:
        logger.warning("[JIRA-TOOL] OAuth refresh failed for user %s: %s", user_id, exc)
        return None

    access_token = token_data.get("access_token")
    if not access_token:
        return None

    updated = dict(creds)
    updated["access_token"] = access_token
    new_refresh = token_data.get("refresh_token")
    if new_refresh:
        updated["refresh_token"] = new_refresh
    expires_in = token_data.get("expires_in")
    if expires_in:
        updated["expires_in"] = expires_in
        updated["expires_at"] = int(time.time()) + int(expires_in)

    store_tokens_in_db(user_id, updated, "jira")
    logger.info("[JIRA-TOOL] Refreshed OAuth token for user %s", user_id)
    return updated


def _get_client(user_id: str) -> JiraClient:
    creds = get_token_data(user_id, "jira")
    if not creds:
        raise ValueError("Jira is not connected. Please connect Jira first.")

    auth_type = (creds.get("auth_type") or "oauth").lower()

    if auth_type == "oauth":
        expires_at = creds.get("expires_at")
        if expires_at and int(time.time()) >= int(expires_at) - 60:
            refreshed = _refresh_oauth_token(user_id, creds)
            if refreshed:
                creds = refreshed

    base_url = creds.get("base_url", "")
    cloud_id = creds.get("cloud_id") if auth_type == "oauth" else None
    token = creds.get("pat_token") if auth_type == "pat" else creds.get("access_token")
    if not token:
        raise ValueError("Jira credentials are incomplete.")

    client = JiraClient(base_url, token, auth_type=auth_type, cloud_id=cloud_id)

    if auth_type == "oauth":
        try:
            client.get_myself()
        except Exception:
            logger.info("[JIRA-TOOL] Token validation failed, attempting refresh for user %s", user_id)
            refreshed = _refresh_oauth_token(user_id, creds)
            if not refreshed:
                raise ValueError("Jira OAuth token expired and refresh failed. Please reconnect Jira.")
            token = refreshed.get("access_token")
            client = JiraClient(base_url, token, auth_type=auth_type, cloud_id=cloud_id)

    return client


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
# Search & read tools
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
    except Exception as exc:
        logger.exception("Jira search failed for user %s: %s", user_id, exc)
        return json.dumps(
            {"status": "error", "error": f"Jira search failed: {exc}. "
             "Continue the investigation using other tools."},
            ensure_ascii=False,
        )

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
    except Exception as exc:
        logger.exception("Jira get issue failed for user %s: %s", user_id, exc)
        return json.dumps(
            {"status": "error", "error": f"Jira get issue failed: {exc}. "
             "Continue the investigation using other tools."},
            ensure_ascii=False,
        )

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
        body_adf = markdown_to_adf(comment)
        result = client.add_comment(issue_key, body_adf)
    except ValueError:
        raise
    except Exception as exc:
        logger.exception("Jira add comment failed for user %s: %s", user_id, exc)
        return json.dumps(
            {"status": "error", "error": f"Failed to add Jira comment: {exc}. Continue using other tools."},
            ensure_ascii=False,
        )

    comment_id = result.get("id")
    browse_url = f"{client.base_url}/browse/{issue_key}"
    if comment_id:
        browse_url += f"?focusedId={comment_id}&page=com.atlassian.jira.plugin.system.issuetabpanels:comment-tabpanel#comment-{comment_id}"

    return json.dumps(
        {"status": "success", "commentId": comment_id, "issueKey": issue_key, "url": browse_url},
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# Create & update tools
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
        detail = ""
        if hasattr(exc, "__cause__") and hasattr(exc.__cause__, "response"):
            try:
                detail = exc.__cause__.response.text[:500]
            except Exception:
                pass  # secondary extraction failure — already logged above
        elif hasattr(exc, "response"):
            try:
                detail = exc.response.text[:500]
            except Exception:
                pass  # secondary extraction failure — already logged above
        return json.dumps(
            {"status": "error",
             "error": f"Failed to create Jira issue: {exc}. {detail}".strip(),
             "hint": "Check project_key exists, issue_type is valid (try 'Bug', 'Task', or 'Story'), and required fields are present. "
                     "Use jira_add_comment on an existing issue as a fallback."},
            ensure_ascii=False,
        )

    issue_key = result.get("key")
    browse_url = f"{client.base_url}/browse/{issue_key}" if issue_key else None

    return json.dumps(
        {"status": "success", "key": issue_key, "id": result.get("id"), "url": browse_url},
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
        return json.dumps(
            {"status": "error", "error": f"Failed to update Jira issue: {exc}. Continue using other tools."},
            ensure_ascii=False,
        )

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
        return json.dumps(
            {"status": "error", "error": f"Failed to link Jira issues: {exc}. Continue using other tools."},
            ensure_ascii=False,
        )

    return json.dumps(
        {"status": "success", "inward": inward_key, "outward": outward_key, "type": link_type},
        ensure_ascii=False,
    )
