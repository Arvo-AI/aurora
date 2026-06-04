"""
Bitbucket Apply Fix Tool - Create PRs from approved fix suggestions.

Mirrors github_apply_fix_tool but uses the Bitbucket API client directly.
"""

import logging
from typing import Optional

from .utils import (
    get_bb_client_for_user,
    forward_if_error,
    build_error_response,
    build_success_response,
)
from chat.backend.agent.tools.apply_fix_utils import (
    get_fix_suggestion,
    build_pr_body,
    generate_branch_name,
    update_suggestion_with_pr,
)

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[bitbucket_apply_fix]"


def _parse_repository(repo_string: str) -> tuple[Optional[str], Optional[str]]:
    """Parse 'workspace/repo_slug' string into tuple."""
    if not repo_string:
        return None, None
    parts = repo_string.split("/")
    if len(parts) == 2 and parts[0] and parts[1]:
        return parts[0], parts[1]
    return None, None


def bitbucket_apply_fix(
    suggestion_id: int,
    use_edited_content: bool = True,
    target_branch: Optional[str] = None,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    """
    Apply an approved fix suggestion by creating a branch and PR on Bitbucket.

    Flow:
    1. Fetch the fix suggestion from the DB
    2. Resolve workspace/repo from connected_repos
    3. Create a branch from the target (default) branch
    4. Commit the fix file to that branch
    5. Open a pull request back to the target branch
    6. Update the suggestion row with the PR link
    """
    if not user_id:
        return build_error_response("User ID is required")

    client = get_bb_client_for_user(user_id)
    if not client:
        return build_error_response("Bitbucket not connected. Please connect Bitbucket first.")

    suggestion = get_fix_suggestion(suggestion_id, user_id)
    if not suggestion:
        return build_error_response(f"Fix suggestion {suggestion_id} not found or access denied")

    if suggestion.get("pr_url"):
        return build_error_response(
            "PR already created for this suggestion", pr_url=suggestion["pr_url"]
        )

    if use_edited_content and suggestion.get("user_edited_content"):
        content = suggestion["user_edited_content"]
    else:
        content = suggestion.get("suggested_content")

    if not content:
        return build_error_response("No content available for this fix")

    # Resolve workspace + repo from the suggestion's repository field
    repo_string = suggestion.get("repository", "")
    workspace, repo_slug = _parse_repository(repo_string)

    if not workspace or not repo_slug:
        return build_error_response(
            f"Cannot determine Bitbucket workspace/repo for repository: {repo_string}"
        )

    # Determine the base branch
    if target_branch:
        base_branch = target_branch
    else:
        base_branch = None
        repo_info = client.get_repository(workspace, repo_slug)
        if isinstance(repo_info, dict) and not repo_info.get("error"):
            base_branch = repo_info.get("mainbranch", {}).get("name")

        if not base_branch:
            return build_error_response(
                f"Could not determine default branch for {workspace}/{repo_slug}. "
                "Try re-saving your Bitbucket workspace selection."
            )

    file_path = suggestion.get("file_path", "")
    if not file_path or not file_path.strip():
        return build_error_response(
            f"Missing file path for suggestion {suggestion_id} ({suggestion.get('title', 'untitled')})"
        )

    commit_message = suggestion.get("commit_message") or f"fix: {suggestion.get('title', 'Aurora fix')}"
    branch_name = generate_branch_name(suggestion.get("incident_id", ""))

    # Step 1: Resolve the base branch to a commit hash for branch creation
    logger.info(f"{_LOG_PREFIX} Creating branch {branch_name} from {base_branch}")
    branches = client.get_branches(workspace, repo_slug)
    if isinstance(branches, dict) and branches.get("error"):
        return build_error_response(f"Failed to list branches: {branches.get('message')}")

    base_hash = None
    if isinstance(branches, list):
        for b in branches:
            if isinstance(b, dict) and b.get("name") == base_branch:
                base_hash = b.get("target", {}).get("hash")
                break

    if not base_hash:
        return build_error_response(
            f"Could not find base branch '{base_branch}' in {workspace}/{repo_slug}"
        )

    # Step 2: Create the fix branch
    result = client.create_branch(workspace, repo_slug, branch_name, base_hash)
    if err := forward_if_error(result):
        return build_error_response(f"Failed to create branch: {result.get('message', err)}")

    # Step 3: Commit the file to the new branch
    logger.info(f"{_LOG_PREFIX} Pushing fix to {file_path} on branch {branch_name}")
    result = client.create_or_update_file(
        workspace, repo_slug, file_path, content, commit_message, branch_name
    )
    if err := forward_if_error(result):
        return build_error_response(
            f"Failed to push fix: {result.get('message', err)}",
            branch_created=branch_name,
        )

    # Step 4: Create the pull request
    pr_title = suggestion.get("title", "Aurora Fix")
    pr_body = build_pr_body(suggestion, file_path)

    logger.info(f"{_LOG_PREFIX} Creating PR: {pr_title}")
    result = client.create_pull_request(
        workspace, repo_slug,
        title=pr_title,
        source_branch=branch_name,
        dest_branch=base_branch,
        description=pr_body,
        close_source=True,
    )
    if err := forward_if_error(result):
        return build_error_response(
            f"Failed to create PR: {result.get('message', err)}",
            branch_created=branch_name,
            commit_pushed=True,
        )

    pr_id = result.get("id", 0)
    pr_url = result.get("links", {}).get("html", {}).get("href", "")
    if not pr_url and pr_id:
        pr_url = f"https://bitbucket.org/{workspace}/{repo_slug}/pull-requests/{pr_id}"

    # Step 5: Update the suggestion row
    db_ok = update_suggestion_with_pr(suggestion_id, pr_url, pr_id, branch_name)
    if not db_ok:
        logger.error(
            f"{_LOG_PREFIX} PR created but DB update failed for suggestion {suggestion_id}, PR: {pr_url}"
        )

    logger.info(f"{_LOG_PREFIX} PR created: {pr_url}")

    return build_success_response(
        message="PR created successfully",
        prUrl=pr_url,
        prNumber=pr_id,
        branch=branch_name,
        repository=f"{workspace}/{repo_slug}",
        filePath=file_path,
        dbUpdated=db_ok,
    )
