"""Bitbucket branch and commit operations tool."""

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


class BitbucketBranchesArgs(BaseModel):
    action: Literal[
        "list_branches",
        "create_branch",
        "delete_branch",
        "list_commits",
        "get_commit",
        "get_diff",
        "compare",
    ] = Field(description="The operation to perform.")
    workspace: Optional[str] = Field(None, description="Workspace slug. Auto-resolves from saved selection if omitted.")
    repo_slug: Optional[str] = Field(None, description="Repository slug. Auto-resolves from saved selection if omitted.")
    branch: Optional[str] = Field(None, description="Branch name (for list_commits, create_branch target lookup).")
    name: Optional[str] = Field(None, description="New branch name (for create_branch).")
    target_hash: Optional[str] = Field(None, description="Target commit hash to branch from (for create_branch).")
    commit_hash: Optional[str] = Field(None, description="Commit hash (for get_commit).")
    spec: Optional[str] = Field(None, description="Diff spec: commit hash, branch, or base..head (for get_diff, compare).")


def bitbucket_branches(
    action: str,
    workspace: Optional[str] = None,
    repo_slug: Optional[str] = None,
    branch: Optional[str] = None,
    name: Optional[str] = None,
    target_hash: Optional[str] = None,
    commit_hash: Optional[str] = None,
    spec: Optional[str] = None,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    if not user_id:
        return build_error_response("User context not available")

    client = get_bb_client_for_user(user_id)
    if not client:
        return build_error_response("Bitbucket not connected. Please connect Bitbucket first.")

    ws, repo, saved_branch, source = resolve_workspace_repo(user_id, workspace, repo_slug)
    if not branch:
        branch = saved_branch

    try:
        if action == "list_branches":
            if err := require_repo(ws, repo):
                return build_error_response(err)
            result = client.get_branches(ws, repo)
            if isinstance(result, list):
                branches = [{
                    "name": b.get("name"),
                    "target_hash": b.get("target", {}).get("hash", "")[:12],
                    "target_date": b.get("target", {}).get("date"),
                    "target_message": b.get("target", {}).get("message", "").split("\n")[0],
                } for b in result]
                return build_success_response(branches=branches, count=len(branches))
            return json.dumps(result, default=str)

        if action == "create_branch":
            if err := require_repo(ws, repo):
                return build_error_response(err)
            if not name:
                return build_error_response("name (new branch name) is required")
            if not target_hash:
                return build_error_response("target_hash is required. Use list_branches or list_commits to find a hash.")
            result = client.create_branch(ws, repo, name, target_hash)
            if err := forward_if_error(result):
                return err
            return build_success_response(
                message=f"Branch '{name}' created from {target_hash[:12]}",
                branch=result.get("name"),
                target=result.get("target", {}).get("hash", "")[:12],
            )

        if action == "delete_branch":
            if err := require_repo(ws, repo):
                return build_error_response(err)
            if not name:
                return build_error_response("name (branch name) is required")
            if cancelled := confirm_or_cancel(user_id,
                    f"Delete branch '{name}' in {ws}/{repo}",
                    "bitbucket_branches"):
                return cancelled
            result = client.delete_branch(ws, repo, name)
            if err := forward_if_error(result):
                return err
            return build_success_response(message=f"Branch '{name}' deleted")

        if action == "list_commits":
            if err := require_repo(ws, repo):
                return build_error_response(err)
            result = client.list_commits(ws, repo, branch=branch)
            if isinstance(result, list):
                commits = [{
                    "hash": c.get("hash", "")[:12],
                    "message": c.get("message", "").split("\n")[0],
                    "author": c.get("author", {}).get("raw", ""),
                    "date": c.get("date"),
                } for c in result]
                return build_success_response(commits=commits, count=len(commits),
                                              branch=branch or "default")
            return json.dumps(result, default=str)

        if action == "get_commit":
            if err := require_repo(ws, repo):
                return build_error_response(err)
            if not commit_hash:
                return build_error_response("commit_hash is required")
            return json.dumps(client.get_commit(ws, repo, commit_hash), default=str)

        # get_diff and compare are equivalent -- both fetch a diff for a spec
        if action in ("get_diff", "compare"):
            if err := require_repo(ws, repo):
                return build_error_response(err)
            if not spec:
                return build_error_response("spec is required (commit hash, branch name, or base..head)")
            result = client.get_diff(ws, repo, spec)
            if err := forward_if_error(result):
                return err
            if isinstance(result, str):
                result = truncate_text(result, DIFF_TRUNCATE_LIMIT, label="diff")
            return build_success_response(diff=result, spec=spec)

        return build_error_response(f"Unknown action: {action}")

    except Exception as e:
        logger.error(f"Bitbucket branches tool error: {e}", exc_info=True)
        return build_error_response(f"Bitbucket API error: {str(e)}")
