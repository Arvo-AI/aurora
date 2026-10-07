---
name: jira
id: jira
description: "Jira integration for searching recent development context, tracking incidents, and managing issues during RCA"
category: knowledge
connection_check:
  method: get_token_data
  provider_key: jira
  required_any_fields:
    - access_token
    - pat_token
tools:
  - jira_search_issues
  - jira_get_issue
  - jira_add_comment
  - jira_create_issue
  - jira_update_issue
  - jira_link_issues
index: "Knowledge -- search Jira for recent changes, open bugs, incidents; create/comment on issues"
rca_priority: 1
allowed-tools: jira_search_issues, jira_get_issue, jira_add_comment, jira_create_issue, jira_update_issue, jira_link_issues
metadata:
  author: aurora
  version: "1.0"
---

# Jira Integration

## Overview
Jira integration for searching recent development context during Root Cause Analysis. Jira is a **mandatory first step** in any RCA investigation — search here BEFORE infrastructure or CI/CD tools.

**Which tools you have:** Only call Jira tools that appear in your bound tool list. During RCA investigation you normally have `jira_search_issues` and `jira_get_issue` only. Write tools (`jira_add_comment`, `jira_create_issue`, …) are bound only when the org turned on **Comment back on Jira tickets** or the user explicitly asked you to write in chat. If a write tool is not in your list, do not attempt it — unattended RCA posts to Jira in a separate step when comment-back is enabled.

When write tools are bound, `jira_mode` controls scope: **comment_only** (comment on existing issues) or **full** (also create, update, and link).

## Instructions

### MANDATORY FIRST STEP -- CHANGE CONTEXT & KNOWLEDGE BASE

**You MUST call Jira tools BEFORE any infrastructure or CI/CD investigation.**
Skipping this step is a failure of the investigation.

Your FIRST tool calls MUST be `jira_search_issues`.

### Tools

**Investigation (always when Jira is connected):**
- `jira_search_issues(jql='...')` — Search Jira issues using JQL.
- `jira_get_issue(issue_key='PROJ-123')` — Full issue details.

**Write (only if bound in your tool list):**
- `jira_add_comment(issue_key='PROJ-123', comment='...')`
- `jira_create_issue(...)` — **full** mode only
- `jira_update_issue(...)` — **full** mode only
- `jira_link_issues(...)` — **full** mode only

### RCA Investigation Flow

#### Step 1 — Find related recent work (DO THIS IMMEDIATELY)
- `jira_search_issues(jql='text ~ "SERVICE" AND updated >= -7d ORDER BY updated DESC')`
- `jira_search_issues(jql='type in (Bug, Incident) AND status != Done AND updated >= -14d ORDER BY updated DESC')`
- `jira_search_issues(jql='type in (Story, Task) AND status = Done AND updated >= -3d ORDER BY updated DESC')`

#### Step 2 — For each relevant ticket, check details
- `jira_get_issue(issue_key='PROJ-123')`

#### What to look for
- Recently completed stories/tasks — code that was just deployed
- Open bugs with similar symptoms — known issues
- Config change tickets — infrastructure or config drift
- Linked PRs/commits — exact code changes to correlate with the failure

#### Step 3 — Use Jira findings to NARROW infrastructure investigation
If a ticket mentions a DB migration, focus on DB connectivity. If a ticket mentions a config change, check configs first.

### Important Rules
- **CRITICAL: During the investigation phase, ONLY use `jira_search_issues` and `jira_get_issue`.**
- Do NOT call write tools during investigation unless the user explicitly asked you to in this chat.
- Do not file during the investigation. A later automated step posts to Jira only if the org turned on comment-back.
- After Jira context, proceed to infrastructure/CI tools.

## RCA Investigation (Mandatory First Step)
**You MUST call Jira tools BEFORE any infrastructure investigation.**

### Step 1 — Find related recent work:
- `jira_search_issues(jql='text ~ "{escaped_service}" AND updated >= -7d ORDER BY updated DESC')`
- `jira_search_issues(jql='type in (Bug, Incident) AND status != Done AND updated >= -14d ORDER BY updated DESC')`
- `jira_search_issues(jql='type in (Story, Task) AND status = Done AND updated >= -3d ORDER BY updated DESC')`

### Step 2 — Check details:
- `jira_get_issue(issue_key='PROJ-123')`

**CRITICAL: During investigation, ONLY use `jira_search_issues` and `jira_get_issue`.**
Do not file during the investigation. A later step posts to Jira only if this org turned on comment-back.
