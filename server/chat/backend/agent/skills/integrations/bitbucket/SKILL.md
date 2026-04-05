---
name: bitbucket
id: bitbucket
description: "Bitbucket code repository integration for managing repos, branches, PRs, issues, and CI/CD pipelines"
category: code_repository
connection_check:
  method: is_connected_function
  module: chat.backend.agent.tools.bitbucket.utils
  function: is_bitbucket_connected
tools:
  - bitbucket_repos
  - bitbucket_branches
  - bitbucket_pull_requests
  - bitbucket_issues
  - bitbucket_pipelines
index: "Code repo -- manage Bitbucket repos, branches, PRs, issues, and CI/CD pipelines (5 tools, 41 actions)"
rca_priority: 2
allowed-tools: bitbucket_repos, bitbucket_branches, bitbucket_pull_requests, bitbucket_issues, bitbucket_pipelines
metadata:
  author: aurora
  version: "1.0"
---

# Bitbucket Integration

## Overview
Bitbucket code repository integration for managing repositories, branches, pull requests, issues, and CI/CD pipelines.
Connected account: {display_name}
Selected workspace: {workspace_slug} | Selected repository: {repo_slug} | Selected branch: {branch_name}

## Instructions

### Tools (5 tools, 41 actions)

**bitbucket_repos** -- Repository, File & Code Operations:
- `list_repos`, `get_repo`, `get_file_contents`, `create_or_update_file`, `delete_file`
- `get_directory_tree`, `search_code`, `list_workspaces`, `get_workspace`

**bitbucket_branches** -- Branch & Commit Operations:
- `list_branches`, `create_branch`, `delete_branch`, `list_commits`, `get_commit`, `get_diff`, `compare`

**bitbucket_pull_requests** -- Pull Request Operations:
- `list_prs`, `get_pr`, `create_pr`, `update_pr`, `merge_pr`, `approve_pr`, `unapprove_pr`, `decline_pr`
- `list_pr_comments`, `add_pr_comment`, `get_pr_diff`, `get_pr_activity`

**bitbucket_issues** -- Issue Operations:
- `list_issues`, `get_issue`, `create_issue`, `update_issue`, `list_issue_comments`, `add_issue_comment`

**bitbucket_pipelines** -- CI/CD Pipeline Operations:
- `list_pipelines`, `get_pipeline`, `trigger_pipeline`, `stop_pipeline`
- `list_pipeline_steps`, `get_step_log`, `get_pipeline_step`

### RCA Investigation Flow

1. Check recent commits for changes that may correlate with the alert:
   `bitbucket_branches(action='list_commits', workspace='WS', repo_slug='REPO')`
2. Check recent PRs for merged changes:
   `bitbucket_pull_requests(action='list_prs', workspace='WS', repo_slug='REPO', state='MERGED')`
3. Check pipeline runs for deployment failures:
   `bitbucket_pipelines(action='list_pipelines', workspace='WS', repo_slug='REPO')`
4. Get step-level logs for failed pipelines:
   `bitbucket_pipelines(action='get_step_log', workspace='WS', repo_slug='REPO', pipeline_uuid='UUID', step_uuid='UUID')`
5. Inspect diffs for suspicious commits:
   `bitbucket_branches(action='get_diff', workspace='WS', repo_slug='REPO', spec='COMMIT_SHA')`

### Tool Usage Rules
- When user asks about PRs, issues, repos, or branches WITHOUT specifying a repository, use the selected workspace/repo from context.
- Workspace and `repo_slug` auto-resolve from saved selection if not passed explicitly.
- Destructive actions (delete branch, delete file, merge PR, decline PR, trigger/stop pipeline) require user confirmation and will prompt automatically.
- Non-destructive operations (create branch, create PR, update PR, approve, comment, create issue) proceed without extra confirmation.
- If no repository is selected and user doesn't specify one, ask which repository they want to work with.

### Important Rules
- Look for: config changes, k8s manifests, Terraform, dependency updates.
- Check pipeline logs when builds fail near the incident time.
- Cross-reference commit history with deployment timing.
