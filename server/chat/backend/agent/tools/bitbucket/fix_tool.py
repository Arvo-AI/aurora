"""
Bitbucket Fix Tool - Suggest code fixes during RCA.

The LLM proposes anchored search-and-replace
edits, the server fetches the current file from Bitbucket, applies the edits,
stores the resulting full file body as the suggestion, and the user reviews +
creates the PR from the UI.

Reuses the edit-application logic from github_fix_tool (replacer chain).
"""

import logging
from typing import Optional

from pydantic import BaseModel, Field

from .utils import (
    get_bb_client_for_user,
    get_default_branch,
    build_error_response,
    build_success_response,
)

from chat.backend.agent.tools.vcs_rca_utils import resolve_repository as _vcs_resolve_repository
from .apply_fix_tool import _parse_repository

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[bitbucket_fix]"


# ---------------------------------------------------------------------------
# Args schema
# ---------------------------------------------------------------------------


class FixEdit(BaseModel):
    """A single anchored search-and-replace edit."""
    old_string: str = Field(
        description=(
            "Exact text to match in the current file. Include enough surrounding "
            "context (typically 1-3 lines above and below the change) to make the "
            "match unique. Whitespace counts."
        )
    )
    new_string: str = Field(
        description="Replacement text. Indentation must match what belongs at that location."
    )
    replace_all: bool = Field(
        default=False,
        description="Replace every occurrence of old_string. Default False requires exactly one match.",
    )


class BitbucketFixArgs(BaseModel):
    """Arguments for bitbucket_fix tool."""
    file_path: str = Field(
        description="Path to the file in the repository (e.g., 'config/deployment.yaml', 'src/app.py')"
    )
    edits: list[FixEdit] = Field(
        min_length=1,
        description=(
            "One or more anchored search-and-replace edits applied sequentially. "
            "Each edit's old_string must match the file exactly once "
            "(unless replace_all=true). Edits operate on the result of prior edits."
        ),
    )
    fix_description: str = Field(
        description="Human-readable description of what this fix does."
    )
    root_cause_summary: str = Field(
        description="Summary of why this change is needed - what root cause it addresses."
    )
    commit_message: Optional[str] = Field(
        default=None,
        description="Suggested commit message. If not provided, one is generated."
    )
    repo: Optional[str] = Field(
        default=None,
        description="Repository in 'workspace/repo_slug' format. Required when multiple repos are connected."
    )
    branch: Optional[str] = Field(
        default=None,
        description="Target branch for the fix. Defaults to the repository's default branch."
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_file_content(
    user_id: str,
    workspace: str,
    repo_slug: str,
    file_path: str,
    branch: Optional[str],
) -> Optional[str]:
    """Fetch file contents from Bitbucket. Returns text content or None."""
    client = get_bb_client_for_user(user_id)
    if not client:
        return None

    ref = branch or "HEAD"
    result = client.get_file_contents(workspace, repo_slug, file_path, commit=ref)

    if isinstance(result, dict):
        if result.get("error"):
            logger.warning(
                "%s Failed to fetch %s from %s/%s: %s",
                _LOG_PREFIX, file_path, workspace, repo_slug, result.get("message"),
            )
            return None
        return result.get("content")

    if isinstance(result, str):
        return result

    return None


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
    """Save a fix suggestion to the database."""
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
                    "fix",
                    "medium",
                    file_path,
                    original_content,
                    suggested_content,
                    repository,
                    commit_message,
                ),
            )
            result = cursor.fetchone()
            conn.commit()
            suggestion_id = result[0] if result else None
            if suggestion_id:
                logger.info(f"{_LOG_PREFIX} Saved fix suggestion {suggestion_id} for incident {incident_id}")
            return suggestion_id
    except Exception as e:
        logger.error(f"{_LOG_PREFIX} Failed to save fix suggestion: {e}", exc_info=True)
        return None


def _build_title(file_path: str, fix_description: str) -> str:
    filename = file_path.split("/")[-1]
    truncated_desc = fix_description[:50]
    suffix = "..." if len(fix_description) > 50 else ""
    return f"Fix {filename}: {truncated_desc}{suffix}"


# ---------------------------------------------------------------------------
# Main tool function
# ---------------------------------------------------------------------------


def bitbucket_fix(
    file_path: str,
    edits: list,
    fix_description: str,
    root_cause_summary: str,
    commit_message: Optional[str] = None,
    repo: Optional[str] = None,
    branch: Optional[str] = None,
    user_id: Optional[str] = None,
    incident_id: Optional[str] = None,
    **kwargs,
) -> str:
    """Suggest a code fix via anchored multi-edit applied server-side."""
    if not user_id:
        return build_error_response("User ID is required")
    if not incident_id:
        return build_error_response("Incident ID is required. This tool should be used during RCA.")
    if not edits:
        return build_error_response("edits must contain at least one entry")

    repo_path, source = _vcs_resolve_repository(user_id, "bitbucket", repo)
    workspace, repo_slug = _parse_repository(repo_path) if repo_path else (None, None)
    if not workspace or not repo_slug:
        return build_error_response(
            "Could not resolve repository. Please specify repo='workspace/repo_slug' or add repo info to Knowledge Base."
        )

    full_repo = f"{workspace}/{repo_slug}"
    logger.info(f"{_LOG_PREFIX} Using repository {full_repo} (resolved from {source})")

    # Resolve branch if not specified
    effective_branch = branch
    if not effective_branch:
        effective_branch = get_default_branch(user_id, workspace, repo_slug)

    original_content = _get_file_content(user_id, workspace, repo_slug, file_path, effective_branch)
    if original_content is None:
        # Support creating new files: if all edits have empty old_string, treat as new file
        all_empty_old = all(
            (e.get("old_string", "") if isinstance(e, dict) else getattr(e, "old_string", "")) == ""
            for e in edits
        )
        if all_empty_old:
            # New file creation — concatenate all new_string values
            suggested_content = "\n".join(
                e.get("new_string", "") if isinstance(e, dict) else getattr(e, "new_string", "")
                for e in edits
            )
            original_content = None
            logger.info(
                "%s File %s does not exist, creating new file (%d bytes)",
                _LOG_PREFIX, file_path, len(suggested_content),
            )
        else:
            return build_error_response(
                f"Could not fetch current contents of {file_path} from {full_repo}. "
                "The file does not exist. To create a new file, set old_string to an empty string."
            )
    else:
        # File exists — apply edits using the shared replacer chain from github_fix_tool
        from chat.backend.agent.tools.github_fix_tool import _apply_edits

        suggested_content, apply_err = _apply_edits(original_content, edits)
        if apply_err or suggested_content is None:
            logger.warning("%s edit application failed for %s: %s", _LOG_PREFIX, file_path, apply_err)
            return build_error_response(apply_err or "edit application failed")

        if suggested_content == original_content:
            return build_error_response(
                "Applied edits produced no change to the file. Double-check old_string/new_string."
            )

    if not suggested_content.strip():
        return build_error_response(
            "Applied edits produced an empty (or whitespace-only) file. If you "
            "really intend to empty this file, do it manually — bitbucket_fix is "
            "for targeted code changes."
        )

    final_commit_message = commit_message or f"fix: {fix_description[:100]}"
    title = _build_title(file_path, fix_description)
    description = f"{fix_description}\n\n**Root Cause:** {root_cause_summary}"

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
        edits_applied=len(edits),
        next_steps="The user can review and edit the suggested fix in the Incidents UI, then create a PR when ready.",
    )
