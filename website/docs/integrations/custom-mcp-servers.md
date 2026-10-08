---
sidebar_position: 5
---

# Custom MCP Servers

Register your own [MCP](https://modelcontextprotocol.io/) servers so Aurora can query systems it has no built-in connector for — an internal service, a vendor's hosted MCP endpoint, or anything you have wrapped in an MCP server yourself. Aurora discovers each server's tools on connect and uses them during chat and investigations.

:::info This is the opposite direction from the MCP integration guide
This page is about Aurora **calling out** to your MCP servers. If you want to drive Aurora *from* Cursor or Claude Desktop, Aurora is itself an MCP server — see [MCP (Model Context Protocol)](./mcp.md).
:::

## Before you start

- **Remote HTTP servers only.** Aurora connects over streamable HTTP or SSE. `stdio` servers (the kind that run as a local subprocess) are not supported, because Aurora runs in a container with no access to your machine.
- The URL must be reachable from the Aurora server, not just from your laptop.
- Private, loopback and link-local MCP URLs are allowed by default (Compose, Helm, and unset env). Multi-tenant SaaS should set `MCP_ALLOW_PRIVATE_TARGETS=false` — see [Private network targets](#private-network-targets).
- Servers are shared **across your whole organization** — see [Scope](#scope-and-permissions).

## Connect a server

1. Click **Connectors** in the left sidebar and open **Custom MCP Servers**.
2. Enter a **Name**. This is how you and the agent refer to the server when picking a tool (`linear`, `netbox`, `context7`). Lowercase letters, digits and dashes, up to 32 characters.
3. Enter the **Server URL**, e.g. `https://mcp.example.com/mcp`.
4. Click **Connect**.

Aurora works out the rest. It probes the URL to determine the transport and what credentials the server wants, then connects:

| What the probe finds | What happens |
|---|---|
| Connects without credentials | Saved immediately |
| OAuth | Aurora opens the provider's sign-in page in a pop-up, registers itself, and completes the flow |
| Needs an API key | A **Token** field appears — paste the token and click Connect again |

The button names the step it is on: **Checking server**, **Waiting for sign-in**, then **Discovering tools**. Allow pop-ups for your Aurora hostname, or the OAuth step cannot open its window.

Nothing is saved until the handshake succeeds, so a server that appears in the list is one Aurora could actually reach.

### If detection gets it wrong

Click **Set authentication manually** to choose the method yourself. Two cases need it:

- **A server that answers `403` instead of `401`.** Detection cannot tell "needs credentials" from "you are forbidden", so it may report the wrong thing.
- **A server whose dynamic client registration is broken or disabled.** Aurora normally registers itself with the OAuth provider automatically; if that fails you will be asked for a client ID to use instead.

Manual mode also offers **Custom header**, for servers that want their key in something other than `Authorization: Bearer` — set the header name (e.g. `X-Api-Key`) alongside the token.

### OAuth redirect URI

Aurora registers itself with the provider using Dynamic Client Registration (RFC 7591) and PKCE, so there is usually nothing to configure. If a provider requires you to pre-register the redirect URI, use:

```
<FRONTEND_URL>/mcp/callback
```

`FRONTEND_URL` is the user-facing Aurora hostname from your `.env` — e.g. `https://aurora.example.com/mcp/callback`. The redirect URI is always derived from server config and never from the request.

## Allow and confirm

Aurora cannot tell what a third-party tool does to your systems, so every discovered tool gets one of two modes:

| Mode | Runs without asking | Available in background investigations |
|---|---|---|
| **Allow** | yes | yes |
| **Confirm** | no, asks you first | no |

A **confirm** tool needs a human present to approve it, so it is withheld from automated RCA and PR review rather than silently failing there.

Defaults are derived per tool, in this order:

1. The server's `destructiveHint` annotation, if set — an explicit "this mutates" always wins.
2. The server's `readOnlyHint` annotation, if set. The server author knows better than a guess.
3. The verb in the tool's name, for servers that send no annotations at all.

Reads default to **Allow**, writes to **Confirm**. Override any tool individually: expand the server's tool list on the connector page and change its dropdown. Annotations buy accuracy, not trust — a server could claim `readOnlyHint` on a destructive tool, which is what the per-tool override is for.

In **Ask mode**, write tools are refused regardless of their setting.

## How the agent uses them

Custom MCP tools are **not** loaded into the prompt individually. A handful of servers with a few hundred tools between them would cost more context than the rest of the system prompt combined. Instead the agent gets two tools and discovers the rest on demand:

| Tool | Purpose |
|---|---|
| `mcp_list_tools` | Called bare, lists your servers and their tool counts. With `server=` it lists that server's tools with compact argument summaries. With `query=` it searches across every server. With `server=` **and** `tool=` it returns that one tool's full JSON Schema. |
| `mcp_call_tool` | Runs one tool: `server`, `tool`, and an `arguments` object. |

The practical effect: adding a server with 200 tools costs a line in the prompt, not 200 tool definitions. You do not need to do anything to get this — it is how the connector works.

## Managing servers

- **Refresh tools** re-runs discovery against the server. Use it after the server gains or loses tools; Aurora does not poll for changes.
- The tool list on each card is collapsed by default, showing the tool count and how many need confirmation. Click it to expand.
- **Delete** (trash icon) removes the server and its stored credentials.

## Limits

| Limit | Value |
|---|---|
| Servers per organization | 10 |
| Tools discovered per server | 256 |
| Name length | 32 characters |

Past 256 tools, Aurora registers the first 256 and tells you it truncated. This is a storage bound, not a context one — the dispatcher above means tool count does not inflate the prompt.

## Scope and permissions

Custom MCP servers are **organization-wide**, not per-user. One row per org holds the whole set, so one person registers a server and every member's agent can use it. Per-tool allow/confirm settings are stored alongside and are therefore also org-wide — treat them as team policy, not personal preference.

Two consequences worth planning for:

- **OAuth identity is shared.** If you connect a server as yourself, the whole org acts as your user on that system. Writes will be attributed to you, and the agent sees exactly what you can see. For anything your team will write through, consider connecting a dedicated bot account rather than a person's.
- **Adding, refreshing and removing servers requires the `connectors:write` permission.** Reading the list requires `connectors:read`.

Credentials — bearer tokens, custom header values, OAuth access and refresh tokens — are stored in your secrets backend (Vault by default), with only a reference kept in the database. See [Vault Configuration](/docs/configuration/vault).

## Private network targets

Aurora resolves every MCP URL and rejects it if **any** resolved address is private, loopback or link-local. This blocks the classic SSRF path — pointing Aurora at `169.254.169.254` to read cloud instance metadata — and a hostname with both a public and a loopback A record cannot slip past it.

Private MCP URLs are **allowed by default** (`MCP_ALLOW_PRIVATE_TARGETS=true` in Compose, Helm, and when the variable is unset). That matches most self-hosted installs, where MCP servers live on cluster-internal addresses.

Set `MCP_ALLOW_PRIVATE_TARGETS=false` on **multi-tenant SaaS** (or anywhere you do not fully trust everyone with `connectors:write`): with private targets allowed, they can aim Aurora at anything your network can reach, including cloud metadata endpoints.

The guard also covers OAuth metadata, registration and token endpoints, not just the MCP connection itself — otherwise a hostile server could redirect Aurora's token request to an internal address.

## Troubleshooting

| Problem | Solution |
|---|---|
| "Could not reach the MCP server" | The URL must be reachable from the Aurora server. Check it is not `localhost`-only or behind a network Aurora cannot route to, and that it speaks streamable HTTP or SSE. |
| "Allow pop-ups for this site to authorize the server" | The OAuth window was blocked. Allow pop-ups for your Aurora hostname and connect again. |
| The URL is rejected before any request is made | Private targets are disabled (`MCP_ALLOW_PRIVATE_TARGETS=false`). Set it to `true` if the server is on a private or loopback address inside your network. |
| Detection says the server needs a token, but it does not | The server answered `403`. Use **Set authentication manually** and pick **None**. |
| "Dynamic client registration is not supported" | The OAuth provider will not self-register clients. Register Aurora manually with the redirect URI above, then supply the client ID in manual mode. |
| A tool the agent should use is never called | Check its mode. A **confirm** tool is withheld from background investigations by design — set it to **Allow** if it is safe to run unattended. |
| Tools the server added are missing | Click **Refresh tools**. Discovery runs on connect, not on a schedule. |
| "At most 10 MCP servers can be registered" | Remove one first. |
