# PR #257 Review: skills-based prompt architecture

## Connection checking must use a single source of truth

The skills registry (`server/chat/backend/agent/skills/registry.py`) introduces its own connection-checking system that diverges from the existing unified connector status endpoint (`server/routes/connector_status.py`). This creates two separate codepaths that can disagree on whether a provider is connected.

### Current state (connector_status.py)

The frontend calls `GET /api/connectors/status`, which runs `_check_all_connectors(user_id, org_id)`. This is the authoritative source:

- Queries both `user_tokens` (with `secret_ref IS NOT NULL AND is_active = TRUE`) and `user_connections` (with `status = 'active'`)
- Uses `org_id` so org-level shared connections are visible
- Does **live API validation** for most providers (Datadog calls `/api/v1/validate`, GitHub calls `/user`, Jenkins calls `/api/json`, Jira/Confluence do OAuth refresh, etc.)
- Runs all checks in parallel via ThreadPoolExecutor
- Single `PROVIDER_CHECKERS` registry maps every provider to its checker function

### What the skills registry does instead

Each SKILL.md file declares a `connection_check` block in YAML frontmatter with one of 4 methods:

| Method | Used by | What it does |
|---|---|---|
| `get_credentials_from_db` | GitHub, Bitbucket | Checks if a DB row exists -- no live validation |
| `get_token_data` | Jenkins, CloudBees, Jira, Confluence, SharePoint | Calls `get_token_data()` and checks a required field -- no live validation |
| `is_connected_function` | Datadog, NewRelic, OpsGenie, Splunk, Dynatrace, Coroot, Cloudflare, Spinnaker, ThousandEyes, kubectl | Dynamically imports and calls `is_*_connected(user_id)` from tool modules |
| `provider_in_preference` | OVH, Scaleway, Tailscale, Grafana | Checks if provider is in user's connected providers list |

### Problems

1. **Flask app context errors**: The `is_connected_function` methods for Datadog, NewRelic, and OpsGenie fail in the chatbot process because those functions need Flask's `current_app` context. Confirmed in logs:
   ```
   routes.datadog.datadog_routes - ERROR - [DATADOG] Failed to retrieve credentials: Working outside of application context.
   routes.newrelic.newrelic_routes - ERROR - [NEWRELIC] Failed to retrieve credentials: Working outside of application context.
   routes.opsgenie.opsgenie_routes - ERROR - [OPSGENIE] Failed to retrieve credentials: Working outside of application context.
   ```

2. **No org_id awareness**: `connector_status.py` checks both `user_id` and `org_id` for shared org-level connections. The registry's `check_connection(skill_id, user_id)` has no org_id parameter, so org-shared connections are invisible.

3. **No live validation**: The registry never pings external APIs. A user with expired/revoked credentials would still show as "connected" if the DB row exists. The frontend would correctly show them as disconnected.

4. **Duplicated logic**: We now have connection-checking logic in 3 places -- `connector_status.py`, the registry's `_dispatch_check`, and the individual `is_*_connected()` functions in tool modules. Any change to how we check a provider must be updated in all of them.

### Recommended fix

The registry should delegate to the same `connector_status` logic instead of reimplementing it. Two options:

**Option A -- Call `_check_all_connectors` once and cache**: At the start of prompt building, call the existing `_check_all_connectors(user_id, org_id)` and pass the results dict to the registry. The registry just looks up `results[provider]["connected"]` instead of running its own checks. This is the cleanest approach since it's already battle-tested and handles OAuth refresh, org scoping, and live validation.

**Option B -- Lightweight DB-only check using the same queries**: If live API calls are too slow for the chat hot path, extract the DB query from `_check_all_connectors` (the `user_tokens` + `user_connections` query) into a shared utility function and have both the registry and the status endpoint use it. The status endpoint would continue to do live validation on top, but the registry would at least have the same DB-level source of truth including org_id.

Either way, the YAML `connection_check` blocks in SKILL.md files and the `_dispatch_check` method in the registry should be removed. Connection status is a platform concern, not something each skill should define independently.
