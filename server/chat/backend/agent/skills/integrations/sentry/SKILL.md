# Sentry — Application Error Tracking & Performance Monitoring

## Metadata
- connection_check: has_user_credentials(user_id, "sentry")
- rca_priority: 4
- tools: cloud_exec (provider="sentry")

## Overview
Sentry tracks application errors, exceptions, and performance issues. Use the Sentry CLI via `cloud_exec` with `provider="sentry"` to investigate issues, view stack traces, get AI-powered root cause analysis, and explore error data.

## CLI Commands (via cloud_exec)

### Investigating Issues
```
cloud_exec(provider="sentry", command="issue list")
cloud_exec(provider="sentry", command="issue list --project my-project --query is:unresolved")
cloud_exec(provider="sentry", command="issue view ISSUE-ID")
cloud_exec(provider="sentry", command="issue events ISSUE-ID")
```

### AI-Powered Analysis
```
cloud_exec(provider="sentry", command="issue explain ISSUE-ID")
cloud_exec(provider="sentry", command="issue plan ISSUE-ID")
```

### Events & Data Exploration
```
cloud_exec(provider="sentry", command="event list ISSUE-ID")
cloud_exec(provider="sentry", command="event view EVENT-ID")
cloud_exec(provider="sentry", command="explore errors --fields title,count() --sort -count()")
```

### Projects & Organization
```
cloud_exec(provider="sentry", command="project list")
cloud_exec(provider="sentry", command="org list")
```

### Arbitrary API Calls
```
cloud_exec(provider="sentry", command="api /organizations/{org}/alert-rules/")
cloud_exec(provider="sentry", command="api /projects/{org}/{project}/releases/")
```

### Schema Discovery
```
cloud_exec(provider="sentry", command="schema")
cloud_exec(provider="sentry", command="schema issues")
```

## RCA Investigation Workflow

1. List recent unresolved issues: `issue list --query is:unresolved --sort date`
2. View issue detail for context: `issue view ISSUE-ID`
3. Get AI root cause analysis: `issue explain ISSUE-ID`
4. Inspect specific events/stack traces: `issue events ISSUE-ID`
5. Explore aggregate error data: `explore errors --fields title,count(),last_seen() --period 1h`
6. Check alert rules: `api /organizations/{org}/alert-rules/`

## Notes
- All commands automatically get `--json` appended for structured output
- The `SENTRY_ORG` and `SENTRY_AUTH_TOKEN` are injected automatically per user
- `issue explain` uses Sentry's Seer AI for root cause analysis — prefer this over manual analysis
- Use `sentry schema` to discover available API endpoints if you need data not covered by a dedicated command
