---
sidebar_position: 4
---

# MCP (Model Context Protocol)

Aurora exposes a focused, token-lean tool surface over [MCP](https://modelcontextprotocol.io/) so external clients (Cursor, Claude Desktop, Windsurf, etc.) can drive real investigations against Aurora without pulling in the full 150-tool agent catalog.

The surface is **hybrid**: a small set of always-visible tools handles the 80% case, and a search-and-call pattern (`search_tools` + `call_tool`) reaches the long tail.

## Tool Tiers

### Tier 1 — Always visible

| Tool | Description |
|------|-------------|
| `list_incidents` | List Aurora incidents, optionally filtered by status |
| `get_incident` | Full incident details with summary, suggestions, citations |
| `incident_findings` | What each RCA sub-agent investigated: role, `tools_used`, citations, follow-ups |
| `incident_finding_detail` | One sub-agent's full finding + per-step `tool_call_history` (the exact tools/steps the RCA ran) |
| `incident_list_alerts` | The incident's correlated alerts: source, title, service, severity, correlation score |
| `get_infrastructure_context` | System topology: environments, services, dependencies, CI/CD, and monitoring — a snapshot of how the system fits together |
| `list_services` | Services in the dependency graph (filters: `resource_type`, `provider`) |
| `service_impact` | Blast radius — downstream services that depend on a given service |
| `list_actions` | List the org's Aurora actions (automations): trigger, mode, run count, last-run status |
| `get_action` | One action's config plus its 20 most recent runs |
| `list_action_runs` | An action's run history: status, timing, errors (`limit`, `offset`) |
| `trigger_rca` | Start a new RCA from a free-text description |
| `knowledge_base_search` | Semantic search across Aurora's ingested docs |
| `search_runbooks` | Unified runbook search across the knowledge base, Confluence, SharePoint |
| `search_tools` | Discover additional tools available behind the long-tail dispatch |
| `call_tool` | Invoke a tool returned by `search_tools` |

#### Tool design philosophy

The MCP surface exposes fast, single-purpose reads (incidents, alerts, topology, service impact, RCA findings, metrics, postmortems, your actions). They return in about a second, so most everyday questions — *"what was my last incident?"*, *"what depends on payments-svc?"* — resolve through these.

To start a new investigation, use `trigger_rca` with a problem description. It creates an incident and dispatches Aurora's full background RCA pipeline — the same pipeline used by the UI's RCA button and webhook-triggered alerts.

The guidance for tool selection is built into the tool descriptions, so this works across MCP clients (Cursor, Claude Desktop/Code, Codex, Windsurf, ...) without any per-client tuning.

### Tier 2 — Connector-gated

These appear in your tool list only when at least one backing integration is connected for the user:

| Tool | Enabling integrations |
|------|----------------------|
| `query_logs` | Datadog · Splunk |
| `query_metrics` | Datadog |
| `query_alerts` | Datadog · New Relic · Dynatrace · OpsGenie · incident.io · Splunk |
| `query_jira` | Jira (search, get issue) |
| `query_notion` | Notion (list databases, fetch database) |
| `query_bitbucket` | Bitbucket (workspaces, repos, branches, PRs) |

Connect a new integration in the Aurora UI and the corresponding tool appears in your MCP client on its next request — the list is rebuilt per request.

### Tier 3 — Long tail via search

Specific endpoints are not in the upfront list. Discover them with `search_tools("query")` and call them with `call_tool("name", { args })`. The full set is governed by a hard-coded **allowlist** in `server/aurora_mcp/registry.py`. Notable reads reachable here (once the connector is connected):

- **DORA / SRE metrics** — `metrics_mttr`, `metrics_mttd`, `metrics_change_failure_rate`, `metrics_incident_frequency`, …
- **Postmortems** — `postmortem_list`, `postmortem_get_for_incident`, plus exports to Confluence/Jira/Notion
- **CI/CD deployments** — `jenkins_list_deployments`, `cloudbees_list_deployments`, `spinnaker_list_deployments` / `spinnaker_list_applications` / `spinnaker_list_pipelines` / `spinnaker_app_health` (great for *"what deployed right before this incident?"*)
- **Sentry** — `sentry_list_projects`, `sentry_list_issues`, `sentry_list_events`
- **Grafana** — `grafana_list_alerts`
- **Action writes** — `action_create`, `action_update`, `action_delete`, `action_restore_default`, `action_trigger` (the action *reads* are first-class Tier-1 tools above)
- **Logs / metrics, Jira, GitHub, Bitbucket, Notion, Confluence/SharePoint runbooks** — and more

**Out of scope for MCP** (deliberately not in the allowlist): Terraform apply/destroy, kubectl mutations, raw shell exec, Cloudflare WAF/DNS writes. These remain in the agent's internal surface only.

## Resources

URI-fetched reference data — costs zero tokens until requested:

| Resource URI | Description |
|-------------|-------------|
| `aurora://catalog/connectors` | The user's connected providers and their status |
| `aurora://catalog/skills` | All skills, with per-user connection status |
| `aurora://incidents/recent` | Last 20 incidents (titles only, no full bodies) |
| `aurora://runbooks/index` | Runbook index per connected doc connector |
| `aurora://health` | Live system health: database, Redis, Weaviate, Celery |

## Prompts

Pre-built workflows your assistant can pick from a menu:

| Prompt | Parameters | Description |
|--------|-----------|-------------|
| `investigate_incident` | `incident_id` | Step-by-step incident investigation |
| `blast_radius_analysis` | `service_name` | Downstream dependencies + active incidents on affected services |
| `triage_alert` | `alert_id` | Triage workflow tying alerts → logs → metrics → recent deploys |
| `summarize_incident` | `incident_id` | Produces a postmortem-shaped summary with citations |

## Authentication

MCP uses per-user Bearer tokens stored in the `mcp_tokens` table. Tokens are resolved directly against Postgres (not via the Flask API) to keep the auth path independent of the main server.

Generate a token from the Aurora UI under **Settings > API Tokens > MCP**, or insert one directly:

```sql
INSERT INTO mcp_tokens (user_id, org_id, token, status)
VALUES ('<user-id>', '<org-id>', '<token>', 'active');
```

Tokens can have an optional `expires_at` timestamp. `last_used_at` is updated automatically.

## Client Setup

### Cursor

Add to `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "aurora": {
      "url": "<AURORA_MCP_URL>/mcp",
      "headers": {
        "Authorization": "Bearer <YOUR_MCP_TOKEN>"
      }
    }
  }
}
```

### Claude Desktop

Add to Claude Desktop's MCP config (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "aurora": {
      "url": "<AURORA_MCP_URL>/mcp",
      "headers": {
        "Authorization": "Bearer <YOUR_MCP_TOKEN>"
      }
    }
  }
}
```

### Windsurf

Add to Windsurf's MCP configuration:

```json
{
  "mcpServers": {
    "aurora": {
      "serverUrl": "<AURORA_MCP_URL>/mcp",
      "headers": {
        "Authorization": "Bearer <YOUR_MCP_TOKEN>"
      }
    }
  }
}
```

> **Cursor tool cap:** Cursor advertises at most **40 MCP tools** across all configured servers (extras are silently dropped). Aurora's upfront surface is ~22 tools, so it fits comfortably — but if you run several MCP servers, keep an eye on the shared budget.

### Claude Code

Add the server with `claude mcp add`, or in `.mcp.json`:

```json
{
  "mcpServers": {
    "aurora": {
      "url": "<AURORA_MCP_URL>/mcp",
      "headers": { "Authorization": "Bearer <YOUR_MCP_TOKEN>" }
    }
  }
}
```

Claude Code defers MCP tools behind its own tool search. To keep Aurora's entrypoints in context, mark the server `alwaysLoad` in your client config — no server-side change needed.

### Codex

Add to `~/.codex/config.toml`. Codex reads the Bearer token from an environment variable:

```toml
[mcp_servers.aurora]
url = "<AURORA_MCP_URL>/mcp"
bearer_token_env_var = "AURORA_MCP_TOKEN"
```

### Cline

Cline defaults URL-only entries to the SSE transport, which Aurora does not use — set the transport explicitly to streamable HTTP in the server entry to avoid a connection failure.

Replace `<AURORA_MCP_URL>` with your Aurora deployment's MCP endpoint:

| Deployment | `<AURORA_MCP_URL>` |
|-----------|----------|
| Docker Compose (local) | `http://localhost:8811` |
| Docker Compose (remote/VM) | `http://<VM_IP>:8811` |
| Kubernetes (port-forward) | `http://localhost:8811` after `kubectl port-forward svc/aurora-oss-mcp 8811:8811 -n aurora-oss` |
| Kubernetes (ingress) | `https://mcp.yourdomain.com` (see [Kubernetes docs](../deployment/kubernetes#mcp-ingress)) |

## Security Considerations

:::warning External Exposure
The MCP server grants full platform access (under each user's own RBAC and RLS scope) to any client with a valid token. When exposing MCP externally via ingress:

- **Always** place it behind an auth proxy (e.g. oauth2-proxy, nginx `auth_request`) in addition to the Bearer token
- Prefer keeping MCP cluster-internal and using `kubectl port-forward` for developer access
- If you must expose it, use TLS and restrict access by IP or VPN
:::

### Allowlist (Tier 3 dispatch)

The set of tools reachable through `call_tool` is hard-coded in `server/aurora_mcp/registry.py`. Infra-write surfaces (Terraform apply, kubectl mutations, shell exec, Cloudflare WAF/DNS) are excluded by design and additionally protected by a startup assertion against banned name fragments. To audit, read `DISPATCH_ALLOWLIST` in that file — it's the single source of truth.

### When to Use Ingress vs Port-Forward

| Approach | Use Case |
|----------|----------|
| **Port-forward** (recommended) | Individual developer access. No ingress config needed. Secure by default. |
| **Ingress** | Shared team endpoint or CI/CD integrations. Requires auth proxy. |

## Example Usage

Once connected, your AI assistant can interact with Aurora:

```text
"What was my most recent incident?"
→ calls list_incidents(limit=1)   (a direct read — not chat)

"What alerts fired for incident X?"
→ calls incident_list_alerts(incident_id="X")

"What depends on payments-svc?"
→ calls service_impact(name="payments-svc")

"What tools/steps did this RCA use?"
→ calls incident_findings(incident_id="X")
→ then incident_finding_detail(incident_id="X", agent_id="...")

"What's our MTTR over the last 30 days?"
→ calls search_tools(query="mttr dora") → call_tool("metrics_mttr", { period: "30d" })

"Why did checkout-svc page at 3am — dig into it?"
→ calls trigger_rca(issue_description="checkout-svc paged at 3am", service="checkout-svc")
   Creates an incident and starts the full RCA pipeline in the background.
```

These map to the same capabilities you see in the Aurora UI: questions resolve through the fast direct tools (about a second), while `trigger_rca` dispatches Aurora's full agent-driven investigation in the background — the same system prompts and skill loader as the in-app RCA.
