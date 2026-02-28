"""Bitbucket repository, file, and code operations tool."""

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
    confirm_or_cancel,
)

logger = logging.getLogger(__name__)


class BitbucketReposArgs(BaseModel):
    action: Literal[
        "list_repos",
        "get_repo",
        "get_file_contents",
        "create_or_update_file",
        "delete_file",
        "get_directory_tree",
        "search_code",
        "list_workspaces",
        "get_workspace",
    ] = Field(description="The operation to perform.")
    workspace: Optional[str] = Field(None, description="Workspace slug. Auto-resolves from saved selection if omitted.")
    repo_slug: Optional[str] = Field(None, description="Repository slug. Auto-resolves from saved selection if omitted.")
    path: Optional[str] = Field(None, description="File or directory path (for file/directory operations).")
    content: Optional[str] = Field(None, description="File content (for create_or_update_file).")
    message: Optional[str] = Field(None, description="Commit message (for create_or_update_file, delete_file).")
    branch: Optional[str] = Field(None, description="Branch name (for file operations). Defaults to saved branch.")
    commit: Optional[str] = Field(None, description="Commit hash or branch ref (for get_file_contents, get_directory_tree). Defaults to HEAD.")
    query: Optional[str] = Field(None, description="Search query (for search_code).")


def bitbucket_repos(
    action: str,
    workspace: Optional[str] = None,
    repo_slug: Optional[str] = None,
    path: Optional[str] = None,
    content: Optional[str] = None,
    message: Optional[str] = None,
    branch: Optional[str] = None,
    commit: Optional[str] = None,
    query: Optional[str] = None,
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
        if action == "list_workspaces":
            result = client.get_workspaces()
            if isinstance(result, list):
                workspaces = [{"slug": w.get("slug"), "name": w.get("name"), "uuid": w.get("uuid")} for w in result]
                return build_success_response(workspaces=workspaces, count=len(workspaces))
            return json.dumps(result, default=str)

        if action == "get_workspace":
            if not ws:
                return build_error_response("workspace is required")
            return json.dumps(client.get_workspace(ws), default=str)

        if action == "list_repos":
            if not ws:
                return build_error_response("workspace is required")
            result = client.get_repositories(ws)
            if isinstance(result, list):
                repos = [{
                    "slug": r.get("slug"),
                    "name": r.get("name"),
                    "full_name": r.get("full_name"),
                    "is_private": r.get("is_private"),
                    "description": r.get("description", ""),
                    "mainbranch": r.get("mainbranch", {}).get("name") if r.get("mainbranch") else None,
                    "updated_on": r.get("updated_on"),
                } for r in result]
                return build_success_response(repositories=repos, count=len(repos), workspace=ws)
            return json.dumps(result, default=str)

        if action == "get_repo":
            if err := require_repo(ws, repo):
                return build_error_response(err)
            return json.dumps(client.get_repository(ws, repo), default=str)

        if action == "get_file_contents":
            if err := require_repo(ws, repo):
                return build_error_response(err)
            if not path:
                return build_error_response("path is required")
            ref = commit or branch or "HEAD"
            return json.dumps(client.get_file_contents(ws, repo, path, commit=ref), default=str)

        if action == "create_or_update_file":
            if err := require_repo(ws, repo):
                return build_error_response(err)
            if not path:
                return build_error_response("path is required")
            if content is None:
                return build_error_response("content is required")
            if not message:
                return build_error_response("message (commit message) is required")
            if not branch:
                return build_error_response("branch is required")
            if cancelled := confirm_or_cancel(user_id,
                    f"Commit file '{path}' to branch '{branch}' in {ws}/{repo}",
                    "bitbucket_repos"):
                return cancelled
            result = client.create_or_update_file(ws, repo, path, content, message, branch)
            if err := forward_if_error(result):
                return err
            return build_success_response(message=f"File '{path}' committed to {branch}", result=result)

        if action == "delete_file":
            if err := require_repo(ws, repo):
                return build_error_response(err)
            if not path:
                return build_error_response("path is required")
            if not message:
                return build_error_response("message (commit message) is required")
            if not branch:
                return build_error_response("branch is required")
            if cancelled := confirm_or_cancel(user_id,
                    f"Delete file '{path}' from branch '{branch}' in {ws}/{repo}",
                    "bitbucket_repos"):
                return cancelled
            result = client.delete_file(ws, repo, path, message, branch)
            if err := forward_if_error(result):
                return err
            return build_success_response(message=f"File '{path}' deleted from {branch}")

        if action == "get_directory_tree":
            if err := require_repo(ws, repo):
                return build_error_response(err)
            ref = commit or branch or "HEAD"
            return json.dumps(client.get_directory_tree(ws, repo, path or "", commit=ref), default=str)

        if action == "search_code":
            if not ws:
                return build_error_response("workspace is required")
            if not query:
                return build_error_response("query is required")
            return json.dumps(client.search_code(ws, query), default=str)

        return build_error_response(f"Unknown action: {action}")

    except Exception as e:
        logger.error(f"Bitbucket repos tool error: {e}", exc_info=True)
        return build_error_response(f"Bitbucket API error: {str(e)}")
