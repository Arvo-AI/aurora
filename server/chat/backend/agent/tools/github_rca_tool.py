"""
GitHub RCA Tool - Unified GitHub investigation tool for Root Cause Analysis.

This tool provides structured GitHub investigation capabilities for RCA workflows,
wrapping existing GitHub MCP tools with timeline correlation and intelligent repo resolution.
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, Tuple, List, Literal
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


def _get_kb_memory_content(user_id: str) -> Optional[str]:
    """Fetch Knowledge Base Memory content for a user."""
    try:
        from utils.db.connection_pool import db_pool
        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SET myapp.current_user_id = %s;", (user_id,))
            conn.commit()
            cursor.execute(
                "SELECT content FROM knowledge_base_memory WHERE user_id = %s",
                (user_id,)
            )
            row = cursor.fetchone()
            if row and row[0]:
                return row[0].strip()
    except Exception as e:
        logger.warning(f"Error fetching KB memory for user {user_id}: {e}")
    return None


def _search_kb_documents_for_repos(user_id: str) -> List[Tuple[str, str]]:
    """
    Search Knowledge Base Documents for GitHub repository references.

    Searches for common terms like 'github', 'repository', 'repo' in user's
    uploaded documents (runbooks, etc.) and extracts repo references.

    Returns: List of (owner, repo_name) tuples
    """
    try:
        from routes.knowledge_base.weaviate_client import search_knowledge_base

        # Search for documents mentioning GitHub repos
        results = search_knowledge_base(
            user_id=user_id,
            query="github repository repo source code",
            limit=5,
            alpha=0.3,  # Favor keyword matching for this use case
            min_score=0.0,
        )

        if not results:
            return []

        # Combine all result content and parse for repos
        all_content = "\n".join(r.get("content", "") for r in results)
        return _parse_repos_from_text(all_content)

    except Exception as e:
        logger.warning(f"Error searching KB documents for repos: {e}")
        return []


def _parse_repos_from_text(text: str) -> List[Tuple[str, str]]:
    """
    Extract GitHub repository references from text.

    Looks for patterns like:
    - github.com/owner/repo
    - owner/repo (when preceded by common indicators)

    Returns: List of (owner, repo_name) tuples
    """
    repos: List[Tuple[str, str]] = []
    seen: set[str] = set()

    def add_repo(owner: str, repo: str) -> None:
        """Add repo to results if not already seen."""
        key = f"{owner}/{repo}".lower()
        if key not in seen:
            repos.append((owner, repo))
            seen.add(key)

    # Pattern 1: github.com/owner/repo (with optional branch info)
    github_url_pattern = r'github\.com/([a-zA-Z0-9_.-]+)/([a-zA-Z0-9_.-]+)'
    for match in re.finditer(github_url_pattern, text, re.IGNORECASE):
        owner, repo = match.group(1), match.group(2)
        # Clean up repo name (remove .git suffix, parentheses, etc.)
        repo = re.sub(r'\.git$', '', repo)
        repo = re.sub(r'\).*$', '', repo)
        add_repo(owner, repo)

    # Pattern 2: explicit owner/repo format (common in markdown lists)
    # Match patterns like "- service: owner/repo" or "repo: owner/repo"
    explicit_pattern = r'(?:^|\s|:)\s*([a-zA-Z0-9_.-]+)/([a-zA-Z0-9_.-]+)(?:\s|$|\(|,)'
    false_positives = {'http', 'https', 'git', 'ssh', 'file'}
    for match in re.finditer(explicit_pattern, text, re.MULTILINE):
        owner, repo = match.group(1), match.group(2)
        if owner.lower() not in false_positives:
            add_repo(owner, repo)

    return repos


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
    incident_time: Optional[str] = Field(
        default=None,
        description=(
            "ISO 8601 timestamp of the incident (e.g., '2024-01-15T14:30:00Z'). "
            "Used for automatic time window correlation. If not provided, uses current time."
        )
    )
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
        description="Hours before incident_time to search for changes (default: 24 hours)."
    )
    commit_sha: Optional[str] = Field(
        default=None,
        description="For 'diff' action: specific commit SHA to get diff for."
    )
    workflow_name: Optional[str] = Field(
        default=None,
        description="For 'deployment_check': filter by specific workflow name."
    )


def _select_first_repo(
    repos: List[Tuple[str, str]],
    source: str
) -> Tuple[str, str]:
    """Select first repo from list and log if multiple found."""
    owner, repo_name = repos[0]
    if len(repos) > 1:
        repo_list = ", ".join(f"{o}/{r}" for o, r in repos)
        logger.info(f"Multiple repos found in {source} ({repo_list}), using first: {owner}/{repo_name}")
    return owner, repo_name


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
    Resolve repository using priority order:
    1. Explicit repo parameter
    2. Knowledge Base Memory (repos mentioned by user)
    3. Knowledge Base Documents (search runbooks, docs for repo refs)
    4. Connected repo from github_repo_selection

    Returns: (owner, repo_name, source_description)
    """
    # 1. Explicit repo parameter
    if explicit_repo:
        parsed = _parse_owner_repo(explicit_repo)
        if parsed:
            return parsed[0], parsed[1], "explicit parameter"
        logger.warning(f"Invalid repo format: {explicit_repo}")

    # 2. Knowledge Base Memory - check if user has documented repos
    kb_memory = _get_kb_memory_content(user_id)
    if kb_memory:
        repos = _parse_repos_from_text(kb_memory)
        if repos:
            owner, repo_name = _select_first_repo(repos, "KB Memory")
            return owner, repo_name, "Knowledge Base Memory"

    # 3. Knowledge Base Documents - search uploaded docs (runbooks, etc.)
    doc_repos = _search_kb_documents_for_repos(user_id)
    if doc_repos:
        owner, repo_name = _select_first_repo(doc_repos, "KB Documents")
        return owner, repo_name, "Knowledge Base Documents"

    # 4. Connected repo from user settings (fallback)
    try:
        from utils.auth.stateless_auth import get_credentials_from_db
        selection = get_credentials_from_db(user_id, "github_repo_selection")
        if selection:
            full_name = selection.get("repository", {}).get("full_name")
            if full_name:
                parsed = _parse_owner_repo(full_name)
                if parsed:
                    if kb_memory:
                        logger.warning(
                            f"No GitHub repo found in Knowledge Base, falling back to connected repo: {full_name}"
                        )
                    return parsed[0], parsed[1], "connected repository (fallback)"
    except Exception as e:
        logger.warning(f"Error getting connected repo: {e}")

    return None, None, "no repository found"


def _calculate_time_windows(
    incident_time: Optional[str],
    time_window_hours: int = 24
) -> Tuple[datetime, datetime]:
    """
    Calculate investigation time windows based on incident time.

    Returns: (start_time, end_time)
    """
    # Validate time_window_hours - clamp to sensible default if invalid
    if not isinstance(time_window_hours, int) or time_window_hours <= 0:
        logger.warning(f"Invalid time_window_hours={time_window_hours}, using default of 24")
        time_window_hours = 24

    # Parse incident time or use current time
    if incident_time:
        try:
            # Handle various ISO 8601 formats
            incident_time_clean = incident_time.replace('Z', '+00:00')
            end_time = datetime.fromisoformat(incident_time_clean)
            # Ensure timezone awareness
            if end_time.tzinfo is None:
                end_time = end_time.replace(tzinfo=timezone.utc)
        except ValueError as e:
            logger.warning(f"Could not parse incident_time '{incident_time}': {e}, using current time")
            end_time = datetime.now(timezone.utc)
    else:
        end_time = datetime.now(timezone.utc)

    # Calculate start time
    start_time = end_time - timedelta(hours=time_window_hours)

    return start_time, end_time


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

    # Build arguments for list_workflow_runs
    args = {
        "owner": owner,
        "repo": repo,
    }
    if branch:
        args["branch"] = branch

    # Call list_workflow_runs
    raw_result = _call_github_mcp_sync("list_workflow_runs", args, user_id)
    parsed = _parse_mcp_response(raw_result)

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

    # Build arguments for list_commits
    args = {
        "owner": owner,
        "repo": repo,
    }
    if branch:
        args["sha"] = branch

    # Note: GitHub API 'since' parameter format
    args["since"] = start_time.strftime('%Y-%m-%dT%H:%M:%SZ')

    # Call list_commits
    raw_result = _call_github_mcp_sync("list_commits", args, user_id)
    parsed = _parse_mcp_response(raw_result)

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

    # Call get_commit
    args = {
        "owner": owner,
        "repo": repo,
        "ref": commit_sha,
    }

    raw_result = _call_github_mcp_sync("get_commit", args, user_id)
    parsed = _parse_mcp_response(raw_result)

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

    # Call list_pull_requests for closed PRs
    args = {
        "owner": owner,
        "repo": repo,
        "state": "closed",
    }
    if branch:
        args["base"] = branch

    raw_result = _call_github_mcp_sync("list_pull_requests", args, user_id)
    parsed = _parse_mcp_response(raw_result)

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
    """Generate hints to help correlate findings with incident."""
    hints = []

    if action == "deployment_check":
        if results.get("failed_runs"):
            hints.append(f"Found {len(results['failed_runs'])} FAILED workflow runs in time window - investigate these first")
        if results.get("suspicious_runs"):
            hints.append(f"Found {len(results['suspicious_runs'])} workflow runs completed within 2 hours of incident")

    elif action == "commits":
        if results.get("suspicious_commits"):
            hints.append(f"Found {len(results['suspicious_commits'])} commits within 2 hours of incident - high priority for review")
        total = results.get("summary", {}).get("total_commits", 0)
        if total > 10:
            hints.append(f"High commit activity ({total} commits) - consider narrowing time window")

    elif action == "diff":
        summary = results.get("summary", {})
        if summary.get("total_changes", 0) > 100:
            hints.append(f"Large change ({summary.get('total_changes')} lines) - review carefully")
        files = results.get("files_changed", [])
        config_files = [f for f in files if any(ext in f.get("filename", "").lower()
                       for ext in ['.yaml', '.yml', '.json', '.env', 'config', 'k8s/', 'deploy/', 'terraform/'])]
        if config_files:
            hints.append(f"Found {len(config_files)} config/infra files changed - likely candidates for root cause")

    elif action == "pull_requests":
        if results.get("recently_merged"):
            hints.append(f"Found {len(results['recently_merged'])} PRs merged within 2 hours of incident")

    return hints


def _format_output(
    action: str,
    results: Dict[str, Any],
    owner: str,
    repo: str,
    repo_source: str,
    time_window: Tuple[datetime, datetime]
) -> str:
    """Format results as JSON string for LLM consumption."""
    output = {
        "status": "success" if "error" not in results else "error",
        "action": action,
        "repository": f"{owner}/{repo}",
        "repository_source": repo_source,
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

        return _format_output(action, results, owner, repo_name, repo_source, time_window)

    except Exception as e:
        logger.error(f"Error in github_rca: {e}", exc_info=True)
        return json.dumps({
            "status": "error",
            "error": f"GitHub RCA failed: {str(e)}",
            "action": action,
            "repository": f"{owner}/{repo_name}",
        })
