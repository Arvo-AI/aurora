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
  feature_flag: is_jira_enabled
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
Jira integration for searching recent development context during Root Cause Analysis and tracking incidents afterward. Jira is a **mandatory first step** in any RCA investigation -- search here BEFORE infrastructure or CI/CD tools.

Jira operates in one of two modes based on user preference (`jira_mode`):
- `comment_only` (default): Only `jira_search_issues`, `jira_get_issue`, and `jira_add_comment` are available.
- `full`: All six tools are available including create, update, and link.

## Instructions

### MANDATORY FIRST STEP -- CHANGE CONTEXT & KNOWLEDGE BASE

**You MUST call Jira tools BEFORE any infrastructure or CI/CD investigation.**
Skipping this step is a failure of the investigation.

Your FIRST tool calls MUST be `jira_search_issues`.

### Tools

**Investigation tools (always available):**
- `jira_search_issues(jql='...')` -- Search Jira issues using JQL. Returns matching issues with key, summary, status, assignee, labels.
- `jira_get_issue(issue_key='PROJ-123')` -- Get full details of a Jira issue by key. Returns description, status, comments, linked PRs.
- `jira_add_comment(issue_key='PROJ-123', comment='...')` -- Add a comment to a Jira issue. Non-destructive operation.

**Write tools (full mode only):**
- `jira_create_issue(project_key='PROJ', summary='...', description='...', issue_type='Bug')` -- Create a new Jira issue in a project.
- `jira_update_issue(issue_key='PROJ-123', ...)` -- Update fields on an existing Jira issue.
- `jira_link_issues(inward_issue='PROJ-123', outward_issue='PROJ-456', link_type='Relates')` -- Create a link between two Jira issues (Relates, Blocks, Clones, etc.).

### RCA Investigation Flow

#### Step 1 -- Find related recent work (DO THIS IMMEDIATELY)
- `jira_search_issues(jql='text ~ "SERVICE" AND updated >= -7d ORDER BY updated DESC')` -- Recent tickets for this service
- `jira_search_issues(jql='type in (Bug, Incident) AND status != Done AND updated >= -14d ORDER BY updated DESC')` -- Open bugs/incidents
- `jira_search_issues(jql='type in (Story, Task) AND status = Done AND updated >= -3d ORDER BY updated DESC')` -- Recently completed work (likely deployed)

#### Step 2 -- For each relevant ticket, check details
- `jira_get_issue(issue_key='PROJ-123')` -- Read the description, linked PRs, comments for context on what changed

#### What to look for
- Recently completed stories/tasks -- code that was just deployed
- Open bugs with similar symptoms -- known issues
- Config change tickets -- infrastructure or config drift
- Linked PRs/commits -- exact code changes to correlate with the failure

#### Step 3 -- Use Jira findings to NARROW infrastructure investigation
If a ticket mentions a DB migration, focus on DB connectivity. If a ticket mentions a config change, check configs first.

### Post-Investigation (comment_only mode)
- `jira_add_comment(issue_key='PROJ-123', comment='update')` -- Add findings to existing issue
- After adding a comment, the tool returns a `url` field. Always share this link with the user as a markdown link so they can click through to Jira.
- Write comments as short, clean plain text. No markdown syntax. Structure: Title, Root Cause, Impact, Evidence, Remediation. Under 15 lines.
- NOTE: In comment_only mode, do NOT create new issues or link issues.

### Post-Investigation (full mode)
- `jira_create_issue(project_key='PROJ', summary='title', description='details', issue_type='Bug')` -- Create incident tracking issue
- `jira_add_comment(issue_key='PROJ-123', comment='update')` -- Add findings to existing issue
- After adding a comment or creating an issue, the tool returns a `url` field. Always share this link with the user as a markdown link so they can click through to Jira.
- Write comments as short, clean plain text. No markdown syntax. Structure: Title, Root Cause, Impact, Evidence, Remediation. Under 15 lines.

### Important Rules
- **CRITICAL: During the investigation phase, ONLY use jira_search_issues and jira_get_issue.**
- Do NOT use jira_create_issue, jira_add_comment, jira_update_issue, or jira_link_issues during investigation.
- Jira filing happens automatically in a separate step after your investigation completes.
- After Jira context, proceed to infrastructure/CI tools.

## RCA Investigation (Mandatory First Step)
**You MUST call Jira tools BEFORE any infrastructure investigation.**

### Step 1 -- Find related recent work:
- `jira_search_issues(jql='text ~ "{service_name}" AND updated >= -7d ORDER BY updated DESC')` -- Recent tickets
- `jira_search_issues(jql='type in (Bug, Incident) AND status != Done AND updated >= -14d ORDER BY updated DESC')` -- Open bugs
- `jira_search_issues(jql='type in (Story, Task) AND status = Done AND updated >= -3d ORDER BY updated DESC')` -- Recently completed

### Step 2 -- Check details:
- `jira_get_issue(issue_key='PROJ-123')` -- Description, linked PRs, comments

### What to look for:
- Recently completed stories --> code just deployed
- Open bugs with similar symptoms --> known issues
- Config change tickets --> infrastructure drift
- Linked PRs --> exact code changes

Use findings to NARROW infrastructure investigation.

**CRITICAL: During investigation, ONLY use jira_search_issues and jira_get_issue.**
Jira filing happens automatically after investigation completes.
