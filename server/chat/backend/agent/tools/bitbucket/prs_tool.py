"""Bitbucket pull request operations tool."""

import json
import logging
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .utils import (
    DIFF_TRUNCATE_LIMIT,
    get_bb_client_for_user,
    resolve_workspace_repo,
    require_repo,
    forward_if_error,
    truncate_text,
    build_error_response,
    build_success_response,
    confirm_or_cancel,
)

logger = logging.getLogger(__name__)


class BitbucketPullRequestsArgs(BaseModel):
    action: Literal[
        "list_prs",
        "get_pr",
        "create_pr",
        "update_pr",
        "merge_pr",
        "approve_pr",
        "unapprove_pr",
        "decline_pr",
        "list_pr_comments",
        "add_pr_comment",
        "get_pr_diff",
        "get_pr_activity",
    ] = Field(description="The operation to perform.")
    workspace: Optional[str] = Field(None, description="Workspace slug. Auto-resolves from saved selection if omitted.")
    repo_slug: Optional[str] = Field(None, description="Repository slug. Auto-resolves from saved selection if omitted.")
    pr_id: Optional[int] = Field(None, description="Pull request ID (required for single-PR operations).")
    title: Optional[str] = Field(None, description="PR title (for create_pr, update_pr).")
    source_branch: Optional[str] = Field(None, description="Source branch (for create_pr).")
    dest_branch: Optional[str] = Field(None, description="Destination branch (for create_pr).")
    description: Optional[str] = Field(None, description="PR description (for create_pr, update_pr).")
    merge_strategy: Optional[str] = Field(None, description="Merge strategy: merge_commit, squash, or fast_forward (for merge_pr).")
    close_source: Optional[bool] = Field(None, description="Close source branch after merge (for merge_pr, create_pr).")
    reviewers: Optional[list[str]] = Field(None, description="List of reviewer UUIDs (for create_pr).")
    content: Optional[str] = Field(None, description="Comment content (for add_pr_comment).")
    state: Optional[str] = Field(None, description="PR state filter: OPEN, MERGED, DECLINED (for list_prs).")


def _require_pr(ws, repo, pr_id) -> Optional[str]:
    """Validate workspace, repo, and pr_id are present."""
    err = require_repo(ws, repo)
    if err:
        return err
    if not pr_id:
        return "pr_id is required"
    return None


def bitbucket_pull_requests(
    action: str,
    workspace: Optional[str] = None,
    repo_slug: Optional[str] = None,
    pr_id: Optional[int] = None,
    title: Optional[str] = None,
    source_branch: Optional[str] = None,
    dest_branch: Optional[str] = None,
    description: Optional[str] = None,
    merge_strategy: Optional[str] = None,
    close_source: Optional[bool] = None,
    reviewers: Optional[list[str]] = None,
    content: Optional[str] = None,
    state: Optional[str] = None,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    if not user_id:
        return build_error_response("User context not available")

    client = get_bb_client_for_user(user_id)
    if not client:
        return build_error_response("Bitbucket not connected. Please connect Bitbucket first.")

    ws, repo, saved_branch, source_desc = resolve_workspace_repo(user_id, workspace, repo_slug)

    try:
        if action == "list_prs":
            if err := require_repo(ws, repo):
                return build_error_response(err)
            result = client.get_pull_requests(ws, repo, state=state)
            if isinstance(result, list):
                prs = [{
                    "id": pr.get("id"),
                    "title": pr.get("title"),
                    "state": pr.get("state"),
                    "author": pr.get("author", {}).get("display_name", ""),
                    "source_branch": pr.get("source", {}).get("branch", {}).get("name", ""),
                    "dest_branch": pr.get("destination", {}).get("branch", {}).get("name", ""),
                    "created_on": pr.get("created_on"),
                    "updated_on": pr.get("updated_on"),
                    "comment_count": pr.get("comment_count", 0),
                } for pr in result]
                return build_success_response(pull_requests=prs, count=len(prs),
                                              state=state or "all")
            return json.dumps(result, default=str)

        if action == "get_pr":
            if err := _require_pr(ws, repo, pr_id):
                return build_error_response(err)
            return json.dumps(client.get_pull_request(ws, repo, pr_id), default=str)

        if action == "create_pr":
            if err := require_repo(ws, repo):
                return build_error_response(err)
            if not title:
                return build_error_response("title is required")
            if not source_branch:
                return build_error_response("source_branch is required")
            if not dest_branch:
                dest_branch = saved_branch or "main"
            result = client.create_pull_request(
                ws, repo, title, source_branch, dest_branch,
                description=description or "",
                close_source=close_source or False,
                reviewers=reviewers,
            )
            if err := forward_if_error(result):
                return err
            return build_success_response(
                message=f"PR #{result.get('id')} created: {title}",
                pr_id=result.get("id"),
                url=result.get("links", {}).get("html", {}).get("href", ""),
            )

        if action == "update_pr":
            if err := _require_pr(ws, repo, pr_id):
                return build_error_response(err)
            fields = {}
            if title:
                fields["title"] = title
            if description is not None:
                fields["description"] = description
            if not fields:
                return build_error_response("At least one field (title, description) is required to update")
            result = client.update_pull_request(ws, repo, pr_id, **fields)
            if err := forward_if_error(result):
                return err
            return build_success_response(message=f"PR #{pr_id} updated")

        if action == "merge_pr":
            if err := _require_pr(ws, repo, pr_id):
                return build_error_response(err)
            strategy = merge_strategy or "merge_commit"
            if cancelled := confirm_or_cancel(user_id,
                    f"Merge PR #{pr_id} in {ws}/{repo} (strategy: {strategy})",
                    "bitbucket_pull_requests"):
                return cancelled
            result = client.merge_pull_request(
                ws, repo, pr_id,
                merge_strategy=strategy,
                close_source=close_source if close_source is not None else False,
            )
            if err := forward_if_error(result):
                return err
            return build_success_response(message=f"PR #{pr_id} merged")

        if action == "approve_pr":
            if err := _require_pr(ws, repo, pr_id):
                return build_error_response(err)
            result = client.approve_pull_request(ws, repo, pr_id)
            if err := forward_if_error(result):
                return err
            return build_success_response(message=f"PR #{pr_id} approved")

        if action == "unapprove_pr":
            if err := _require_pr(ws, repo, pr_id):
                return build_error_response(err)
            result = client.unapprove_pull_request(ws, repo, pr_id)
            if err := forward_if_error(result):
                return err
            return build_success_response(message=f"PR #{pr_id} approval removed")

        if action == "decline_pr":
            if err := _require_pr(ws, repo, pr_id):
                return build_error_response(err)
            if cancelled := confirm_or_cancel(user_id,
                    f"Decline PR #{pr_id} in {ws}/{repo}",
                    "bitbucket_pull_requests"):
                return cancelled
            result = client.decline_pull_request(ws, repo, pr_id)
            if err := forward_if_error(result):
                return err
            return build_success_response(message=f"PR #{pr_id} declined")

        if action == "list_pr_comments":
            if err := _require_pr(ws, repo, pr_id):
                return build_error_response(err)
            result = client.list_pr_comments(ws, repo, pr_id)
            if isinstance(result, list):
                comments = [{
                    "id": c.get("id"),
                    "content": c.get("content", {}).get("raw", ""),
                    "author": c.get("user", {}).get("display_name", ""),
                    "created_on": c.get("created_on"),
                } for c in result]
                return build_success_response(comments=comments, count=len(comments))
            return json.dumps(result, default=str)

        if action == "add_pr_comment":
            if err := _require_pr(ws, repo, pr_id):
                return build_error_response(err)
            if not content:
                return build_error_response("content is required")
            result = client.add_pr_comment(ws, repo, pr_id, content)
            if err := forward_if_error(result):
                return err
            return build_success_response(message=f"Comment added to PR #{pr_id}")

        if action == "get_pr_diff":
            if err := _require_pr(ws, repo, pr_id):
                return build_error_response(err)
            result = client.get_pr_diff(ws, repo, pr_id)
            if err := forward_if_error(result):
                return err
            if isinstance(result, str):
                result = truncate_text(result, DIFF_TRUNCATE_LIMIT, label="diff")
            return build_success_response(diff=result, pr_id=pr_id)

        if action == "get_pr_activity":
            if err := _require_pr(ws, repo, pr_id):
                return build_error_response(err)
            return json.dumps(client.get_pr_activity(ws, repo, pr_id), default=str)

        return build_error_response(f"Unknown action: {action}")

    except Exception as e:
        logger.error(f"Bitbucket PRs tool error: {e}", exc_info=True)
        return build_error_response(f"Bitbucket API error: {str(e)}")
