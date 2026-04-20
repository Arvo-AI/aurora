---
sidebar_position: 4
---

# MCP (Model Context Protocol)

Aurora exposes its full API surface over [MCP](https://modelcontextprotocol.io/), letting AI coding assistants (Cursor, Claude Desktop, Windsurf, etc.) query incidents, search your knowledge base, and call any Aurora API endpoint directly from the editor.

## Available Tools

| Tool | Description |
|------|-------------|
| `list_incidents` | List incidents, optionally filtered by status |
| `get_incident` | Full incident details with summary, suggestions, and alerts |
| `ask_incident` | Ask Aurora AI a question about an incident |
| `get_graph_stats` | Infrastructure graph: single points of failure, critical services |
| `search_knowledge_base` | Semantic search across ingested runbooks, postmortems, and docs |
| `aurora_api` | Generic proxy to any of Aurora's ~340 API endpoints |

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
      "url": "http://<AURORA_MCP_HOST>:<MCP_PORT>/mcp",
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
      "url": "http://<AURORA_MCP_HOST>:<MCP_PORT>/mcp",
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
      "serverUrl": "http://<AURORA_MCP_HOST>:<MCP_PORT>/mcp",
      "headers": {
        "Authorization": "Bearer <YOUR_MCP_TOKEN>"
      }
    }
  }
}
```

Replace `<AURORA_MCP_HOST>` with your Aurora deployment's MCP endpoint:

| Deployment | Endpoint |
|-----------|----------|
| Docker Compose (local) | `localhost:8811` |
| Docker Compose (remote/VM) | `<VM_IP>:8811` |
| Kubernetes (port-forward) | `localhost:8811` after `kubectl port-forward svc/aurora-oss-mcp 8811:8811 -n aurora-oss` |
| Kubernetes (ingress) | `mcp.yourdomain.com` (see [Kubernetes docs](../deployment/kubernetes#mcp-ingress)) |

## Security Considerations

:::warning External Exposure
The MCP server grants full platform access to any client with a valid token. When exposing MCP externally via ingress:

- **Always** place it behind an auth proxy (e.g. oauth2-proxy, nginx `auth_request`) in addition to the Bearer token
- Prefer keeping MCP cluster-internal and using `kubectl port-forward` for developer access
- If you must expose it, use TLS and restrict access by IP or VPN
:::

### When to Use Ingress vs Port-Forward

| Approach | Use Case |
|----------|----------|
| **Port-forward** (recommended) | Individual developer access. No ingress config needed. Secure by default. |
| **Ingress** | Shared team endpoint or CI/CD integrations. Requires auth proxy. |

## Example Usage

Once connected, your AI assistant can interact with Aurora:

```
"List all investigating incidents"
→ calls list_incidents(status="investigating")

"What caused incident abc-123?"
→ calls get_incident("abc-123"), then ask_incident("abc-123", "What was the root cause?")

"Show me the infrastructure graph stats"
→ calls get_graph_stats()

"Check the health endpoint"
→ calls aurora_api(method="GET", path="/health/")
```

The `aurora_api` tool is a generic proxy -- read the `aurora://api-catalog` resource in your MCP client to discover all available endpoints.
