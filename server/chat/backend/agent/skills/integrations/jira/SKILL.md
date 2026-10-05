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
  version: "2.0"
---

# Jira Integration

## Overview
Jira is a **context source** for Root Cause Analysis: search it for recent changes, open bugs and past incidents BEFORE infrastructure or CI/CD tools.

Whether Aurora may also *write* to Jira is an org setting (`jira_mode`), and it is **off by default**:
- `read_only` (default): search and read only. `jira_search_issues` and `jira_get_issue`.
- `comment_only`: may also comment on issues that already exist.
- `full`: may also create, update and link issues.

**This org is in `{jira_mode}` mode.** Tools outside that mode are not registered; calling one fails.

### Why writes are off by default
Atlassian OAuth has no bot identity. Anything Aurora posts is authored by the Atlassian account that connected the integration, so it shows up under a real person's name. Every write therefore carries an Aurora attribution banner, and writes stay disabled until an admin turns them on.

## Instructions

### MANDATORY FIRST STEP -- CHANGE CONTEXT & KNOWLEDGE BASE

**You MUST call Jira tools BEFORE any infrastructure or CI/CD investigation.**
Skipping this step is a failure of the investigation.

Your FIRST tool calls MUST be `jira_search_issues`.

### Tools

**Investigation tools (always available):**
- `jira_search_issues(jql='...')` -- Search Jira issues using JQL. Returns matching issues with key, summary, status, assignee, labels.
- `jira_get_issue(issue_key='PROJ-123')` -- Get full details of a Jira issue by key. Returns description, status, comments, linked PRs.

**Write tools (only when `jira_mode` allows):**
- `jira_add_comment(issue_key='PROJ-123', comment='...')` -- `comment_only` or `full`.
- `jira_create_issue(project_key='PROJ', summary='...', description='...', issue_type='Bug')` -- `full` only.
- `jira_update_issue(issue_key='PROJ-123', ...)` -- `full` only.
- `jira_link_issues(inward_issue='PROJ-123', outward_issue='PROJ-456', link_type='Relates')` -- `full` only.

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

### Post-Investigation (read_only mode)
- Do NOT attempt any Jira write. Put the findings in your answer and cite the Jira issues you read as markdown links.

### Post-Investigation (comment_only mode)
- `jira_add_comment(issue_key='PROJ-123', comment='update')` -- Add findings to an existing issue.
- Do NOT create new issues or link issues.
- After commenting, the tool returns a `url` field. Share it as a markdown link.
- Write comments as short, clean plain text. No markdown syntax. Structure: Title, Root Cause, Impact, Evidence, Remediation. Under 15 lines.

### Post-Investigation (full mode)
- `jira_create_issue(project_key='PROJ', summary='title', description='details', issue_type='Bug')` -- Create incident tracking issue.
- `jira_add_comment(issue_key='PROJ-123', comment='update')` -- Add findings to an existing issue.
- Prefer commenting on a matching issue over creating a new one.
- After commenting or creating, the tool returns a `url` field. Share it as a markdown link.
- Write comments as short, clean plain text. No markdown syntax. Structure: Title, Root Cause, Impact, Evidence, Remediation. Under 15 lines.

### Important Rules
- **CRITICAL: During the investigation phase, ONLY use jira_search_issues and jira_get_issue.**
- Do NOT use jira_create_issue, jira_add_comment, jira_update_issue, or jira_link_issues during investigation.
- Jira filing, when the org has enabled it, happens automatically in a separate step after your investigation completes.
- If a write tool returns a permission error, do NOT retry it — the org has writes disabled. Report the finding in your answer instead.
- After Jira context, proceed to infrastructure/CI tools.

## RCA Investigation (Mandatory First Step)
**You MUST call Jira tools BEFORE any infrastructure investigation.**

### Step 1 -- Find related recent work:
- `jira_search_issues(jql='text ~ "{escaped_service}" AND updated >= -7d ORDER BY updated DESC')` -- Recent tickets
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
Jira filing happens after investigation completes, and only if this org enabled Jira writes.
