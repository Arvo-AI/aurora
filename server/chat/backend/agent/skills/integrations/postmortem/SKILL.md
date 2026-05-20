---
name: postmortem
id: postmortem
description: "Postmortem generation and management tools for writing, reading, and versioning incident postmortems"
category: core
connection_check:
  method: always
tools:
  - get_postmortem
index: "Incident postmortem management -- read, write, version postmortem documents"
rca_priority: 90
metadata:
  author: aurora
  version: "1.0"
---

# Postmortem Tools

## Overview
Core tools for reading postmortem documents during investigation, and writing them during the dedicated "Generate Postmortem" action. `get_postmortem` is available in all sessions. `save_postmortem` is only available during the "Generate Postmortem" action — not during RCA.

## Tools

### `get_postmortem(incident_id)`
Read the current postmortem for an incident. Returns markdown content, generated_at, and updated_at timestamps. Returns `status: "not_found"` if no postmortem exists yet. Only the latest version is returned; historical versions are browsable in the UI but not via this tool.

### `save_postmortem(incident_id, content)`
Write or update a postmortem. **Only available during the "Generate Postmortem" action — not during RCA.** Each save automatically snapshots the previous content as a version for history tracking. Content must be complete markdown — partial updates are not supported. Max 100,000 characters.

## Workflow

1. Call `get_postmortem` — if a prior version exists, use it as a baseline to preserve structure and confirmed facts
2. Gather context from RCA summary and connected communication platforms (Slack, etc.)
3. Write structured markdown following the output format below
4. Call `save_postmortem`

## Output Format

Always generate postmortems with these sections in order:

```markdown
# Postmortem: <Incident Title>

**Date:** YYYY-MM-DD HH:MM UTC
**Duration:** Xh Ym
**Severity:** critical/high/medium/low
**Service:** <service name>

## Summary
2-3 sentences describing what happened.

## Timeline
- **HH:MM UTC** - Event description
- **HH:MM UTC** - Event description

## Root Cause
Technical explanation of what failed and why.

## Impact
Services, users, SLAs, and data affected.

## Contributing Factors
Human/process factors: deployment pressure, alert fatigue, communication gaps, handoff confusion.
Only include if evidence exists from conversations.

## Resolution
How the incident was resolved or mitigated.

## Action Items
- [ ] Concrete follow-up item
- [ ] Another follow-up item

## Lessons Learned
What can prevent similar incidents in the future.
```

## Guidelines
- Professional, factual tone
- Do not speculate beyond what data supports
- Incorporate human context from Slack/communication tools when available
- If Slack tools are available, search relevant channels in the incident time window before writing
- Keep the document concise but thorough
- Use checkboxes for action items so they can be tracked
