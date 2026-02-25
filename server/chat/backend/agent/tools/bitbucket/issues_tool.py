"""Bitbucket issue operations tool."""

import json
import logging
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .utils import (
    get_bb_client_for_user,
    resolve_workspace_repo,
    require_repo,
    forward_if_error,
    build_error_response,
    build_success_response,
)

logger = logging.getLogger(__name__)


class BitbucketIssuesArgs(BaseModel):
    action: Literal[
        "list_issues",
        "get_issue",
        "create_issue",
        "update_issue",
        "list_issue_comments",
        "add_issue_comment",
    ] = Field(description="The operation to perform.")
    workspace: Optional[str] = Field(None, description="Workspace slug. Auto-resolves from saved selection if omitted.")
    repo_slug: Optional[str] = Field(None, description="Repository slug. Auto-resolves from saved selection if omitted.")
    issue_id: Optional[int] = Field(None, description="Issue ID (required for single-issue operations).")
    title: Optional[str] = Field(None, description="Issue title (for create_issue, update_issue).")
    content: Optional[str] = Field(None, description="Issue body or comment content.")
    kind: Optional[str] = Field(None, description="Issue kind: bug, enhancement, proposal, task (for create_issue).")
    priority: Optional[str] = Field(None, description="Issue priority: trivial, minor, major, critical, blocker (for create_issue).")
    status: Optional[str] = Field(None, description="Issue status: new, open, resolved, on hold, invalid, duplicate, wontfix, closed (for update_issue).")


def _require_issue(ws, repo, issue_id) -> Optional[str]:
    """Validate workspace, repo, and issue_id are present."""
    err = require_repo(ws, repo)
    if err:
        return err
    if not issue_id:
        return "issue_id is required"
    return None


def bitbucket_issues(
    action: str,
    workspace: Optional[str] = None,
    repo_slug: Optional[str] = None,
    issue_id: Optional[int] = None,
    title: Optional[str] = None,
    content: Optional[str] = None,
    kind: Optional[str] = None,
    priority: Optional[str] = None,
    status: Optional[str] = None,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    if not user_id:
        return build_error_response("User context not available")

    client = get_bb_client_for_user(user_id)
    if not client:
        return build_error_response("Bitbucket not connected. Please connect Bitbucket first.")

    ws, repo, saved_branch, source = resolve_workspace_repo(user_id, workspace, repo_slug)

    try:
        if action == "list_issues":
            if err := require_repo(ws, repo):
                return build_error_response(err)
            result = client.get_issues(ws, repo)
            if isinstance(result, list):
                issues = [{
                    "id": i.get("id"),
                    "title": i.get("title"),
                    "state": i.get("state"),
                    "kind": i.get("kind"),
                    "priority": i.get("priority"),
                    "assignee": i.get("assignee", {}).get("display_name", "") if i.get("assignee") else "",
                    "reporter": i.get("reporter", {}).get("display_name", "") if i.get("reporter") else "",
                    "created_on": i.get("created_on"),
                    "updated_on": i.get("updated_on"),
                } for i in result]
                return build_success_response(issues=issues, count=len(issues))
            return json.dumps(result, default=str)

        if action == "get_issue":
            if err := _require_issue(ws, repo, issue_id):
                return build_error_response(err)
            return json.dumps(client.get_issue(ws, repo, issue_id), default=str)

        if action == "create_issue":
            if err := require_repo(ws, repo):
                return build_error_response(err)
            if not title:
                return build_error_response("title is required")
            result = client.create_issue(
                ws, repo, title,
                content=content or "",
                kind=kind or "bug",
                priority=priority or "major",
            )
            if err := forward_if_error(result):
                return err
            return build_success_response(
                message=f"Issue #{result.get('id')} created: {title}",
                issue_id=result.get("id"),
                url=result.get("links", {}).get("html", {}).get("href", ""),
            )

        if action == "update_issue":
            if err := _require_issue(ws, repo, issue_id):
                return build_error_response(err)
            fields = {}
            if title:
                fields["title"] = title
            if content is not None:
                fields["content"] = {"raw": content}
            if kind:
                fields["kind"] = kind
            if priority:
                fields["priority"] = priority
            if status:
                fields["state"] = status
            if not fields:
                return build_error_response("At least one field is required to update")
            result = client.update_issue(ws, repo, issue_id, **fields)
            if err := forward_if_error(result):
                return err
            return build_success_response(message=f"Issue #{issue_id} updated")

        if action == "list_issue_comments":
            if err := _require_issue(ws, repo, issue_id):
                return build_error_response(err)
            result = client.list_issue_comments(ws, repo, issue_id)
            if isinstance(result, list):
                comments = [{
                    "id": c.get("id"),
                    "content": c.get("content", {}).get("raw", ""),
                    "author": c.get("user", {}).get("display_name", ""),
                    "created_on": c.get("created_on"),
                } for c in result]
                return build_success_response(comments=comments, count=len(comments))
            return json.dumps(result, default=str)

        if action == "add_issue_comment":
            if err := _require_issue(ws, repo, issue_id):
                return build_error_response(err)
            if not content:
                return build_error_response("content is required")
            result = client.add_issue_comment(ws, repo, issue_id, content)
            if err := forward_if_error(result):
                return err
            return build_success_response(message=f"Comment added to issue #{issue_id}")

        return build_error_response(f"Unknown action: {action}")

    except Exception as e:
        logger.error(f"Bitbucket issues tool error: {e}", exc_info=True)
        return build_error_response(f"Bitbucket API error: {str(e)}")
