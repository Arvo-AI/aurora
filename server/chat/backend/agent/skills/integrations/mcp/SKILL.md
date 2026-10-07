---
name: mcp
id: mcp
description: "Customer-registered MCP servers — query systems Aurora has no built-in connector for, using tools the customer's own MCP servers expose"
category: integrations
connection_check:
  method: is_connected_function
  module: chat.backend.agent.tools.custom_mcp_tools
  function: is_mcp_connected
index: "Custom MCP servers — tools for systems without a built-in Aurora connector"
rca_priority: 5
metadata:
  author: aurora
  version: "1.0"
---

# Custom MCP Servers

## Overview
Your organisation has registered one or more of its own MCP (Model Context Protocol)
servers. These expose systems Aurora has no built-in connector for, so they are often the
ONLY way to see telemetry or configuration for those systems. Treat them as first-class
investigation sources, not a last resort.

Their individual tools are NOT listed in your prompt — there can be hundreds. You reach
them through two tools: `mcp_list_tools` to discover, then `mcp_call_tool` to invoke.

## Connected servers

{mcp_servers_section}

## Instructions

1. **Discover before calling.** `mcp_list_tools()` with no arguments gives the server list
   and counts. `mcp_list_tools(query="...")` searches across every server at once — use it
   when you do not know which server holds what you need. `mcp_list_tools(server="...")`
   lists one server's tools with their input schemas.
2. **Call with exact names.** Pass the `server` and `tool` strings from the discovery result
   verbatim to `mcp_call_tool`, with `arguments` matching the `inputSchema` it showed you.
   Guessing a tool name wastes a turn; the call is refused with the known server list.
3. **Read the schema, not your assumptions.** These tools are defined by whoever built the
   server, not by Aurora. The `description` and `inputSchema` from discovery are
   authoritative, including which arguments are required.
4. **Prefer a specific tool over a broad one.** If a server offers both `get_zone` and
   `list_zones`, fetch the single record you need rather than listing everything.
5. **Report which server answered.** When a finding comes from a custom MCP server, name the
   server label in your conclusion so a human can verify it against the right system.
6. **A failure is information.** If a tool returns a connection error, say so plainly and move
   on to another source. Do not retry the same call repeatedly — the server may be
   unreachable from Aurora's network, which is a real finding worth reporting.

## Tools marked `"write": true`

Any tool whose name does not begin with a read verb (`get_`, `list_`, `search_`, `read_`,
`describe_`, `query_`, `fetch_`, `check_`) is treated as a write, unless the server declared
otherwise.

- In interactive chat, calling one prompts the user to confirm first.
- During RCA and other background work these tools are **not listed and cannot be called** —
  no human is present to approve them. Do not plan around them; investigate with reads only.

Aurora cannot determine what a third-party tool actually changes, so this classification is
based on naming. If a read-looking tool appears to have modified something, report it.
