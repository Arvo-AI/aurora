---
name: gitlab
id: gitlab
description: "GitLab code repository integration for investigating code changes, deployments, commits, MRs, and suggesting fixes during RCA"
category: code_repository
connection_check:
  method: get_credentials_from_db
  provider_key: gitlab
  required_field: base_url
tools:
  - gitlab
index: "Code repo — discover projects, check pipelines/commits/MRs, suggest & apply fixes (GitLab)"
rca_priority: 3
allowed-tools: gitlab
metadata:
  author: aurora
  version: "2.0"
---

# GitLab Integration

## Overview
GitLab integration for investigating code changes during Root Cause Analysis and managing code fixes.
Connected via org-level Group Access Token. Instance: {base_url}

## Instructions

### Single Tool — Multiple Actions
All GitLab operations use the `gitlab` tool with an `action` parameter:

| Action | Purpose | Required Params |
|--------|---------|-----------------|
| `list_projects` | Discover connected projects | — |
| `deployment_check` | CI/CD pipelines | repo (or auto) |
| `commits` | Recent commits + correlation | repo (or auto) |
| `diff` | File changes for a commit | commit_sha |
| `merge_requests` | Merged MRs in time window | repo (or auto) |
| `suggest_fix` | Propose a code fix | file_path, suggested_content, fix_description, root_cause_summary |
| `apply_fix` | Create MR from suggestion | suggestion_id |
| `commit_terraform` | Push Terraform files | repo, commit_message |

### RCA Investigation Workflow
Code changes are the most common root cause of incidents.
Investigate GitLab BEFORE deep-diving into infrastructure.

**Step 1 — Discover projects:**
`gitlab(action='list_projects')` — returns all connected projects.

**Step 2 — Check pipelines (did something just ship?):**
`gitlab(action='deployment_check', repo='namespace/project', incident_time='<ISO8601>')`
Finds failed pipelines and pipelines completed within 2 hours of the incident.

**Step 3 — Check commits (what code changed?):**
`gitlab(action='commits', repo='namespace/project', incident_time='<ISO8601>')`
Lists commits with automatic suspicious-commit flagging.

**Step 4 — Inspect suspicious changes:**
`gitlab(action='diff', repo='namespace/project', commit_sha='<sha>')`
Shows file-level additions/deletions. Prioritize config/infra files.

**Step 5 — Check merged MRs:**
`gitlab(action='merge_requests', repo='namespace/project', incident_time='<ISO8601>')`
Finds MRs merged in the time window; recently merged MRs are flagged.

**Step 6 — Suggest fix:**
`gitlab(action='suggest_fix', file_path=..., suggested_content=..., fix_description=..., root_cause_summary=...)`
Suggests a fix stored for user review. User can approve, then use `action='apply_fix'`.

### Important Rules
- Pass `incident_time` on every RCA call for automatic time correlation.
- Use `time_window_hours` (default 24) to widen/narrow the search.
- If only one project connected, `repo` auto-resolves. If multiple, pass `repo=` explicitly.
- Projects are REMOTE — use the gitlab tool to read, never local shell commands.
- Look for: config changes, k8s manifests, Terraform, dependency updates.
