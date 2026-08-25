"""Bitbucket repository, file, and code operations tool."""

import json
import logging
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .fix_tool import FixEdit
from .utils import (
    get_bb_client_for_user,
    get_default_branch,
    require_repo,
    forward_if_error,
    build_error_response,
    build_success_response,
    confirm_or_cancel,
    page_file_content,
    apply_edits_checked,
)

from utils.db.connection_pool import db_pool
from utils.auth.stateless_auth import set_rls_context

logger = logging.getLogger(__name__)


class BitbucketReposArgs(BaseModel):
    action: Literal[
        "list_repos",
        "get_repo",
        "get_file_contents",
        "create_or_update_file",
        "edit_file",
        "delete_file",
        "get_directory_tree",
        "search_code",
        "list_workspaces",
        "get_workspace",
    ] = Field(description="The operation to perform.")
    workspace: Optional[str] = Field(None, description="Workspace slug (required for repo-scoped actions).")
    repo_slug: Optional[str] = Field(None, description="Repository slug (required for repo-scoped actions).")
    path: Optional[str] = Field(None, description="File or directory path (for file/directory operations).")
    content: Optional[str] = Field(None, description="File content (for create_or_update_file).")
    message: Optional[str] = Field(None, description="Commit message (for create_or_update_file, edit_file, delete_file).")
    branch: Optional[str] = Field(None, description="Branch name (for file operations). Defaults to saved branch.")
    commit: Optional[str] = Field(None, description="Commit hash or branch ref (for get_file_contents, get_directory_tree). Defaults to HEAD.")
    query: Optional[str] = Field(None, description="Search query (for search_code).")
    start_line: Optional[int] = Field(
        None,
        description="1-indexed start line for get_file_contents paging. Defaults to 1. Pass the continue hint to read more.",
    )
    edits: Optional[list[FixEdit]] = Field(
        None,
        description="Anchored search-and-replace edits for edit_file (old_string/new_string/replace_all).",
    )


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
    start_line: Optional[int] = None,
    edits: Optional[list] = None,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    if not user_id:
        return build_error_response("User context not available")

    client = get_bb_client_for_user(user_id)
    if not client:
        return build_error_response("Bitbucket not connected. Please connect Bitbucket first.")

    ws, repo = workspace, repo_slug

    repo_scoped = action in (
        "get_repo", "get_file_contents", "create_or_update_file",
        "edit_file", "delete_file", "get_directory_tree",
    )
    if repo_scoped and ws and repo:
        if not branch:
            branch = get_default_branch(user_id, ws, repo)

    try:
        if action == "list_workspaces":
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cur:
                    set_rls_context(cur, conn, user_id, log_prefix="[BitbucketRepos:workspaces]")
                    cur.execute(
                        """SELECT DISTINCT split_part(repo_full_name, '/', 1) AS workspace
                           FROM connected_repos
                           WHERE provider = 'bitbucket'
                             AND repo_full_name LIKE '%%/%%'
                           ORDER BY workspace""",
                    )
                    rows = cur.fetchall()
            if not rows:
                return build_success_response(
                    workspaces=[], count=0,
                    message="No workspaces connected. The user must select repos in the Bitbucket connector settings.",
                )
            workspaces = [{"slug": r[0], "name": r[0]} for r in rows]
            return build_success_response(workspaces=workspaces, count=len(workspaces))

        if action == "get_workspace":
            if not ws:
                return build_error_response("workspace is required")
            return json.dumps(client.get_workspace(ws), default=str)

        if action == "list_repos":
            if not ws:
                return build_error_response("workspace is required")
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cur:
                    set_rls_context(cur, conn, user_id, log_prefix="[BitbucketRepos:list]")
                    cur.execute(
                        """SELECT repo_full_name, default_branch, is_private,
                                  metadata_summary, metadata_status
                           FROM connected_repos
                           WHERE provider = 'bitbucket'
                             AND repo_full_name LIKE %s
                           ORDER BY repo_full_name""",
                        (ws + "/%",),
                    )
                    rows = cur.fetchall()
            if not rows:
                return build_success_response(
                    repositories=[], count=0, workspace=ws,
                    message="No repos connected for this workspace. The user must select repos in the Bitbucket connector settings.",
                )
            repos = []
            for r in rows:
                full_name = r[0]
                slug = full_name.split("/", 1)[1] if "/" in full_name else full_name
                repos.append({
                    "slug": slug,
                    "full_name": full_name,
                    "is_private": r[2],
                    "description": r[3] or ("(generating...)" if r[4] != 'ready' else "(no description)"),
                    "mainbranch": r[1],
                })
            return build_success_response(repositories=repos, count=len(repos), workspace=ws)

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
            result = client.get_file_contents(ws, repo, path, commit=ref)
            if err := forward_if_error(result):
                return err
            if isinstance(result, dict) and isinstance(result.get("content"), str):
                result = {**result, "content": page_file_content(result["content"], start_line or 1)}
            return json.dumps(result, default=str)

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
                    "bitbucket:commit_file"):
                return cancelled
            result = client.create_or_update_file(ws, repo, path, content, message, branch)
            if err := forward_if_error(result):
                return err
            return build_success_response(message=f"File '{path}' committed to {branch}", result=result)

        if action == "edit_file":
            if err := require_repo(ws, repo):
                return build_error_response(err)
            if not path:
                return build_error_response("path is required")
            if not edits:
                return build_error_response("edits is required")
            if not message:
                return build_error_response("message (commit message) is required")
            if not branch:
                return build_error_response("branch is required")
            fetched = client.get_file_contents(ws, repo, path, commit=branch)
            if err := forward_if_error(fetched):
                return err
            original = fetched.get("content") if isinstance(fetched, dict) else None
            if not isinstance(original, str):
                return build_error_response(f"Could not read current contents of {path}")
            suggested, apply_err = apply_edits_checked(original, edits)
            if apply_err or suggested is None:
                return build_error_response(apply_err or "edit application failed")
            if cancelled := confirm_or_cancel(user_id,
                    f"Commit edit to '{path}' on branch '{branch}' in {ws}/{repo}",
                    "bitbucket:commit_file"):
                return cancelled
            result = client.create_or_update_file(ws, repo, path, suggested, message, branch)
            if err := forward_if_error(result):
                return err
            return build_success_response(message=f"File '{path}' edited on {branch}", result=result)

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
                    "bitbucket:delete_file"):
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
