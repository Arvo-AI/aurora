---
name: github
id: github
description: "GitHub code repository integration for investigating code changes, deployments, commits, PRs, and suggesting fixes during RCA"
category: code_repository
connection_check:
  method: get_credentials_from_db
  provider_key: github
  required_field: username
tools:
  - get_connected_repos
  - github_rca
  - github_fix
  - github_apply_fix
  - github_commit
index: "Code repo — discover repos, check deployments/commits/PRs, suggest & apply fixes"
rca_priority: 2
allowed-tools: get_connected_repos, github_rca, github_fix, github_apply_fix, github_commit
metadata:
  author: aurora
  version: "1.0"
---

# GitHub Integration

## Overview
GitHub integration for investigating code changes during Root Cause Analysis and managing code fixes.
Connected account: {username}

## Instructions

### Multi-Repo Discovery
- Multiple repositories may be connected. Call `get_connected_repos` FIRST to list them with descriptions.
- Each repo has an LLM-generated summary describing what it contains — use these to pick the right repo for your task.
- If only one repo is connected, `github_rca` auto-selects it. If multiple, you MUST pass `repo='owner/repo'`.

### Tool Usage (use in this order)
1. `get_connected_repos` — Discover available repos + descriptions. Always call first.
2. `github_rca(repo='owner/repo', action=...)` — Investigate code changes for RCA:
   - `deployment_check` — GitHub Actions workflow runs (failures, suspicious timing)
   - `commits` — Recent commits with automatic 2-hour incident correlation
   - `diff` (requires `commit_sha`) — File-level changes for a specific commit
   - `pull_requests` — Merged PRs in the time window
   - Pass `incident_time` (ISO 8601) for automatic time window correlation
3. `github_fix(file_path=..., suggested_content=..., fix_description=..., root_cause_summary=...)` — Suggest a code fix (stored for user review, not auto-applied)
4. `github_apply_fix(suggestion_id=...)` — Create a PR from an approved fix (only after user reviews)
5. `github_commit(repo=..., commit_message=...)` — Push Terraform files to GitHub

### MCP Tools (for direct GitHub API operations beyond RCA)
- Files: `get_file_contents`, `create_or_update_file`, `push_files`, `get_repository_tree`
- Branches: `create_branch`, `list_branches`, `list_commits`, `get_commit`
- PRs: `create_pull_request`, `list_pull_requests`, `merge_pull_request`, `get_pull_request_files`
- Issues: `create_issue`, `list_issues`, `search_issues`, `add_issue_comment`
- Actions: `list_workflow_runs`, `get_workflow_run`, `get_job_logs`, `run_workflow`
- Security: `list_code_scanning_alerts`, `list_dependabot_alerts`, `list_secret_scanning_alerts`
- All MCP tools require `owner` and `repo` parameters (split from 'owner/repo').

### RCA Investigation Workflow
Code changes are the most common root cause of incidents.
Investigate GitHub BEFORE deep-diving into infrastructure.

**Step 1 — Discover repos:**
`get_connected_repos()` — returns all connected repos with descriptions.
Read the descriptions to pick the repo most relevant to the alert.

**Step 2 — Check deployments (did something just ship?):**
`github_rca(repo='owner/repo', action='deployment_check', incident_time='<ISO8601>')`
Finds failed workflow runs and runs completed within 2 hours of the incident.

**Step 3 — Check commits (what code changed?):**
`github_rca(repo='owner/repo', action='commits', incident_time='<ISO8601>')`
Lists commits with automatic suspicious-commit flagging (within 2 hrs of incident).

**Step 4 — Inspect suspicious changes:**
`github_rca(repo='owner/repo', action='diff', commit_sha='<sha>')`
Shows file-level additions/deletions. Prioritize config/infra files (.yaml, .env, terraform/).

**Step 5 — Check merged PRs:**
`github_rca(repo='owner/repo', action='pull_requests', incident_time='<ISO8601>')`
Finds PRs merged in the time window; recently merged PRs are flagged.

**Step 6 — Suggest fix:**
`github_fix(file_path=..., suggested_content=..., fix_description=..., root_cause_summary=...)`
Suggests a fix stored for user review. User can approve, then `github_apply_fix` creates a PR.

### Important Rules
- Pass `incident_time` on every github_rca call for automatic time correlation.
- Use `time_window_hours` (default 24) to widen/narrow the search.
- Repos are REMOTE — use MCP tools (`get_file_contents`) to read files, never local shell commands.
- Look for: config changes, k8s manifests, Terraform, dependency updates.
