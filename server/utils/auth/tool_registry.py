"""Centralized registry of gated tools for the org tool permissions system.

Keys match the tool_name values passed to gate_action() after the
mcp_{server}_{tool} / bitbucket:{action} / iac_tool:{action} refactoring.
"""

TOOL_REGISTRY = {
    # GitHub MCP
    "mcp_github_push_files": {"connector": "github", "label": "Push files to branch", "risk": "medium", "default": True},
    "mcp_github_create_branch": {"connector": "github", "label": "Create branch", "risk": "low", "default": True},
    "mcp_github_create_pull_request": {"connector": "github", "label": "Create pull request", "risk": "low", "default": True},
    "mcp_github_create_or_update_file": {"connector": "github", "label": "Create or update file", "risk": "medium", "default": True},
    "mcp_github_create_repository": {"connector": "github", "label": "Create repository", "risk": "high"},
    "mcp_github_create_issue": {"connector": "github", "label": "Create issue", "risk": "low", "default": True},
    "mcp_github_create_pull_request_review": {"connector": "github", "label": "Submit PR review", "risk": "medium"},
    "mcp_github_merge_pull_request": {"connector": "github", "label": "Merge pull request", "risk": "high"},
    "mcp_github_update_pull_request_branch": {"connector": "github", "label": "Update PR branch", "risk": "low", "default": True},
    "mcp_github_fork_repository": {"connector": "github", "label": "Fork repository", "risk": "medium"},
    "mcp_github_add_issue_comment": {"connector": "github", "label": "Comment on issue/PR", "risk": "low", "default": True},
    "mcp_github_delete_file": {"connector": "github", "label": "Delete file", "risk": "high"},
    "mcp_github_cancel_workflow_run": {"connector": "github", "label": "Cancel CI workflow", "risk": "medium"},
    "mcp_github_rerun_workflow": {"connector": "github", "label": "Re-run workflow", "risk": "medium"},
    "mcp_github_rerun_workflow_failed_jobs": {"connector": "github", "label": "Re-run failed jobs", "risk": "low"},
    "mcp_github_update_issue": {"connector": "github", "label": "Update issue", "risk": "low", "default": True},
    # Bitbucket
    "bitbucket:trigger_pipeline": {"connector": "bitbucket", "label": "Trigger pipeline", "risk": "medium"},
    "bitbucket:stop_pipeline": {"connector": "bitbucket", "label": "Stop pipeline", "risk": "medium"},
    "bitbucket:commit_file": {"connector": "bitbucket", "label": "Commit file to branch", "risk": "medium"},
    "bitbucket:delete_file": {"connector": "bitbucket", "label": "Delete file", "risk": "high"},
    "bitbucket:delete_branch": {"connector": "bitbucket", "label": "Delete branch", "risk": "high"},
    "bitbucket:merge_pr": {"connector": "bitbucket", "label": "Merge pull request", "risk": "high"},
    "bitbucket:decline_pr": {"connector": "bitbucket", "label": "Decline pull request", "risk": "medium"},
    # Terraform / IaC
    "iac_tool:apply": {"connector": "terraform", "label": "Apply infrastructure changes", "risk": "high"},
    "iac_tool:destroy": {"connector": "terraform", "label": "Destroy infrastructure", "risk": "critical"},
    # Notion
    "notion_update_database_properties": {"connector": "notion", "label": "Delete database columns", "risk": "high"},
    "notion_export_postmortem": {"connector": "notion", "label": "Export postmortem", "risk": "low", "default": True},
    # Spinnaker
    "spinnaker_rca": {"connector": "spinnaker", "label": "Trigger deployment pipeline", "risk": "high"},
}


def get_default_enabled_tools() -> set:
    """Return tool_keys that should be enabled by default on first seed."""
    return {k for k, v in TOOL_REGISTRY.items() if v.get("default")}


def get_tools_by_connector() -> dict:
    """Group registry entries by connector for UI rendering."""
    grouped: dict = {}
    for key, meta in TOOL_REGISTRY.items():
        connector = meta["connector"]
        if connector not in grouped:
            grouped[connector] = []
        grouped[connector].append({"tool_key": key, **meta})
    return grouped
