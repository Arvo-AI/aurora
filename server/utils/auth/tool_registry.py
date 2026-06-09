"""Centralized registry of gated tools for the org tool permissions system.

Keys match the tool_name values passed to gate_action() after the
mcp_{server}_{tool} / bitbucket:{action} / iac_tool:{action} refactoring.
"""

_TIER_BRANCH_AND_MR = "Branch & MR"

TOOL_REGISTRY = {
    # GitHub MCP — Tier 1: Read & Comment (default ON)
    "mcp_github_create_issue": {"connector": "github", "label": "Create issue", "tier": "Read & Comment", "default": True},
    "mcp_github_add_issue_comment": {"connector": "github", "label": "Comment on issue/PR", "tier": "Read & Comment", "default": True},
    "mcp_github_update_issue": {"connector": "github", "label": "Update issue", "tier": "Read & Comment", "default": True},
    "mcp_github_add_comment_to_pending_review": {"connector": "github", "label": "Add comment to pending review", "tier": "Read & Comment", "default": True},
    "mcp_github_add_project_item": {"connector": "github", "label": "Add item to project", "tier": "Read & Comment", "default": True},
    "mcp_github_update_project_item_field_value": {"connector": "github", "label": "Update project item field", "tier": "Read & Comment", "default": True},
    "mcp_github_assign_copilot_to_issue": {"connector": "github", "label": "Assign Copilot to issue", "tier": "Read & Comment", "default": True},
    "mcp_github_request_copilot_review": {"connector": "github", "label": "Request Copilot review", "tier": "Read & Comment", "default": True},
    "mcp_github_rerun_failed_jobs": {"connector": "github", "label": "Re-run failed jobs", "tier": "Read & Comment", "default": True},
    # GitHub MCP — Tier 2: Branch & PR (default ON)
    "mcp_github_create_branch": {"connector": "github", "label": "Create branch", "tier": _TIER_BRANCH_AND_MR, "default": True},
    "mcp_github_create_pull_request": {"connector": "github", "label": "Create pull request", "tier": _TIER_BRANCH_AND_MR, "default": True},
    "mcp_github_push_files": {"connector": "github", "label": "Push files to branch", "tier": _TIER_BRANCH_AND_MR, "default": True},
    "mcp_github_create_or_update_file": {"connector": "github", "label": "Create or update file", "tier": _TIER_BRANCH_AND_MR, "default": True},
    "mcp_github_update_pull_request_branch": {"connector": "github", "label": "Update PR branch", "tier": _TIER_BRANCH_AND_MR, "default": True},
    "mcp_github_create_pull_request_review": {"connector": "github", "label": "Submit PR review", "tier": _TIER_BRANCH_AND_MR, "default": True},
    "mcp_github_close_pull_request_review": {"connector": "github", "label": "Close PR review", "tier": _TIER_BRANCH_AND_MR, "default": True},
    "mcp_github_manage_pull_request_review": {"connector": "github", "label": "Manage PR review", "tier": _TIER_BRANCH_AND_MR, "default": True},
    "mcp_github_cancel_workflow_run": {"connector": "github", "label": "Cancel CI workflow", "tier": _TIER_BRANCH_AND_MR, "default": True},
    "mcp_github_rerun_workflow_run": {"connector": "github", "label": "Re-run workflow", "tier": _TIER_BRANCH_AND_MR, "default": True},
    "mcp_github_delete_pending_review": {"connector": "github", "label": "Delete pending review", "tier": _TIER_BRANCH_AND_MR, "default": True},
    "mcp_github_fork_repository": {"connector": "github", "label": "Fork repository", "tier": _TIER_BRANCH_AND_MR, "default": True},
    # GitHub MCP — Tier 3: Destructive (default OFF)
    "mcp_github_merge_pull_request": {"connector": "github", "label": "Merge pull request", "tier": "Destructive"},
    "mcp_github_delete_file": {"connector": "github", "label": "Delete file", "tier": "Destructive"},
    "mcp_github_create_repository": {"connector": "github", "label": "Create repository", "tier": "Destructive"},
    # Bitbucket
    "bitbucket:trigger_pipeline": {"connector": "bitbucket", "label": "Trigger pipeline", "tier": "Pipelines"},
    "bitbucket:stop_pipeline": {"connector": "bitbucket", "label": "Stop pipeline", "tier": "Pipelines"},
    "bitbucket:commit_file": {"connector": "bitbucket", "label": "Commit file to branch", "tier": "Code"},
    "bitbucket:delete_file": {"connector": "bitbucket", "label": "Delete file", "tier": "Destructive"},
    "bitbucket:delete_branch": {"connector": "bitbucket", "label": "Delete branch", "tier": "Destructive"},
    "bitbucket:merge_pr": {"connector": "bitbucket", "label": "Merge pull request", "tier": "Destructive"},
    "bitbucket:decline_pr": {"connector": "bitbucket", "label": "Decline pull request", "tier": "Code"},
    # Terraform / IaC
    "iac_tool:apply": {"connector": "terraform", "label": "Apply infrastructure changes", "tier": "Destructive"},
    "iac_tool:destroy": {"connector": "terraform", "label": "Destroy infrastructure", "tier": "Destructive"},
    # Notion
    "notion_update_database_properties": {"connector": "notion", "label": "Delete database columns", "tier": "Destructive"},
    "notion_export_postmortem": {"connector": "notion", "label": "Export postmortem", "tier": "Write", "default": True},
    # Spinnaker
    "spinnaker_rca": {"connector": "spinnaker", "label": "Trigger deployment pipeline", "tier": "Destructive"},
    # GitLab — Tier 1: Suggest (default ON)
    "gitlab:suggest_fix": {"connector": "gitlab", "label": "Suggest code fix", "tier": "Suggest", "default": True},
    # GitLab — Tier 2: Branch & MR (default ON)
    "gitlab:create_branch": {"connector": "gitlab", "label": "Create branch", "tier": _TIER_BRANCH_AND_MR, "default": True},
    "gitlab:push_files": {"connector": "gitlab", "label": "Push file changes to branch", "tier": _TIER_BRANCH_AND_MR, "default": True},
    "gitlab:create_merge_request": {"connector": "gitlab", "label": "Create merge request", "tier": _TIER_BRANCH_AND_MR, "default": True},
    # GitLab — Tier 3: IaC (default ON)
    "gitlab:commit_terraform": {"connector": "gitlab", "label": "Commit Terraform files & open MR", "tier": "IaC", "default": True},
    # GitLab — Tier 4: Destructive (default OFF)
    "gitlab:delete_branch": {"connector": "gitlab", "label": "Delete branch", "tier": "Destructive"},
}


def get_default_enabled_tools() -> set:
    """Return tool_keys that should be enabled by default on first seed."""
    return {k for k, v in TOOL_REGISTRY.items() if v.get("default")}


def seed_org_tool_permissions(org_id: str, user_id: str) -> int:
    """Seed default tool permissions for org. Idempotent (DO NOTHING on conflict)."""
    from datetime import datetime, timezone
    from utils.db.connection_pool import db_pool

    defaults = get_default_enabled_tools()
    now = datetime.now(timezone.utc)

    with db_pool.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SET myapp.current_user_id = %s;", (user_id,))
            cur.execute("SET myapp.current_org_id = %s;", (org_id,))
            conn.commit()
            for tool_key in TOOL_REGISTRY:
                enabled = tool_key in defaults
                cur.execute(
                    """INSERT INTO org_tool_permissions (org_id, tool_key, enabled, updated_by, updated_at)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (org_id, tool_key) DO NOTHING""",
                    (org_id, tool_key, enabled, user_id, now),
                )
            conn.commit()
    return len(TOOL_REGISTRY)


def get_tools_by_connector() -> dict:
    """Group registry entries by connector for UI rendering."""
    grouped: dict = {}
    for key, meta in TOOL_REGISTRY.items():
        connector = meta["connector"]
        if connector not in grouped:
            grouped[connector] = []
        grouped[connector].append({"tool_key": key, **meta})
    return grouped
