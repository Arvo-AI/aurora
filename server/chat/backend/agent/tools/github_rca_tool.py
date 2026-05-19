"""
GitHub RCA Tool - Unified GitHub investigation tool for Root Cause Analysis.

This tool provides structured GitHub investigation capabilities for RCA workflows,
wrapping existing GitHub MCP tools with timeline correlation and intelligent repo resolution.
"""

import json
import logging
from datetime import datetime
from typing import Dict, Any, Optional, Tuple, List, Literal

import requests
from pydantic import BaseModel, Field

from utils.auth.github_auth_router import (
    NoGitHubAuthError,
    get_auth_for_user_repo,
    make_auth_header,
)

logger = logging.getLogger(__name__)


class GitHubRCAArgs(BaseModel):
    """Arguments for github_rca tool."""
    action: Literal["deployment_check", "commits", "diff", "pull_requests"] = Field(
        description=(
            "Action to perform: "
            "'deployment_check' (check GitHub Actions workflow runs and deployments), "
            "'commits' (list recent commits with timeline correlation), "
            "'diff' (show file changes for a specific commit), "
            "'pull_requests' (list merged PRs in time window)"
        )
    )
    # incident_time is intentionally excluded from this schema.
    # It is always injected from the authoritative DB timestamp (incidents.started_at)
    # by the tool wrapper in cloud_tools.py. Exposing it here would allow the agent
    # LLM to supply a hallucinated value.
    repo: Optional[str] = Field(
        default=None,
        description=(
            "Repository in 'owner/repo' format (e.g., 'apple/imessage-relay'). "
            "If not provided, uses Knowledge Base mapping or connected repo."
        )
    )
    branch: Optional[str] = Field(
        default=None,
        description="Branch to investigate. Defaults to repository's default branch (usually 'main')."
    )
    time_window_hours: int = Field(
        default=24,
        description="Hours before the incident to search for changes (default: 24 hours)."
    )
    commit_sha: Optional[str] = Field(
        default=None,
        description="For 'diff' action: specific commit SHA to get diff for."
    )
    workflow_name: Optional[str] = Field(
        default=None,
        description="For 'deployment_check': filter by specific workflow name."
    )


def _parse_owner_repo(full_name: str) -> Optional[Tuple[str, str]]:
    """Parse 'owner/repo' string into tuple, returns None if invalid format."""
    parts = full_name.split('/')
    if len(parts) == 2:
        return parts[0], parts[1]
    return None


def _resolve_repository(
    user_id: str,
    explicit_repo: Optional[str] = None
) -> Tuple[Optional[str], Optional[str], str]:
    """
    Resolve repository using priority order.
    Delegates to shared vcs_rca_utils with provider='github'.

    Returns: (owner, repo_name, source_description)
    """
    from .vcs_rca_utils import resolve_repository
    repo_path, source = resolve_repository(user_id, "github", explicit_repo)
    if not repo_path:
        return None, None, source
    parsed = _parse_owner_repo(repo_path)
    if parsed:
        return parsed[0], parsed[1], source
    return None, None, f"invalid repo format: {repo_path}"


def _calculate_time_windows(
    incident_time: Optional[str],
    time_window_hours: int = 24
) -> Tuple[datetime, datetime]:
    """Delegate to shared vcs_rca_utils."""
    from .vcs_rca_utils import calculate_time_windows
    return calculate_time_windows(incident_time, time_window_hours)


def _call_github_mcp_sync(
    tool_name: str,
    arguments: Dict[str, Any],
    user_id: str,
    timeout: int = 60
) -> Dict[str, Any]:
    """
    Synchronous wrapper to call GitHub MCP tools.
    Uses existing RealMCPServerManager infrastructure.
    """
    from .mcp_tools import _mcp_manager, run_async_in_thread

    async def _async_call():
        # Ensure GitHub MCP server is initialized
        await _mcp_manager.initialize_mcp_server("github", user_id)
        # Call the tool
        return await _mcp_manager.call_mcp_tool(
            server_type="github",
            tool_name=tool_name,
            arguments=arguments
        )

    try:
        result = run_async_in_thread(_async_call(), timeout=timeout)
        return result if result else {"error": "No response from MCP"}
    except Exception as e:
        logger.error(f"MCP call failed for {tool_name}: {e}")
        return {"error": str(e)}


def _parse_mcp_response(result: Dict[str, Any]) -> Dict[str, Any]:
    """Parse MCP response content to extract data."""
    if not result:
        return {}

    # Check for error
    if "error" in result:
        return {"error": result["error"]}

    # Extract content from standard MCP response format
    content = result.get("content", [])
    if content and isinstance(content, list) and len(content) > 0:
        first_content = content[0]
        if isinstance(first_content, dict) and first_content.get("type") == "text":
            text = first_content.get("text", "")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                # MCP returned non-JSON text (e.g., error message), wrap it to preserve
                return {"text": text}

    return result


def _call_github_api(
    user_id: str,
    owner: str,
    repo: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = 30,
):
    """Call GitHub REST API for a user/repo using the auth router.

    Bypasses the MCP github server (which spawns a Docker container and
    therefore cannot run in Aurora's chatbot service, which deliberately
    has no docker socket). The auth router transparently picks App or
    OAuth credentials per the hybrid mode rules and returns a token that
    works with the standard ``Authorization: token <value>`` header.

    Returns the parsed JSON body on success (list or dict, matching
    GitHub's native shape so existing downstream parsers keep working),
    or ``{"error": "..."}`` on any failure.
    """
    repo_full_name = f"{owner}/{repo}"
    try:
        auth = get_auth_for_user_repo(user_id, repo_full_name)
    except NoGitHubAuthError as exc:
        return {"error": f"No GitHub credential for {repo_full_name}: {exc}"}
    except Exception as exc:
        logger.exception("Auth resolution failed for user=%s repo=%s", user_id, repo_full_name)
        return {"error": f"GitHub auth resolution failed: {type(exc).__name__}"}

    headers = {
        **make_auth_header(auth),
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = f"https://api.github.com/repos/{owner}/{repo}{path}"

    try:
        resp = requests.get(url, headers=headers, params=params or {}, timeout=timeout)
    except requests.RequestException as exc:
        return {"error": f"GitHub API request failed: {type(exc).__name__}"}

    if resp.status_code == 404:
        return {"error": f"GitHub returned 404 for {path} (repo private without App access, or path wrong)"}
    if resp.status_code == 403:
        return {"error": f"GitHub returned 403 for {path} (rate limit or missing permission)"}
    if resp.status_code != 200:
        return {"error": f"GitHub API status={resp.status_code} for {path}: {resp.text[:200]}"}

    try:
        return resp.json()
    except ValueError:
        return {"error": "GitHub response was not valid JSON"}


def _action_deployment_check(
    owner: str,
    repo: str,
    branch: Optional[str],
    start_time: datetime,
    end_time: datetime,
    workflow_name: Optional[str],
    user_id: str
) -> Dict[str, Any]:
    """
    Check GitHub Actions workflow runs and deployments.

    MCP tools used: list_workflow_runs
    """
    results = {
        "workflow_runs": [],
        "failed_runs": [],
        "suspicious_runs": [],  # Completed close to incident
        "summary": {}
    }

    # GET /repos/{owner}/{repo}/actions/runs
    params: Dict[str, Any] = {"per_page": 50}
    if branch:
        params["branch"] = branch

    parsed = _call_github_api(user_id, owner, repo, "/actions/runs", params)

    if isinstance(parsed, dict) and "error" in parsed:
        return {"error": parsed["error"]}

    # Extract workflow runs
    workflow_runs = []
    if isinstance(parsed, dict):
        workflow_runs = parsed.get("workflow_runs", [])
    elif isinstance(parsed, list):
        workflow_runs = parsed

    # Filter and categorize runs within time window
    for run in workflow_runs:
        try:
            # Parse run creation time
            created_at_str = run.get("created_at", "")
            if not created_at_str:
                continue

            run_time = datetime.fromisoformat(created_at_str.replace('Z', '+00:00'))

            # Check if within time window
            if start_time <= run_time <= end_time:
                # Filter by workflow name if specified
                if workflow_name:
                    run_workflow_name = run.get("name", "") or run.get("workflow", {}).get("name", "")
                    if workflow_name.lower() not in run_workflow_name.lower():
                        continue

                run_info = {
                    "id": run.get("id"),
                    "name": run.get("name") or run.get("workflow", {}).get("name", "Unknown"),
                    "status": run.get("status"),
                    "conclusion": run.get("conclusion"),
                    "created_at": created_at_str,
                    "updated_at": run.get("updated_at"),
                    "html_url": run.get("html_url"),
                    "head_sha": run.get("head_sha"),
                    "head_branch": run.get("head_branch"),
                }

                results["workflow_runs"].append(run_info)

                # Categorize
                conclusion = run.get("conclusion", "")
                if conclusion == "failure":
                    results["failed_runs"].append(run_info)
                elif conclusion == "success":
                    # Check if completed close to incident (within 2 hours)
                    updated_at_str = run.get("updated_at", "")
                    if updated_at_str:
                        updated_time = datetime.fromisoformat(updated_at_str.replace('Z', '+00:00'))
                        time_diff = (end_time - updated_time).total_seconds()
                        if 0 <= time_diff <= 7200:  # Within 2 hours before incident
                            results["suspicious_runs"].append(run_info)
        except Exception as e:
            logger.warning(f"Error processing workflow run: {e}")
            continue

    # Build summary
    results["summary"] = {
        "total_runs": len(results["workflow_runs"]),
        "failed_runs": len(results["failed_runs"]),
        "suspicious_runs": len(results["suspicious_runs"]),
    }

    return results


def _action_commits(
    owner: str,
    repo: str,
    branch: Optional[str],
    start_time: datetime,
    end_time: datetime,
    user_id: str
) -> Dict[str, Any]:
    """
    List recent commits with timeline correlation.

    MCP tools used: list_commits
    """
    results = {
        "commits": [],
        "suspicious_commits": [],  # Within 2 hours of incident
        "summary": {}
    }

    # GET /repos/{owner}/{repo}/commits
    params: Dict[str, Any] = {
        "since": start_time.strftime('%Y-%m-%dT%H:%M:%SZ'),
        "per_page": 100,
    }
    if branch:
        params["sha"] = branch

    parsed = _call_github_api(user_id, owner, repo, "/commits", params)

    if isinstance(parsed, dict) and "error" in parsed:
        return {"error": parsed["error"]}

    # Extract commits
    commits = []
    if isinstance(parsed, list):
        commits = parsed
    elif isinstance(parsed, dict):
        commits = parsed.get("commits", []) or [parsed] if parsed.get("sha") else []

    # Process and filter commits
    for commit in commits:
        try:
            # Extract commit info
            commit_data = commit.get("commit", {}) or {}
            author_data = commit_data.get("author", {}) or {}

            commit_date_str = author_data.get("date", "")
            if not commit_date_str:
                # Try alternative location
                commit_date_str = commit.get("authored_date", "")

            if not commit_date_str:
                continue

            commit_time = datetime.fromisoformat(commit_date_str.replace('Z', '+00:00'))

            # Check if within time window
            if commit_time > end_time:
                continue

            commit_info = {
                "sha": commit.get("sha", "")[:8],  # Short SHA
                "full_sha": commit.get("sha", ""),
                "message": commit_data.get("message", "").split('\n')[0],  # First line only
                "author": author_data.get("name", "Unknown"),
                "date": commit_date_str,
                "html_url": commit.get("html_url", ""),
            }

            results["commits"].append(commit_info)

            # Flag as suspicious if within 2 hours of incident
            time_diff = (end_time - commit_time).total_seconds()
            if 0 <= time_diff <= 7200:  # Within 2 hours
                results["suspicious_commits"].append(commit_info["sha"])

        except Exception as e:
            logger.warning(f"Error processing commit: {e}")
            continue

    # Build summary
    results["summary"] = {
        "total_commits": len(results["commits"]),
        "suspicious_commits": len(results["suspicious_commits"]),
    }

    return results


def _action_diff(
    owner: str,
    repo: str,
    commit_sha: str,
    user_id: str
) -> Dict[str, Any]:
    """
    Get diff for a specific commit.

    MCP tools used: get_commit
    """
    results = {
        "commit": {},
        "files_changed": [],
        "summary": {}
    }

    if not commit_sha:
        return {"error": "commit_sha is required for diff action"}

    # GET /repos/{owner}/{repo}/commits/{ref}
    parsed = _call_github_api(user_id, owner, repo, f"/commits/{commit_sha}")

    if isinstance(parsed, dict) and "error" in parsed:
        return {"error": parsed["error"]}

    if not isinstance(parsed, dict):
        return {"error": "Unexpected response format from get_commit"}

    # Extract commit info
    commit_data = parsed.get("commit", {}) or {}
    author_data = commit_data.get("author", {}) or {}

    results["commit"] = {
        "sha": parsed.get("sha", "")[:8],
        "full_sha": parsed.get("sha", ""),
        "message": commit_data.get("message", ""),
        "author": author_data.get("name", "Unknown"),
        "date": author_data.get("date", ""),
        "html_url": parsed.get("html_url", ""),
    }

    # Extract file changes
    files = parsed.get("files", []) or []
    for f in files:
        file_info = {
            "filename": f.get("filename", ""),
            "status": f.get("status", ""),  # added, removed, modified
            "additions": f.get("additions", 0),
            "deletions": f.get("deletions", 0),
            "changes": f.get("changes", 0),
            "patch": f.get("patch", "")[:500] if f.get("patch") else "",  # Truncate patch
        }
        results["files_changed"].append(file_info)

    # Build summary
    total_additions = sum(f.get("additions", 0) for f in files)
    total_deletions = sum(f.get("deletions", 0) for f in files)

    results["summary"] = {
        "files_count": len(files),
        "additions": total_additions,
        "deletions": total_deletions,
        "total_changes": total_additions + total_deletions,
    }

    return results


def _action_pull_requests(
    owner: str,
    repo: str,
    branch: Optional[str],
    start_time: datetime,
    end_time: datetime,
    user_id: str
) -> Dict[str, Any]:
    """
    List merged pull requests in the time window.

    MCP tools used: list_pull_requests
    """
    results = {
        "merged_prs": [],
        "recently_merged": [],  # Merged close to incident
        "summary": {}
    }

    # GET /repos/{owner}/{repo}/pulls — closed only, sorted by updated desc
    params: Dict[str, Any] = {
        "state": "closed",
        "sort": "updated",
        "direction": "desc",
        "per_page": 100,
    }
    if branch:
        params["base"] = branch

    parsed = _call_github_api(user_id, owner, repo, "/pulls", params)

    if isinstance(parsed, dict) and "error" in parsed:
        return {"error": parsed["error"]}

    # Extract PRs
    prs = []
    if isinstance(parsed, list):
        prs = parsed
    elif isinstance(parsed, dict):
        prs = parsed.get("items", []) or [parsed] if parsed.get("number") else []

    # Filter to merged PRs within time window
    for pr in prs:
        try:
            merged_at_str = pr.get("merged_at")
            if not merged_at_str:
                continue  # Not merged, skip

            merged_time = datetime.fromisoformat(merged_at_str.replace('Z', '+00:00'))

            # Check if within time window
            if not (start_time <= merged_time <= end_time):
                continue

            pr_info = {
                "number": pr.get("number"),
                "title": pr.get("title", ""),
                "author": pr.get("user", {}).get("login", "Unknown"),
                "merged_at": merged_at_str,
                "merged_by": pr.get("merged_by", {}).get("login", "Unknown") if pr.get("merged_by") else "Unknown",
                "html_url": pr.get("html_url", ""),
                "base_branch": pr.get("base", {}).get("ref", ""),
                "head_branch": pr.get("head", {}).get("ref", ""),
            }

            results["merged_prs"].append(pr_info)

            # Flag as recently merged if within 2 hours of incident
            time_diff = (end_time - merged_time).total_seconds()
            if 0 <= time_diff <= 7200:
                results["recently_merged"].append(pr_info["number"])

        except Exception as e:
            logger.debug(f"Error processing PR: {e}")
            continue

    # Build summary
    results["summary"] = {
        "total_merged": len(results["merged_prs"]),
        "recently_merged": len(results["recently_merged"]),
    }

    return results


def _generate_correlation_hints(
    action: str,
    results: Dict[str, Any]
) -> List[str]:
    """Delegate to shared vcs_rca_utils."""
    from .vcs_rca_utils import generate_correlation_hints
    return generate_correlation_hints(action, results)


def _format_output(
    action: str,
    results: Dict[str, Any],
    owner: str,
    repo: str,
    repo_source: str,
    time_window: Tuple[datetime, datetime],
    auth_method: Optional[str] = None,
    installation_id: Optional[int] = None,
) -> str:
    """Format results as JSON string for LLM consumption."""
    output = {
        "status": "success" if "error" not in results else "error",
        "action": action,
        "repository": f"{owner}/{repo}",
        "repository_source": repo_source,
        "auth_method": auth_method,
        "installation_id": installation_id,
        "time_window": {
            "start": time_window[0].strftime('%Y-%m-%dT%H:%M:%SZ'),
            "end": time_window[1].strftime('%Y-%m-%dT%H:%M:%SZ'),
            "hours": int((time_window[1] - time_window[0]).total_seconds() / 3600),
        },
        "results": results,
    }

    # Add correlation hints if not an error
    if "error" not in results:
        hints = _generate_correlation_hints(action, results)
        if hints:
            output["correlation_hints"] = hints

    return json.dumps(output, indent=2, default=str)


def github_rca(
    action: str,
    incident_time: Optional[str] = None,
    repo: Optional[str] = None,
    branch: Optional[str] = None,
    time_window_hours: int = 24,
    commit_sha: Optional[str] = None,
    workflow_name: Optional[str] = None,
    user_id: Optional[str] = None,
    **kwargs
) -> str:
    """
    Unified GitHub investigation tool for Root Cause Analysis.

    This tool provides structured GitHub investigation for RCA workflows.
    It wraps GitHub MCP tools with timeline correlation and intelligent repo resolution.

    Actions:
    - deployment_check: Check GitHub Actions workflow runs for failures or recent deployments
    - commits: List recent commits with automatic timeline correlation
    - diff: Show file changes for a specific commit (requires commit_sha)
    - pull_requests: List merged PRs in the time window

    Args:
        action: The action to perform (deployment_check, commits, diff, pull_requests)
        incident_time: ISO 8601 timestamp of incident for time correlation
        repo: Repository in 'owner/repo' format (auto-resolved if not provided)
        branch: Branch to investigate (defaults to main)
        time_window_hours: Hours before incident to search (default: 24)
        commit_sha: For 'diff' action - specific commit to get diff for
        workflow_name: For 'deployment_check' - filter by workflow name
        user_id: User ID (injected by decorator)
        **kwargs: Additional arguments (ignored, for decorator compatibility)

    Returns:
        JSON string with investigation results and correlation hints
    """
    logger.info(f"github_rca called: action={action}, repo={repo}, user_id={user_id}")

    # Validate user_id
    if not user_id:
        return json.dumps({
            "status": "error",
            "error": "User context not available. Ensure you are authenticated.",
        })

    # Validate action
    valid_actions = ["deployment_check", "commits", "diff", "pull_requests"]
    if action not in valid_actions:
        return json.dumps({
            "status": "error",
            "error": f"Invalid action '{action}'. Valid actions: {', '.join(valid_actions)}",
        })

    # Resolve repository
    owner, repo_name, repo_source = _resolve_repository(user_id, repo)
    if not owner or not repo_name:
        return json.dumps({
            "status": "error",
            "error": "No repository found. Please specify 'repo' parameter (e.g., 'owner/repo'), add repo info to Knowledge Base Memory, or connect a GitHub repository in settings.",
            "hint": "Options: 1) Pass repo='owner/repo' parameter, 2) Add repo to Knowledge Base (Memory or upload a runbook with 'github.com/owner/repo'), 3) Connect a repo in Settings > Integrations > GitHub.",
        })

    logger.info(f"Resolved repository: {owner}/{repo_name} (source: {repo_source})")

    repo_full_name = f"{owner}/{repo_name}"
    try:
        auth = get_auth_for_user_repo(user_id, repo_full_name)
    except NoGitHubAuthError:
        return json.dumps({
            "status": "error",
            "error": "GitHub not connected for this user. Install the GitHub App or connect via OAuth.",
            "action": action,
            "repository": repo_full_name,
        })
    except Exception as e:
        logger.exception(
            "Error resolving GitHub auth for user %s repo %s",
            user_id, repo_full_name,
        )
        return json.dumps({
            "status": "error",
            "error": f"Failed to resolve GitHub auth: {e}",
            "action": action,
            "repository": repo_full_name,
        })

    logger.info(
        f"Resolved GitHub auth for {user_id} {repo_full_name}: "
        f"method={auth.method} installation_id={auth.installation_id}"
    )

    # Calculate time windows
    start_time, end_time = _calculate_time_windows(incident_time, time_window_hours)
    time_window = (start_time, end_time)

    logger.info(f"Time window: {start_time} to {end_time}")

    # Execute action
    try:
        if action == "deployment_check":
            results = _action_deployment_check(
                owner, repo_name, branch, start_time, end_time, workflow_name, user_id
            )
        elif action == "commits":
            results = _action_commits(
                owner, repo_name, branch, start_time, end_time, user_id
            )
        elif action == "diff":
            if not commit_sha:
                return json.dumps({
                    "status": "error",
                    "error": "commit_sha is required for 'diff' action. First use 'commits' action to find suspicious commits.",
                })
            results = _action_diff(owner, repo_name, commit_sha, user_id)
        elif action == "pull_requests":
            results = _action_pull_requests(
                owner, repo_name, branch, start_time, end_time, user_id
            )

        return _format_output(
            action,
            results,
            owner,
            repo_name,
            repo_source,
            time_window,
            auth_method=auth.method,
            installation_id=auth.installation_id,
        )

    except Exception as e:
        logger.error(f"Error in github_rca: {e}", exc_info=True)
        return json.dumps({
            "status": "error",
            "error": f"GitHub RCA failed: {str(e)}",
            "action": action,
            "repository": f"{owner}/{repo_name}",
        })
