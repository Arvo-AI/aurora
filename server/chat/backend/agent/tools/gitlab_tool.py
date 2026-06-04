"""
Unified GitLab Tool — single entry point for all GitLab operations.

Actions:
  - list_projects: List connected GitLab projects
  - deployment_check: Check CI/CD pipelines for failures
  - commits: List recent commits with timeline correlation
  - diff: Show file changes for a specific commit
  - merge_requests: List merged MRs in time window
  - suggest_fix: Suggest a code fix (saved for user review)
  - apply_fix: Create a branch + MR from an approved fix suggestion
  - commit_terraform: Push Terraform files to a project
"""

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, Literal
from urllib.parse import quote

from pydantic import BaseModel, Field

from .apply_fix_utils import update_suggestion_with_pr

from routes.gitlab.gitlab_api_utils import gitlab_api_request, build_error_response, build_success_response, is_gitlab_connected
from utils.auth.command_gate import gate_action
from utils.db.connection_pool import db_pool
from utils.auth.stateless_auth import set_rls_context
from .vcs_rca_utils import resolve_repository, calculate_time_windows, get_connected_repos_for_provider, generate_correlation_hints
from .iac.iac_write_tool import get_terraform_directory

logger = logging.getLogger(__name__)


class GitLabToolArgs(BaseModel):
    """Arguments for the unified gitlab tool."""
    action: Literal[
        "list_projects",
        "deployment_check", "commits", "diff", "merge_requests",
        "suggest_fix", "apply_fix", "commit_terraform",
        "create_branch", "push_files", "create_merge_request", "delete_branch",
    ] = Field(
        description=(
            "Action to perform: "
            "'list_projects' (list connected projects), "
            "'deployment_check' (check CI/CD pipelines), "
            "'commits' (recent commits with timeline correlation), "
            "'diff' (file changes for a commit — requires commit_sha), "
            "'merge_requests' (merged MRs in time window), "
            "'suggest_fix' (propose a code fix — requires file_path, suggested_content, fix_description, root_cause_summary), "
            "'apply_fix' (create MR from approved suggestion — requires suggestion_id), "
            "'commit_terraform' (push Terraform files — requires repo, commit_message), "
            "'create_branch' (create a new branch — requires branch, optionally target_branch as base), "
            "'push_files' (push file changes to a branch — requires branch, file_path, suggested_content, commit_message), "
            "'create_merge_request' (open an MR — requires branch, target_branch), "
            "'delete_branch' (delete a branch — requires branch)"
        )
    )
    repo: Optional[str] = Field(default=None, description="Project path 'namespace/project'. Auto-resolves if only one connected.")
    branch: Optional[str] = Field(default=None, description="Branch to target. Defaults to project's default branch.")
    incident_time: Optional[str] = Field(default=None, description="ISO 8601 incident timestamp for time-window correlation.")
    time_window_hours: int = Field(default=24, description="Hours before incident_time to search (default: 24).")
    commit_sha: Optional[str] = Field(default=None, description="For 'diff': specific commit SHA.")
    # suggest_fix / push_files params
    file_path: Optional[str] = Field(default=None, description="For 'suggest_fix'/'push_files': path to the file in the project.")
    suggested_content: Optional[str] = Field(default=None, description="For 'suggest_fix'/'push_files': complete file content.")
    fix_description: Optional[str] = Field(default=None, description="For 'suggest_fix': what this fix does.")
    root_cause_summary: Optional[str] = Field(default=None, description="For 'suggest_fix': why this change is needed.")
    commit_message: Optional[str] = Field(default=None, description="For 'suggest_fix'/'commit_terraform'/'push_files': commit message.")
    # apply_fix params
    suggestion_id: Optional[int] = Field(default=None, description="For 'apply_fix': ID of the fix suggestion to apply.")
    target_branch: Optional[str] = Field(default=None, description="For 'apply_fix'/'create_branch'/'create_merge_request': base/target branch (default: main).")
    # create_merge_request params
    mr_title: Optional[str] = Field(default=None, description="For 'create_merge_request': MR title.")
    mr_description: Optional[str] = Field(default=None, description="For 'create_merge_request': MR description body.")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _resolve_repository(user_id: str, explicit_repo: Optional[str] = None) -> Tuple[Optional[str], str]:
    return resolve_repository(user_id, "gitlab", explicit_repo)


def _calculate_time_windows(incident_time: Optional[str], time_window_hours: int = 24):
    return calculate_time_windows(incident_time, time_window_hours)


# ---------------------------------------------------------------------------
# Action: list_projects
# ---------------------------------------------------------------------------

def _action_list_projects(user_id: str) -> str:
    return get_connected_repos_for_provider(user_id, "gitlab")


# ---------------------------------------------------------------------------
# Actions: RCA investigation (deployment_check, commits, diff, merge_requests)
# ---------------------------------------------------------------------------

def _action_deployment_check(
    project_path: str, branch: Optional[str],
    start_time: datetime, end_time: datetime, user_id: str,
) -> Dict[str, Any]:
    results: Dict[str, Any] = {"pipelines": [], "failed_pipelines": [], "suspicious_pipelines": [], "summary": {}}
    encoded = quote(project_path, safe="")
    params: Dict[str, Any] = {
        "updated_after": start_time.strftime('%Y-%m-%dT%H:%M:%SZ'),
        "updated_before": end_time.strftime('%Y-%m-%dT%H:%M:%SZ'),
        "order_by": "updated_at", "sort": "desc", "per_page": 50,
    }
    if branch:
        params["ref"] = branch

    resp = gitlab_api_request("GET", f"/projects/{encoded}/pipelines", user_id, params=params)
    if isinstance(resp, dict) and "error" in resp:
        return resp

    for pipeline in (resp if isinstance(resp, list) else []):
        info = {
            "id": pipeline.get("id"), "status": pipeline.get("status"),
            "ref": pipeline.get("ref"), "sha": pipeline.get("sha", "")[:8],
            "created_at": pipeline.get("created_at"), "updated_at": pipeline.get("updated_at"),
            "web_url": pipeline.get("web_url"),
        }
        results["pipelines"].append(info)
        if pipeline.get("status") == "failed":
            results["failed_pipelines"].append(info)
        elif pipeline.get("status") == "success":
            updated_str = pipeline.get("updated_at", "")
            if updated_str:
                try:
                    updated_time = datetime.fromisoformat(updated_str.replace('Z', '+00:00'))
                    if 0 <= (end_time - updated_time).total_seconds() <= 7200:
                        results["suspicious_pipelines"].append(info)
                except (ValueError, TypeError):
                    pass  # Non-critical: skip time-correlation for malformed timestamps

    results["summary"] = {
        "total_pipelines": len(results["pipelines"]),
        "failed": len(results["failed_pipelines"]),
        "suspicious": len(results["suspicious_pipelines"]),
    }
    return results


def _action_commits(
    project_path: str, branch: Optional[str],
    start_time: datetime, end_time: datetime, user_id: str,
) -> Dict[str, Any]:
    results: Dict[str, Any] = {"commits": [], "suspicious_commits": [], "summary": {}}
    encoded = quote(project_path, safe="")
    params: Dict[str, Any] = {
        "since": start_time.strftime('%Y-%m-%dT%H:%M:%SZ'),
        "until": end_time.strftime('%Y-%m-%dT%H:%M:%SZ'),
        "per_page": 50,
    }
    if branch:
        params["ref_name"] = branch

    resp = gitlab_api_request("GET", f"/projects/{encoded}/repository/commits", user_id, params=params)
    if isinstance(resp, dict) and "error" in resp:
        return resp

    for commit in (resp if isinstance(resp, list) else []):
        commit_date_str = commit.get("committed_date") or commit.get("created_at", "")
        commit_info = {
            "sha": commit.get("short_id", commit.get("id", "")[:8]),
            "full_sha": commit.get("id", ""),
            "message": commit.get("title", ""),
            "author": commit.get("author_name", "Unknown"),
            "date": commit_date_str,
            "web_url": commit.get("web_url", ""),
        }
        results["commits"].append(commit_info)
        if commit_date_str:
            try:
                commit_time = datetime.fromisoformat(commit_date_str.replace('Z', '+00:00'))
                if 0 <= (end_time - commit_time).total_seconds() <= 7200:
                    results["suspicious_commits"].append(commit_info["sha"])
            except (ValueError, TypeError):
                pass  # Non-critical: skip time-correlation for malformed timestamps

    results["summary"] = {"total_commits": len(results["commits"]), "suspicious_commits": len(results["suspicious_commits"])}
    return results


def _action_diff(project_path: str, commit_sha: str, user_id: str) -> Dict[str, Any]:
    if not commit_sha:
        return {"error": "commit_sha is required for diff action"}

    encoded = quote(project_path, safe="")
    resp = gitlab_api_request("GET", f"/projects/{encoded}/repository/commits/{commit_sha}/diff", user_id)
    if isinstance(resp, dict) and "error" in resp:
        return resp

    commit_resp = gitlab_api_request("GET", f"/projects/{encoded}/repository/commits/{commit_sha}", user_id)
    results: Dict[str, Any] = {"commit": {}, "files_changed": [], "summary": {}}

    if isinstance(commit_resp, dict) and "error" not in commit_resp:
        results["commit"] = {
            "sha": commit_resp.get("short_id", ""), "full_sha": commit_resp.get("id", ""),
            "message": commit_resp.get("message", ""), "author": commit_resp.get("author_name", "Unknown"),
            "date": commit_resp.get("committed_date", ""), "web_url": commit_resp.get("web_url", ""),
        }

    diffs = resp if isinstance(resp, list) else []
    total_add = total_del = 0
    for d in diffs:
        additions = d.get("diff", "").count("\n+") - d.get("diff", "").count("\n+++")
        deletions = d.get("diff", "").count("\n-") - d.get("diff", "").count("\n---")
        file_info = {
            "filename": d.get("new_path", d.get("old_path", "")),
            "status": "added" if d.get("new_file") else "deleted" if d.get("deleted_file") else "modified",
            "additions": max(0, additions), "deletions": max(0, deletions),
            "patch": d.get("diff", "")[:500],
        }
        results["files_changed"].append(file_info)
        total_add += file_info["additions"]
        total_del += file_info["deletions"]

    results["summary"] = {"files_count": len(diffs), "additions": total_add, "deletions": total_del, "total_changes": total_add + total_del}
    return results


def _action_merge_requests(
    project_path: str, branch: Optional[str],
    start_time: datetime, end_time: datetime, user_id: str,
) -> Dict[str, Any]:
    results: Dict[str, Any] = {"merged_mrs": [], "recently_merged": [], "summary": {}}
    encoded = quote(project_path, safe="")
    params: Dict[str, Any] = {
        "state": "merged",
        "updated_after": start_time.strftime('%Y-%m-%dT%H:%M:%SZ'),
        "updated_before": end_time.strftime('%Y-%m-%dT%H:%M:%SZ'),
        "order_by": "updated_at", "sort": "desc", "per_page": 50,
    }
    if branch:
        params["target_branch"] = branch

    resp = gitlab_api_request("GET", f"/projects/{encoded}/merge_requests", user_id, params=params)
    if isinstance(resp, dict) and "error" in resp:
        return resp

    for mr in (resp if isinstance(resp, list) else []):
        merged_at_str = mr.get("merged_at", "")
        if not merged_at_str:
            continue
        mr_info = {
            "iid": mr.get("iid"), "title": mr.get("title", ""),
            "author": mr.get("author", {}).get("username", "Unknown"),
            "merged_at": merged_at_str,
            "merged_by": mr.get("merged_by", {}).get("username", "Unknown") if mr.get("merged_by") else "Unknown",
            "web_url": mr.get("web_url", ""),
            "target_branch": mr.get("target_branch", ""), "source_branch": mr.get("source_branch", ""),
        }
        results["merged_mrs"].append(mr_info)
        try:
            merged_time = datetime.fromisoformat(merged_at_str.replace('Z', '+00:00'))
            if 0 <= (end_time - merged_time).total_seconds() <= 7200:
                results["recently_merged"].append(mr_info["iid"])
        except (ValueError, TypeError):
            pass  # Non-critical: skip time-correlation for malformed timestamps

    results["summary"] = {"total_merged": len(results["merged_mrs"]), "recently_merged": len(results["recently_merged"])}
    return results


# ---------------------------------------------------------------------------
# Action: suggest_fix
# ---------------------------------------------------------------------------

def _action_suggest_fix(
    user_id: str, incident_id: Optional[str],
    repo: Optional[str], branch: Optional[str],
    file_path: Optional[str], suggested_content: Optional[str],
    fix_description: Optional[str], root_cause_summary: Optional[str],
    commit_message: Optional[str],
) -> str:
    if not incident_id:
        return build_error_response("incident_id is required for suggest_fix")
    if not file_path or not suggested_content or not fix_description or not root_cause_summary:
        return build_error_response("file_path, suggested_content, fix_description, and root_cause_summary are all required")

    if not gate_action(
        user_id=user_id,
        tool_name="gitlab:suggest_fix",
        summary=f"Suggest fix for {file_path}: {fix_description[:80]}",
    ).allowed:
        return build_error_response("Operation cancelled by user")

    project_path, source = _resolve_repository(user_id, repo)
    if not project_path:
        return build_error_response(f"Could not resolve project: {source}")

    encoded_project = quote(project_path, safe="")
    encoded_file = quote(file_path, safe="")
    params = {"ref": branch} if branch else {}
    resp = gitlab_api_request("GET", f"/projects/{encoded_project}/repository/files/{encoded_file}/raw", user_id, params=params, raw_response=True)
    original_content = resp if isinstance(resp, str) else None

    final_commit_message = commit_message or f"fix: {fix_description[:100]}"
    filename = file_path.split('/')[-1]
    title = f"Fix {filename}: {fix_description[:50]}{'...' if len(fix_description) > 50 else ''}"
    description = f"{fix_description}\n\n**Root Cause:** {root_cause_summary}"

    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO incident_suggestions
                   (incident_id, title, description, type, risk, file_path,
                    original_content, suggested_content, repository, command)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (incident_id, title, description, "fix", "medium",
                 file_path, original_content, suggested_content, project_path, final_commit_message),
            )
            result = cursor.fetchone()
            conn.commit()
            suggestion_id = result[0] if result else None
    except Exception as e:
        logger.error(f"Failed to save fix suggestion: {e}", exc_info=True)
        return build_error_response("Failed to save fix suggestion to database")

    if not suggestion_id:
        return build_error_response("Failed to save fix suggestion to database")

    return build_success_response(
        message="Fix suggestion saved for user review",
        suggestion_id=suggestion_id, project=project_path, file_path=file_path,
        has_original_content=original_content is not None,
        next_steps="The user can review the fix in the Incidents UI, then call action='apply_fix' when ready.",
    )


# ---------------------------------------------------------------------------
# Action: apply_fix
# ---------------------------------------------------------------------------

def _action_apply_fix(
    user_id: str, suggestion_id: Optional[int], target_branch: Optional[str],
    use_edited_content: bool = True,
) -> str:
    """
    Create a branch + MR for a fix suggestion.

    Approval gate: This function is invoked from the UI 'Create PR' button
    (server/routes/incidents_routes.py), which constitutes explicit user approval.
    The agent SKILL.md instructs the agent not to call apply_fix autonomously.
    """
    if not suggestion_id:
        return build_error_response("suggestion_id is required for apply_fix")

    suggestion = None
    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            set_rls_context(cursor, conn, user_id, log_prefix="[gitlab:apply_fix]")
            cursor.execute(
                """SELECT s.id, s.incident_id, s.title, s.description, s.file_path,
                          s.original_content, s.suggested_content, s.user_edited_content,
                          s.repository, s.command, s.pr_url, s.created_branch
                   FROM incident_suggestions s JOIN incidents i ON s.incident_id = i.id
                   WHERE s.id = %s AND s.type = 'fix'""",
                (suggestion_id,),
            )
            row = cursor.fetchone()
            if row:
                suggestion = {
                    "id": row[0], "incident_id": str(row[1]), "title": row[2],
                    "description": row[3], "file_path": row[4], "original_content": row[5],
                    "suggested_content": row[6], "user_edited_content": row[7],
                    "repository": row[8], "commit_message": row[9],
                    "pr_url": row[10], "created_branch": row[11],
                }
    except Exception as e:
        logger.error(f"Failed to fetch fix suggestion: {e}")
        return build_error_response(f"Database error: {e}")

    if not suggestion:
        return build_error_response(f"Fix suggestion {suggestion_id} not found or access denied")
    if suggestion.get("pr_url"):
        return build_error_response("MR already created for this suggestion", mr_url=suggestion["pr_url"])

    content = (suggestion.get("user_edited_content") or suggestion.get("suggested_content")) if use_edited_content else suggestion.get("suggested_content")
    if not content:
        return build_error_response("No content available for this fix")

    project_path = suggestion.get("repository", "")
    if not project_path:
        return build_error_response("No project path in suggestion")

    encoded_project = quote(project_path, safe="")

    # Resolve default branch from project metadata when not explicitly provided
    if not target_branch:
        project_meta = gitlab_api_request("GET", f"/projects/{encoded_project}", user_id)
        base_branch = project_meta.get("default_branch", "main") if isinstance(project_meta, dict) and "error" not in project_meta else "main"
    else:
        base_branch = target_branch
    file_path = suggestion.get("file_path", "")
    commit_msg = suggestion.get("commit_message") or f"fix: {suggestion.get('title', 'Aurora fix')}"

    incident_short = suggestion.get("incident_id", "unknown")[:8]
    branch_name = f"fix/aurora-{incident_short}-{int(time.time())}"

    if not gate_action(
        user_id=user_id,
        tool_name="gitlab:create_branch",
        summary=f"Create branch '{branch_name}' from '{base_branch}' in {project_path}",
    ).allowed:
        return build_error_response("Branch creation cancelled by user")

    resp = gitlab_api_request("POST", f"/projects/{encoded_project}/repository/branches", user_id,
                              json_body={"branch": branch_name, "ref": base_branch})
    if isinstance(resp, dict) and "error" in resp:
        return build_error_response(f"Failed to create branch: {resp['error']}")

    encoded_file = quote(file_path, safe="")
    file_check = gitlab_api_request("GET", f"/projects/{encoded_project}/repository/files/{encoded_file}", user_id,
                                    params={"ref": branch_name})
    action_type = "update" if (isinstance(file_check, dict) and "error" not in file_check) else "create"

    if not gate_action(
        user_id=user_id,
        tool_name="gitlab:push_files",
        summary=f"Push fix to '{file_path}' on branch '{branch_name}'",
    ).allowed:
        return build_error_response("File push cancelled by user", branch_created=branch_name)

    commit_resp = gitlab_api_request("POST", f"/projects/{encoded_project}/repository/commits", user_id,
                                     json_body={"branch": branch_name, "commit_message": commit_msg,
                                                "actions": [{"action": action_type, "file_path": file_path, "content": content}]})
    if isinstance(commit_resp, dict) and "error" in commit_resp:
        # Best-effort cleanup of orphaned branch
        try:
            gitlab_api_request("DELETE", f"/projects/{encoded_project}/repository/branches/{quote(branch_name, safe='')}", user_id)
        except Exception:
            pass  # Don't mask the original commit error
        return build_error_response(f"Failed to commit fix: {commit_resp['error']}", branch_created=branch_name)

    mr_title = suggestion.get("title", "Aurora Fix")
    mr_body = (
        f"## Incident Fix\n\n**Incident ID**: {suggestion.get('incident_id', 'N/A')}\n\n"
        f"### Description\n{suggestion.get('description', 'No description')}\n\n"
        f"### File Changed\n- `{file_path}`\n\n---\n*Created by Aurora from an RCA fix suggestion.*"
    )

    if not gate_action(
        user_id=user_id,
        tool_name="gitlab:create_merge_request",
        summary=f"Create MR '{mr_title}' targeting '{base_branch}' in {project_path}",
    ).allowed:
        return build_error_response("Merge request creation cancelled by user", branch_created=branch_name)

    mr_resp = gitlab_api_request("POST", f"/projects/{encoded_project}/merge_requests", user_id,
                                 json_body={"source_branch": branch_name, "target_branch": base_branch,
                                            "title": mr_title, "description": mr_body})
    if isinstance(mr_resp, dict) and "error" in mr_resp:
        return build_error_response(f"Failed to create MR: {mr_resp['error']}", branch_created=branch_name)

    mr_url = mr_resp.get("web_url", "")
    mr_iid = mr_resp.get("iid", 0)

    if not update_suggestion_with_pr(suggestion_id, mr_url, mr_iid, branch_name):
        return build_success_response(
            message="Merge Request created but DB sync failed — duplicate MR guard may not work on retry",
            mrUrl=mr_url, mrIid=mr_iid, branch=branch_name,
            project=project_path, filePath=file_path, db_synced=False,
        )

    return build_success_response(message="Merge Request created", mrUrl=mr_url, mrIid=mr_iid,
                                  branch=branch_name, project=project_path, filePath=file_path)


# ---------------------------------------------------------------------------
# Action: create_branch
# ---------------------------------------------------------------------------

def _action_create_branch(
    user_id: str, repo: Optional[str], branch: Optional[str], target_branch: Optional[str],
) -> str:
    if not branch:
        return build_error_response("branch is required for create_branch")

    project_path, source = _resolve_repository(user_id, repo)
    if not project_path:
        return build_error_response(f"Could not resolve project: {source}")

    encoded_project = quote(project_path, safe="")

    if not target_branch:
        project_meta = gitlab_api_request("GET", f"/projects/{encoded_project}", user_id)
        base = project_meta.get("default_branch", "main") if isinstance(project_meta, dict) and "error" not in project_meta else "main"
    else:
        base = target_branch

    if not gate_action(
        user_id=user_id,
        tool_name="gitlab:create_branch",
        summary=f"Create branch '{branch}' from '{base}' in {project_path}",
    ).allowed:
        return build_error_response("Branch creation cancelled by user")

    resp = gitlab_api_request("POST", f"/projects/{encoded_project}/repository/branches", user_id,
                              json_body={"branch": branch, "ref": base})
    if isinstance(resp, dict) and "error" in resp:
        return build_error_response(f"Failed to create branch: {resp['error']}")

    return build_success_response(
        message=f"Branch '{branch}' created from '{base}'",
        branch=branch, base=base, project=project_path,
        web_url=resp.get("web_url", ""),
    )


# ---------------------------------------------------------------------------
# Action: push_files
# ---------------------------------------------------------------------------

def _action_push_files(
    user_id: str, repo: Optional[str], branch: Optional[str],
    file_path: Optional[str], content: Optional[str], commit_message: Optional[str],
) -> str:
    if not branch:
        return build_error_response("branch is required for push_files")
    if not file_path or not content:
        return build_error_response("file_path and suggested_content are required for push_files")
    if not commit_message:
        return build_error_response("commit_message is required for push_files")

    project_path, source = _resolve_repository(user_id, repo)
    if not project_path:
        return build_error_response(f"Could not resolve project: {source}")

    encoded_project = quote(project_path, safe="")

    if not gate_action(
        user_id=user_id,
        tool_name="gitlab:push_files",
        summary=f"Push changes to '{file_path}' on branch '{branch}' in {project_path}",
    ).allowed:
        return build_error_response("File push cancelled by user")

    encoded_file = quote(file_path, safe="")
    file_check = gitlab_api_request("GET", f"/projects/{encoded_project}/repository/files/{encoded_file}", user_id,
                                    params={"ref": branch})
    action_type = "update" if (isinstance(file_check, dict) and "error" not in file_check) else "create"

    resp = gitlab_api_request("POST", f"/projects/{encoded_project}/repository/commits", user_id,
                              json_body={"branch": branch, "commit_message": commit_message,
                                         "actions": [{"action": action_type, "file_path": file_path, "content": content}]})
    if isinstance(resp, dict) and "error" in resp:
        return build_error_response(f"Failed to push files: {resp['error']}")

    return build_success_response(
        message=f"Pushed {action_type} to '{file_path}' on branch '{branch}'",
        commit_sha=resp.get("id", ""), commit_url=resp.get("web_url", ""),
        project=project_path, branch=branch, file_path=file_path,
    )


# ---------------------------------------------------------------------------
# Action: create_merge_request
# ---------------------------------------------------------------------------

def _action_create_merge_request(
    user_id: str, repo: Optional[str], branch: Optional[str],
    target_branch: Optional[str], mr_title: Optional[str], mr_description: Optional[str],
) -> str:
    if not branch:
        return build_error_response("branch (source branch) is required for create_merge_request")

    project_path, source = _resolve_repository(user_id, repo)
    if not project_path:
        return build_error_response(f"Could not resolve project: {source}")

    encoded_project = quote(project_path, safe="")

    if not target_branch:
        project_meta = gitlab_api_request("GET", f"/projects/{encoded_project}", user_id)
        target = project_meta.get("default_branch", "main") if isinstance(project_meta, dict) and "error" not in project_meta else "main"
    else:
        target = target_branch

    title = mr_title or f"Merge '{branch}' into '{target}'"

    if not gate_action(
        user_id=user_id,
        tool_name="gitlab:create_merge_request",
        summary=f"Create MR '{title}' from '{branch}' to '{target}' in {project_path}",
    ).allowed:
        return build_error_response("Merge request creation cancelled by user")

    resp = gitlab_api_request("POST", f"/projects/{encoded_project}/merge_requests", user_id,
                              json_body={"source_branch": branch, "target_branch": target,
                                         "title": title, "description": mr_description or ""})
    if isinstance(resp, dict) and "error" in resp:
        return build_error_response(f"Failed to create MR: {resp['error']}")

    return build_success_response(
        message=f"Merge request created",
        mr_url=resp.get("web_url", ""), mr_iid=resp.get("iid", 0),
        project=project_path, source_branch=branch, target_branch=target,
    )


# ---------------------------------------------------------------------------
# Action: delete_branch
# ---------------------------------------------------------------------------

def _action_delete_branch(user_id: str, repo: Optional[str], branch: Optional[str]) -> str:
    if not branch:
        return build_error_response("branch is required for delete_branch")

    project_path, source = _resolve_repository(user_id, repo)
    if not project_path:
        return build_error_response(f"Could not resolve project: {source}")

    encoded_project = quote(project_path, safe="")

    if not gate_action(
        user_id=user_id,
        tool_name="gitlab:delete_branch",
        summary=f"Delete branch '{branch}' in {project_path}",
    ).allowed:
        return build_error_response("Branch deletion cancelled by user")

    resp = gitlab_api_request("DELETE", f"/projects/{encoded_project}/repository/branches/{quote(branch, safe='')}", user_id)
    if isinstance(resp, dict) and "error" in resp:
        return build_error_response(f"Failed to delete branch: {resp['error']}")

    return build_success_response(message=f"Branch '{branch}' deleted", project=project_path, branch=branch)


# ---------------------------------------------------------------------------
# Action: commit_terraform
# ---------------------------------------------------------------------------

def _action_commit_terraform(
    user_id: str, repo: Optional[str], branch: Optional[str],
    commit_message: Optional[str], session_id: Optional[str],
) -> str:
    if not repo:
        return build_error_response("repo is required for commit_terraform")
    if not commit_message:
        return build_error_response("commit_message is required for commit_terraform")

    if not gate_action(
        user_id=user_id,
        tool_name="gitlab:commit_terraform",
        summary=f"Commit Terraform files to {repo}: {commit_message}",
    ).allowed:
        return build_error_response("Operation cancelled by user")

    try:
        terraform_dir = None
        if session_id == 'current':
            base_dir = Path("/app/terraform_workdir")
            if user_id:
                user_dir = base_dir / user_id
                if user_dir.exists():
                    session_dirs = [
                        d for d in user_dir.iterdir()
                        if d.is_dir() and d.name.startswith('session_') and list(d.glob("*.tf"))
                    ]
                    if session_dirs:
                        terraform_dir = str(max(session_dirs, key=lambda x: x.stat().st_mtime))
        else:
            terraform_dir = get_terraform_directory(user_id, session_id)

        if not terraform_dir or terraform_dir == "terraform_workdir":
            base_dir = Path("/app/terraform_workdir")
            if user_id:
                user_dir = base_dir / user_id
                if user_dir.exists():
                    session_dirs = [d for d in user_dir.iterdir() if d.is_dir() and d.name.startswith('session_')]
                    if session_dirs:
                        terraform_dir = str(max(session_dirs, key=lambda x: x.stat().st_mtime))

        if not terraform_dir or not Path(terraform_dir).exists():
            return build_error_response("No Terraform files found to commit")

        terraform_files = []
        encoded_project = quote(repo, safe="")

        # Resolve target branch (fetch project default if not specified)
        if not branch:
            project_meta = gitlab_api_request("GET", f"/projects/{encoded_project}", user_id)
            target_branch_resolved = project_meta.get("default_branch", "main") if isinstance(project_meta, dict) and "error" not in project_meta else "main"
        else:
            target_branch_resolved = branch

        for tf_file in Path(terraform_dir).glob("*.tf"):
            file_path = f"terraform/{tf_file.name}"
            # Check if file exists to determine create vs update
            file_check = gitlab_api_request("GET", f"/projects/{encoded_project}/repository/files/{quote(file_path, safe='')}", user_id, params={"ref": target_branch_resolved})
            action = "update" if isinstance(file_check, dict) and "error" not in file_check else "create"
            with open(tf_file) as f:
                terraform_files.append({"action": action, "file_path": file_path, "content": f.read()})

        if not terraform_files:
            return build_error_response("No .tf files found in terraform directory")

        # Create a feature branch for the Terraform commit (never push directly to default)
        iac_branch = f"iac/aurora-{session_id or 'manual'}-{int(time.time())}"
        branch_resp = gitlab_api_request("POST", f"/projects/{encoded_project}/repository/branches", user_id,
                                         json_body={"branch": iac_branch, "ref": target_branch_resolved})
        if isinstance(branch_resp, dict) and "error" in branch_resp:
            return build_error_response(f"Failed to create branch: {branch_resp['error']}")

        resp = gitlab_api_request("POST", f"/projects/{encoded_project}/repository/commits", user_id,
                                  json_body={"branch": iac_branch, "commit_message": commit_message, "actions": terraform_files})
        if isinstance(resp, dict) and "error" in resp:
            try:
                gitlab_api_request("DELETE", f"/projects/{encoded_project}/repository/branches/{quote(iac_branch, safe='')}", user_id)
            except Exception:
                logger.warning(
                    "Failed to delete temporary IaC branch after commit failure (project=%s, branch=%s)",
                    encoded_project,
                    iac_branch,
                    exc_info=True,
                )
            return build_error_response(f"GitLab commit failed: {resp['error']}", branch_created=iac_branch)

        # Open a Merge Request for review
        mr_resp = gitlab_api_request("POST", f"/projects/{encoded_project}/merge_requests", user_id,
                                     json_body={"source_branch": iac_branch, "target_branch": target_branch_resolved,
                                                "title": f"IaC: {commit_message}", "description": f"Aurora-generated Terraform commit with {len(terraform_files)} file(s)."})
        if isinstance(mr_resp, dict) and "error" in mr_resp:
            return build_error_response(
                f"Committed files but failed to create MR: {mr_resp['error']}",
                commit_sha=resp.get("id", ""), commit_url=resp.get("web_url", ""),
                branch=iac_branch,
                files_committed=[f["file_path"] for f in terraform_files],
            )
        mr_url = mr_resp.get("web_url", "") if isinstance(mr_resp, dict) else ""

        return build_success_response(
            message=f"Committed {len(terraform_files)} Terraform files and opened MR to {repo}/{target_branch_resolved}",
            commit_sha=resp.get("id", ""), commit_url=resp.get("web_url", ""),
            mr_url=mr_url, branch=iac_branch,
            files_committed=[f["file_path"] for f in terraform_files],
        )
    except Exception as e:
        logger.error("Error in commit_terraform: %s", e, exc_info=True)
        return build_error_response(f"GitLab commit failed: {str(e)}")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def gitlab_tool(
    action: str,
    repo: Optional[str] = None,
    branch: Optional[str] = None,
    incident_time: Optional[str] = None,
    time_window_hours: int = 24,
    commit_sha: Optional[str] = None,
    file_path: Optional[str] = None,
    suggested_content: Optional[str] = None,
    fix_description: Optional[str] = None,
    root_cause_summary: Optional[str] = None,
    commit_message: Optional[str] = None,
    suggestion_id: Optional[int] = None,
    target_branch: Optional[str] = None,
    mr_title: Optional[str] = None,
    mr_description: Optional[str] = None,
    user_id: Optional[str] = None,
    incident_id: Optional[str] = None,
    session_id: Optional[str] = None,
    **kwargs,
) -> str:
    """Unified GitLab tool — dispatches to the appropriate action."""
    logger.info("gitlab_tool called: action=%s, repo=%s", action, repo)

    if not user_id:
        return json.dumps({"status": "error", "error": "User context not available."})

    if not is_gitlab_connected(user_id):
        return json.dumps({"status": "error", "error": "GitLab is not connected. Ask an admin to connect GitLab in Settings > Connectors."})

    # list_projects doesn't need repo resolution
    if action == "list_projects":
        return _action_list_projects(user_id)

    # suggest_fix / apply_fix / commit_terraform have their own resolution
    if action == "suggest_fix":
        return _action_suggest_fix(
            user_id, incident_id, repo, branch,
            file_path, suggested_content, fix_description, root_cause_summary, commit_message,
        )
    if action == "apply_fix":
        use_edited = kwargs.get("use_edited_content", True)
        return _action_apply_fix(user_id, suggestion_id, target_branch, use_edited_content=use_edited)
    if action == "commit_terraform":
        return _action_commit_terraform(user_id, repo, branch, commit_message, session_id)
    if action == "create_branch":
        return _action_create_branch(user_id, repo, branch, target_branch)
    if action == "push_files":
        return _action_push_files(user_id, repo, branch, file_path, suggested_content, commit_message)
    if action == "create_merge_request":
        return _action_create_merge_request(user_id, repo, branch, target_branch, mr_title, mr_description)
    if action == "delete_branch":
        return _action_delete_branch(user_id, repo, branch)

    # RCA actions need repo resolution + time windows
    valid_rca_actions = ["deployment_check", "commits", "diff", "merge_requests"]
    if action not in valid_rca_actions:
        return json.dumps({"status": "error", "error": f"Invalid action '{action}'."})

    project_path, repo_source = _resolve_repository(user_id, repo)
    if not project_path:
        return json.dumps({
            "status": "error", "error": f"No project resolved: {repo_source}",
            "hint": "Pass repo='namespace/project' or connect GitLab in Settings > Connectors.",
        })

    start_time, end_time = _calculate_time_windows(incident_time, time_window_hours)

    try:
        if action == "deployment_check":
            results = _action_deployment_check(project_path, branch, start_time, end_time, user_id)
        elif action == "commits":
            results = _action_commits(project_path, branch, start_time, end_time, user_id)
        elif action == "diff":
            if not commit_sha:
                return json.dumps({"status": "error", "error": "commit_sha is required for 'diff' action."})
            results = _action_diff(project_path, commit_sha, user_id)
        elif action == "merge_requests":
            results = _action_merge_requests(project_path, branch, start_time, end_time, user_id)

        output: Dict[str, Any] = {
            "status": "success" if "error" not in results else "error",
            "action": action, "project": project_path, "project_source": repo_source,
            "time_window": {"start": start_time.strftime('%Y-%m-%dT%H:%M:%SZ'),
                            "end": end_time.strftime('%Y-%m-%dT%H:%M:%SZ'), "hours": time_window_hours},
            "results": results,
        }
        if "error" not in results:
            hints = generate_correlation_hints(action, results)
            if hints:
                output["correlation_hints"] = hints

        return json.dumps(output, indent=2, default=str)

    except Exception as e:
        logger.error(f"Error in gitlab_tool: {e}", exc_info=True)
        return json.dumps({"status": "error", "error": f"GitLab action failed: {str(e)}", "project": project_path})
