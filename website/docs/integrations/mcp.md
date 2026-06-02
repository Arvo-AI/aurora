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
| `chat_with_aurora` | **Default for any open-ended question.** Aurora's agent picks the right data sources, runs RCAs, and cites sources. Prefer this over individual tools unless the user explicitly asks for raw data. |
| `list_incidents` | List Aurora incidents, optionally filtered by status |
| `get_incident` | Full incident details with summary, suggestions, citations |
| `ask_incident` | Incident-scoped follow-up Q&A |
| `trigger_rca` | Kick off (or restart) Aurora's RCA pipeline for an incident |
| `knowledge_base_search` | Semantic search across Aurora's ingested docs |
| `search_runbooks` | Unified runbook search across the knowledge base, Confluence, SharePoint |
| `search_tools` | Discover additional tools available behind the long-tail dispatch |
| `call_tool` | Invoke a tool returned by `search_tools` |

### Tier 2 — Connector-gated

These appear in your tool list only when at least one backing integration is connected for the user:

| Tool | Enabling integrations |
|------|----------------------|
| `query_logs` | Datadog · New Relic · Splunk · Coroot · Dynatrace |
| `query_metrics` | Datadog · New Relic · Coroot · Dynatrace |
| `query_traces` | Datadog APM · Coroot · Dynatrace |
| `query_alerts` | Datadog · OpsGenie · incident.io |
| `github_rca` | GitHub |
| `query_jira` | Jira (search, get issue) |
| `query_incidentio` | incident.io (list, get, timeline) |
| `query_thousandeyes` | ThousandEyes (tests, alerts, agents) |
| `query_coroot` | Coroot (applications, incidents, logs) |
| `query_notion` | Notion (search, fetch page) |
| `query_bitbucket` | Bitbucket (repos, branches, PRs) |

Connect a new integration in the Aurora UI and the corresponding tool appears in your MCP client on its next request — the list is rebuilt per request.

### Tier 3 — Long tail via search

Specific endpoints (individual ThousandEyes routes, Notion writes, Bitbucket writes, postmortem actions, etc.) are not in the upfront list. Discover them with `search_tools("query")` and call them with `call_tool("name", { args })`. The full set is governed by a hard-coded **allowlist** in `server/aurora_mcp/registry.py`.

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
"Why did checkout-svc page at 3am?"
→ calls chat_with_aurora("Why did checkout-svc page at 3am?")
   Aurora's agent picks the right tools, runs the full RCA, and replies with citations.

"List all investigating incidents"
→ calls list_incidents(status="investigating")

"Pull the raw Datadog logs for checkout-svc"
→ calls query_logs(query="service:checkout-svc", source="datadog")

"Are there any tools for creating Jira issues?"
→ calls search_tools(query="jira create")
→ then call_tool("jira_create_issue", { project: "OPS", summary: "...", ... })
```

The default path is `chat_with_aurora` — Aurora's own agent runs server-side with its full system prompts and skill loader, so behavior matches the Aurora UI. Direct tools are available for surgical access to raw data when needed.
