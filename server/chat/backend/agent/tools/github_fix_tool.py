"""
GitHub Fix Tool - Suggest code fixes during RCA.

This tool allows the RCA agent to suggest code fixes when it identifies
a code issue that caused an incident. The fix is stored as a suggestion
for user review before being applied.
"""

import logging
from typing import Optional
from pydantic import BaseModel, Field

from .github_mcp_utils import (
    call_github_mcp_sync,
    parse_file_content_response,
    build_error_response,
    build_success_response,
)

logger = logging.getLogger(__name__)


class GitHubFixArgs(BaseModel):
    """Arguments for github_fix tool."""
    file_path: str = Field(
        description="Path to the file in the repository (e.g., 'config/deployment.yaml', 'src/app.py')"
    )
    suggested_content: str = Field(
        description="The complete suggested file content with the fix applied. Must be the full file, not just the diff."
    )
    fix_description: str = Field(
        description="Human-readable description of what this fix does (e.g., 'Increase memory limit from 256Mi to 512Mi')"
    )
    root_cause_summary: str = Field(
        description="Summary of why this change is needed - what root cause does it address"
    )
    commit_message: Optional[str] = Field(
        default=None,
        description="Suggested commit message for this fix. If not provided, one will be generated."
    )
    repo: Optional[str] = Field(
        default=None,
        description="Repository in 'owner/repo' format. If not provided, uses Knowledge Base mapping or connected repo."
    )
    branch: Optional[str] = Field(
        default=None,
        description="Target branch for the fix. Defaults to repository's default branch."
    )


def _resolve_repository(
    user_id: str,
    explicit_repo: Optional[str] = None
) -> tuple[Optional[str], Optional[str], str]:
    """
    Resolve repository using the same logic as github_rca_tool.
    Imported from github_rca_tool to maintain consistency.
    """
    from .github_rca_tool import _resolve_repository as rca_resolve_repo
    return rca_resolve_repo(user_id, explicit_repo)


def _get_file_content(owner: str, repo: str, path: str, branch: Optional[str], user_id: str) -> Optional[str]:
    """Fetch current file content from GitHub using MCP."""
    args = {"owner": owner, "repo": repo, "path": path}
    if branch:
        args["branch"] = branch

    result = call_github_mcp_sync("get_file_contents", args, user_id)
    return parse_file_content_response(result)


def _save_fix_suggestion(
    incident_id: str,
    user_id: str,
    title: str,
    description: str,
    file_path: str,
    original_content: Optional[str],
    suggested_content: str,
    repository: str,
    commit_message: Optional[str],
) -> Optional[int]:
    """Save fix suggestion to database."""
    from utils.db.connection_pool import db_pool

    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO incident_suggestions
                (incident_id, title, description, type, risk, file_path,
                 original_content, suggested_content, repository, command)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    incident_id,
                    title,
                    description,
                    "fix",  # type
                    "medium",  # risk - code changes are medium risk
                    file_path,
                    original_content,
                    suggested_content,
                    repository,
                    commit_message,  # Store commit message in command field for now
                )
            )
            result = cursor.fetchone()
            conn.commit()
            return result[0] if result else None
    except Exception as e:
        logger.error(f"Failed to save fix suggestion: {e}")
        return None


def _build_title(file_path: str, fix_description: str) -> str:
    """Build a concise title from file path and description."""
    filename = file_path.split('/')[-1]
    truncated_desc = fix_description[:50]
    suffix = "..." if len(fix_description) > 50 else ""
    return f"Fix {filename}: {truncated_desc}{suffix}"


def github_fix(
    file_path: str,
    suggested_content: str,
    fix_description: str,
    root_cause_summary: str,
    commit_message: Optional[str] = None,
    repo: Optional[str] = None,
    branch: Optional[str] = None,
    user_id: Optional[str] = None,
    incident_id: Optional[str] = None,
    **kwargs,
) -> str:
    """
    Suggest a code fix for an identified issue during RCA.

    Creates an incident_suggestion with type='fix' that stores the proposed
    change for user review. The user can then edit the suggestion and create
    a PR when ready.

    Args:
        file_path: Path to the file in the repository
        suggested_content: Complete fixed file content
        fix_description: What this fix does
        root_cause_summary: Why this change is needed
        commit_message: Optional commit message
        repo: Optional repository in 'owner/repo' format
        branch: Optional target branch
        user_id: User ID (injected by tool wrapper)
        incident_id: Incident ID (injected by tool wrapper)

    Returns:
        JSON string with result status and details
    """
    if not user_id:
        return build_error_response("User ID is required")

    if not incident_id:
        return build_error_response("Incident ID is required. This tool should be used during RCA.")

    # Resolve repository
    owner, repo_name, source = _resolve_repository(user_id, repo)
    if not owner or not repo_name:
        return build_error_response(
            "Could not resolve repository. Please specify repo parameter "
            "(e.g., repo='owner/repo') or add repository info to Knowledge Base."
        )

    full_repo = f"{owner}/{repo_name}"
    logger.info(f"[github_fix] Using repository {full_repo} (resolved from {source})")

    # Fetch original file content (optional - we proceed without it if unavailable)
    original_content = _get_file_content(owner, repo_name, file_path, branch, user_id)
    if original_content is None:
        logger.warning(f"Could not fetch original content for {file_path}, proceeding without it")

    # Generate commit message if not provided
    final_commit_message = commit_message or f"fix: {fix_description[:100]}"

    # Build title and description
    title = _build_title(file_path, fix_description)
    description = f"{fix_description}\n\n**Root Cause:** {root_cause_summary}"

    # Save to database
    suggestion_id = _save_fix_suggestion(
        incident_id=incident_id,
        user_id=user_id,
        title=title,
        description=description,
        file_path=file_path,
        original_content=original_content,
        suggested_content=suggested_content,
        repository=full_repo,
        commit_message=final_commit_message,
    )

    if not suggestion_id:
        return build_error_response("Failed to save fix suggestion to database")

    return build_success_response(
        message="Fix suggestion saved for user review",
        suggestion_id=suggestion_id,
        repository=full_repo,
        file_path=file_path,
        has_original_content=original_content is not None,
        next_steps="The user can review and edit the suggested fix in the Incidents UI, then create a PR when ready."
    )
