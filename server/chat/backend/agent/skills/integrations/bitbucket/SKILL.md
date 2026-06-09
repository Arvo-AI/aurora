---
name: bitbucket
id: bitbucket
description: "Bitbucket code repository integration for managing repos, branches, PRs, issues, and CI/CD pipelines"
category: code_repository
connection_check:
  method: get_credentials_from_db
  provider_key: bitbucket
  required_field: access_token
tools:
  - bitbucket_repos
  - bitbucket_branches
  - bitbucket_pull_requests
  - bitbucket_issues
  - bitbucket_pipelines
  - bitbucket_fix
index: "Code repo -- manage Bitbucket repos, branches, PRs, issues, CI/CD pipelines, and suggest code fixes (6 tools, 42 actions)"
rca_priority: 2
allowed-tools: bitbucket_repos, bitbucket_branches, bitbucket_pull_requests, bitbucket_issues, bitbucket_pipelines, bitbucket_fix
metadata:
  author: aurora
  version: "1.1"
---

# Bitbucket Integration

## Overview
Bitbucket code repository integration for managing repositories, branches, pull requests, issues, and CI/CD pipelines.
Connected account: {display_name}
Use `bitbucket_repos(action='list_repos', workspace='WS')` or `bitbucket_repos(action='list_workspaces')` to discover connected repos.
Workspace and repository auto-resolve from saved user selection if not passed explicitly.

## Instructions

### Tools (6 tools, 42 actions)

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

**bitbucket_fix** -- Suggest Code Fixes During RCA:
- Use when you identify a specific code change that would fix the root cause
- Accepts anchored search-and-replace edits (old_string → new_string)
- Fetches the current file, applies edits, and saves the suggestion for user review
- The user can then review, edit, and create a PR from the Incidents UI
- Parameters: `file_path`, `edits` (list of {old_string, new_string, replace_all}), `fix_description`, `root_cause_summary`
- Optional: `repo` (workspace/repo_slug), `commit_message`, `branch`

### RCA Investigation Flow

1. List connected repos in the workspace:
   `bitbucket_repos(action='list_repos', workspace='WS')`
2. Check recent commits for changes that may correlate with the alert:
   `bitbucket_branches(action='list_commits', workspace='WS', repo_slug='REPO')`
3. Check recent PRs for merged changes:
   `bitbucket_pull_requests(action='list_prs', workspace='WS', repo_slug='REPO', state='MERGED')`
4. Check pipeline runs for deployment failures:
   `bitbucket_pipelines(action='list_pipelines', workspace='WS', repo_slug='REPO')`
5. Get step-level logs for failed pipelines:
   `bitbucket_pipelines(action='get_step_log', workspace='WS', repo_slug='REPO', pipeline_uuid='UUID', step_uuid='UUID')`
6. Inspect diffs for suspicious commits:
   `bitbucket_branches(action='get_diff', workspace='WS', repo_slug='REPO', spec='COMMIT_SHA')`
7. If a root cause code change is identified, propose a fix:
   `bitbucket_fix(file_path='path/to/file', edits=[{old_string: '...', new_string: '...'}], fix_description='...', root_cause_summary='...')`

### Tool Usage Rules
- `list_repos` and `list_workspaces` return only the repos/workspaces the user has connected — not everything in their Bitbucket account.
- When user asks about PRs, issues, repos, or branches WITHOUT specifying a repository, use the selected workspace/repo from context.
- Workspace and `repo_slug` auto-resolve from saved selection if not passed explicitly.
- **During background RCA**: tools are READ-ONLY. Do NOT manually create branches, commit files, or create PRs. Use `bitbucket_fix` to propose code changes — it saves suggestions for user review.
- Destructive actions (delete branch, delete file, merge PR, decline PR, trigger/stop pipeline) require user confirmation and will prompt automatically.
- Non-destructive operations (create branch, create PR, update PR, approve, comment, create issue) proceed without extra confirmation.
- `bitbucket_fix` does NOT modify the repo directly — it saves the suggestion for user review. No confirmation needed.
- If no repository is selected and user doesn't specify one, ask which repository they want to work with.

### Important Rules
- Look for: config changes, k8s manifests, Terraform, dependency updates.
- Check pipeline logs when builds fail near the incident time.
- Cross-reference commit history with deployment timing.
- When you identify the problematic code change, use `bitbucket_fix` to propose a revert or correction.
- **NEVER** manually create a branch + commit file + create PR. Always use `bitbucket_fix` instead — the user will create the PR from the Incidents UI after reviewing your suggestion.
