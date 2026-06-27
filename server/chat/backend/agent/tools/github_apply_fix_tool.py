"""
GitHub Apply Fix Tool - Create PRs from approved fix suggestions.

This tool creates a branch and PR from an approved fix suggestion,
allowing users to review and merge code changes identified during RCA.
"""

import logging
from typing import Optional
from pydantic import BaseModel, Field

from .apply_fix_utils import update_suggestion_with_pr, get_fix_suggestion, build_pr_body, generate_branch_name
from .github_mcp_utils import (
    call_github_mcp_sync,
    parse_mcp_response,
    build_error_response,
    build_success_response,
)

logger = logging.getLogger(__name__)


class GitHubApplyFixArgs(BaseModel):
    """Arguments for github_apply_fix tool."""
    suggestion_id: int = Field(
        description="ID of the fix suggestion to apply"
    )
    use_edited_content: bool = Field(
        default=True,
        description="Use user-edited content if available, otherwise use original suggested content"
    )
    target_branch: Optional[str] = Field(
        default=None,
        description="Base branch to create PR against. Defaults to repository's default branch (usually 'main')."
    )


def _get_fix_content(suggestion: dict, use_edited_content: bool) -> Optional[str]:
    """Get the content to use for the fix, preferring edited content if requested."""
    if use_edited_content and suggestion.get("user_edited_content"):
        return suggestion["user_edited_content"]
    return suggestion.get("suggested_content")


def _parse_repository(repo_string: str) -> tuple[Optional[str], Optional[str]]:
    """Parse 'owner/repo' string into tuple."""
    if not repo_string:
        logger.warning("[_parse_repository] Empty repository string provided")
        return None, None
    parts = repo_string.split("/")
    if len(parts) == 2 and parts[0] and parts[1]:
        return parts[0], parts[1]
    logger.warning(f"[_parse_repository] Invalid repository format: '{repo_string}' (expected 'owner/repo')")
    return None, None


def _create_branch(owner: str, repo: str, branch_name: str, base_branch: str, user_id: str) -> Optional[str]:
    """Create a new branch. Returns error message if failed, None on success."""
    result = call_github_mcp_sync(
        "create_branch",
        {"owner": owner, "repo": repo, "branch": branch_name, "from_branch": base_branch},
        user_id
    )
    parsed = parse_mcp_response(result)
    if "error" in parsed:
        return parsed["error"]
    return None


def _push_fix(owner: str, repo: str, branch_name: str, file_path: str,
              content: str, commit_message: str, user_id: str) -> Optional[str]:
    """Push the fix to the branch. Returns error message if failed, None on success."""
    result = call_github_mcp_sync(
        "push_files",
        {
            "owner": owner,
            "repo": repo,
            "branch": branch_name,
            "files": [{"path": file_path, "content": content}],
            "message": commit_message,
        },
        user_id
    )
    parsed = parse_mcp_response(result)
    if "error" in parsed:
        return parsed["error"]
    return None


def _create_pr(owner: str, repo: str, title: str, body: str,
               head: str, base: str, user_id: str) -> tuple[Optional[str], Optional[str], int]:
    """Create a PR. Returns (error, pr_url, pr_number)."""
    result = call_github_mcp_sync(
        "create_pull_request",
        {"owner": owner, "repo": repo, "title": title, "body": body, "head": head, "base": base},
        user_id
    )
    logger.info(f"[_create_pr] Raw MCP response: {result}")
    parsed = parse_mcp_response(result)
    logger.info(f"[_create_pr] Parsed response: {parsed}")

    if "error" in parsed:
        return parsed["error"], None, 0

    # Try multiple possible field names for URL and number
    pr_url = parsed.get("html_url") or parsed.get("url") or parsed.get("pullRequestUrl") or ""
    pr_number = parsed.get("number") or parsed.get("pullRequestNumber") or 0

    # Construct URL if not provided but we have a number
    if not pr_url and pr_number:
        pr_url = f"https://github.com/{owner}/{repo}/pull/{pr_number}"

    # If no URL and no number, log the full response for debugging
    if not pr_url and not pr_number:
        logger.warning(
            f"[_create_pr] No URL or number in MCP response. "
            f"Parsed: {parsed}, Raw result keys: {list(result.keys()) if isinstance(result, dict) else 'not a dict'}"
        )

    return None, pr_url, pr_number


def github_apply_fix(
    suggestion_id: int,
    use_edited_content: bool = True,
    target_branch: Optional[str] = None,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    """
    Apply an approved fix suggestion by creating a branch and PR.

    This tool:
    1. Fetches the fix suggestion from the database
    2. Creates a new branch for the fix
    3. Commits the fix using MCP push_files
    4. Creates a PR using MCP create_pull_request
    5. Updates the suggestion with PR information

    Args:
        suggestion_id: ID of the fix suggestion to apply
        use_edited_content: Use user-edited content if available
        target_branch: Base branch for PR (defaults to main)
        user_id: User ID (injected by tool wrapper)

    Returns:
        JSON string with PR URL and details
    """
    if not user_id:
        return build_error_response("User ID is required")

    # Fetch and validate the suggestion
    suggestion = get_fix_suggestion(suggestion_id, user_id)
    if not suggestion:
        return build_error_response(f"Fix suggestion {suggestion_id} not found or access denied")

    if suggestion.get("pr_url"):
        return build_error_response("PR already created for this suggestion", pr_url=suggestion["pr_url"])

    # Get content to use
    content = _get_fix_content(suggestion, use_edited_content)
    if not content:
        return build_error_response("No content available for this fix")

    # Parse repository
    owner, repo = _parse_repository(suggestion.get("repository", ""))
    if not owner or not repo:
        return build_error_response(f"Invalid repository format: {suggestion.get('repository')}")

    # Prepare branch and commit info
    branch_name = generate_branch_name(suggestion.get("incident_id", ""))
    base_branch = target_branch or "main"
    file_path = suggestion.get("file_path", "")
    commit_message = suggestion.get("commit_message") or f"fix: {suggestion.get('title', 'Aurora fix')}"

    # Validate file_path before any git operations
    if not file_path or not file_path.strip():
        return build_error_response(
            f"Missing file path for suggestion {suggestion_id} ({suggestion.get('title', 'untitled')})"
        )

    # Step 1: Create branch
    logger.info(f"[github_apply_fix] Creating branch {branch_name} from {base_branch}")
    error = _create_branch(owner, repo, branch_name, base_branch, user_id)
    if error:
        return build_error_response(f"Failed to create branch: {error}")

    # Step 2: Push the fix
    logger.info(f"[github_apply_fix] Pushing fix to {file_path}")
    error = _push_fix(owner, repo, branch_name, file_path, content, commit_message, user_id)
    if error:
        return build_error_response(f"Failed to push fix: {error}", branch_created=branch_name)

    # Step 3: Create PR
    pr_title = suggestion.get("title", "Aurora Fix")
    pr_body = build_pr_body(suggestion, file_path)

    logger.info(f"[github_apply_fix] Creating PR: {pr_title}")
    error, pr_url, pr_number = _create_pr(owner, repo, pr_title, pr_body, branch_name, base_branch, user_id)
    if error:
        return build_error_response(f"Failed to create PR: {error}", branch_created=branch_name, commit_pushed=True)

    # Step 4: Update suggestion with PR info
    db_update_success = update_suggestion_with_pr(suggestion_id, pr_url, pr_number, branch_name)
    if not db_update_success:
        logger.error(
            f"[github_apply_fix] PR created but DB update failed for suggestion {suggestion_id}, PR: {pr_url} - floating reference"
        )

    logger.info(f"[github_apply_fix] PR created successfully: {pr_url}")

    return build_success_response(
        message="PR created successfully",
        prUrl=pr_url,
        prNumber=pr_number,
        branch=branch_name,
        repository=f"{owner}/{repo}",
        filePath=file_path,
        dbUpdated=db_update_success,
    )
