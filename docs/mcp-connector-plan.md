# Plan: customer-supplied MCP servers as connectors

DEV-1604. Status: awaiting approval. No code written.

## Problem

The customer wants Aurora to talk to systems Aurora has no connector for. Their framing on
Oct 2: everything missing a connector still has an API, they will write the MCP servers
themselves (Cloudflare already publishes one), and the only blocker is that Aurora speaks to
a fixed list of servers compiled into the image.

Today that list is three entries, hardcoded:

```
REAL_MCP_SERVER_PATHS = {"aws": ..., "github": ..., "context7": ...}
```
`server/chat/backend/agent/tools/mcp_tools.py:28-33`, with matching `server_configs` at
`:110-127`. There is no table, no route, no UI, and no code path by which a customer adds a
fourth. Greps for `custom_mcp`, `mcp_connector`, `user_mcp` and `mcp_server_url` return
nothing, so there is no partial work to resume.

## The direction confusion worth clearing up first

The repo contains two unrelated MCP subsystems. Only one of them is relevant.

| Path | Direction | Relevant? |
|---|---|---|
| `server/aurora_mcp/`, `server/mcp_server.py` | Aurora **is** an MCP server; Claude/Cursor call Aurora | No |
| `server/chat/backend/agent/tools/mcp_tools.py` | Aurora **is** an MCP client; the agent calls out | Yes |

This work extends the client side. Nothing in `aurora_mcp/` changes.

## Scope

A customer registers N remote MCP servers, Aurora discovers their tools at registration, and
the agent can call them in chat and in RCA. Per DEV-1600 this is the Private Link one-pager's
Phase 1 row, **scoped to servers reachable from the Aurora environment**.

Deliberately out of scope, each with a reason:

| Excluded | Why |
|---|---|
| stdio / command-spawned servers | Running a customer-supplied command inside Aurora's container is remote code execution. See "Transport" below. |
| OAuth-authenticated servers | Dynamic client registration, a callback route, and refresh handling is its own ticket. Bearer tokens cover self-built servers, and the major hosted servers accept a token as an automation path alongside OAuth. |
| MCP resources and prompts | Tools are the whole ask. `resources/list` adds surface for no requested capability. |
| On-prem servers unreachable from Arvo's network | Blocked on the agent extension already tracked in DEV-1600. Nothing here makes an unroutable host routable. |
| Exposing custom tools **directly** through `aurora_mcp` | No new passthrough tool is added there. But see the note below — a read-only path already exists transitively, and claiming otherwise would be wrong. |

### Correction: Aurora-as-server already reaches custom servers transitively

Worth stating plainly rather than discovering it in review. `server/aurora_mcp/` exposes
`trigger_rca`, which POSTs to `/api/incidents/trigger-rca`
(`aurora_mcp/tools_always_on.py:61-75`) and starts the background RCA agent — and that agent
holds the custom MCP tools. So this chain exists the moment this feature ships:

```
Claude Desktop -> Aurora MCP (trigger_rca) -> RCA agent -> customer's MCP server
```

Not closable without special-casing `trigger_rca`, and arguably correct: an investigation
should use every source available. Two things make it acceptable rather than alarming:

- Background context offers **no write tools at all** (see the gating table), so the transitive
  reach is read-only by construction.
- `chat_with_aurora` and `ask_incident`, the two tools that would otherwise be an open relay
  into the agent's full toolset, are already deprecated stubs that return an error string
  (`tools_always_on.py:228-250`).

What stays excluded is adding a *new* `aurora_mcp` tool that proxies arbitrary custom-server
calls on demand. That would be a general-purpose tunnel into a customer network for any holder
of an Aurora MCP token, which is a different risk from an RCA reading telemetry.

## Prerequisite: the `mcp` dependency is unpinned and already broken

`server/requirements.txt:113` reads:

```
mcp[cli]>=1.27.0
```

An unpinned floor. Verified by installing both resolutions:

| | `mcp==1.27.0` | `mcp==2.3.0` (current latest) |
|---|---|---|
| Client transport | `streamablehttp_client(url, headers=..., timeout=, auth=)` | `streamable_http_client(url, *, http_client)` — **no `headers=`** |
| Session | `ClientSession(read, write)`, `await initialize()` | `Client(transport)`, no `initialize` |
| HTTP library | `httpx` | `httpx2` |
| `from mcp.server.fastmcp import FastMCP` | works | **`ModuleNotFoundError`** |

That last row is the live problem: `server/mcp_server.py:29` imports `mcp.server.fastmcp`.
The next clean image build resolves 2.3.0 and that import fails. The SDK ships an error
message naming the rename and recommending `pin 'mcp<2'`. Current deployments only work
because they run an older cached layer.

**Pin `mcp[cli]>=1.27.0,<2` as part of this PR.** One line, unblocks the work, and fixes a
build-breaker that is not otherwise anyone's ticket. Migrating to 2.x is a separate decision
and touches `aurora_mcp/` far more than it touches this feature.

## Transport: remote HTTP only, via the SDK

Two decisions, both of which remove code rather than add it.

**1. No stdio.** The existing client spawns servers as subprocesses. Accepting a
customer-supplied command would let any `connectors:write` holder execute arbitrary binaries
in the Aurora container. Remote-only also deletes the entire problem class that dominates
`mcp_tools.py`: `RealMCPServerManager` spends roughly 400 of its 650 lines on `Popen`,
per-type threading locks, dead-process detection, restart-on-broken-pipe and `atexit`
cleanup. A URL has no lifecycle.

It also sidesteps a pre-existing bug rather than copying it. `self.server_processes` is keyed
by `server_type` (`:101`) on a module-level singleton (`:809`), so processes are shared across
users. For per-customer servers that would be cross-tenant credential bleed. There is no
process, so there is no shared process.

**2. Use the installed SDK, do not hand-roll JSON-RPC.** `mcp` is already a dependency.
`streamablehttp_client` + `ClientSession` handles the handshake, SSE framing, session IDs and
protocol negotiation. The existing stdio code hand-rolls all of this (`send_mcp_message` at
`:340`, plus speculative retries against invented method names like `tools.list` and
`call_tool` at `:545` and `:593` — those are not in the spec and should not be reproduced).

**Connection per call.** No pooling, no warm sessions, no locks. Costs one handshake
round-trip per tool call; buys the deletion of all lifecycle code.

```
# ponytail: one HTTP handshake per tool call, no session reuse. Ceiling is added
# latency on chatty multi-call turns. Upgrade path: cache the ClientSession per
# (user, server) behind the existing _langchain_tools_cache TTL.
```

## Storage: one Vault blob holding a list

Reuses the decision already made in `docs/multi-datadog-plan.md`, for the same reason.

`store_tokens_in_db` is a ~480-line if/elif chain where every branch ends in
`ON CONFLICT (org_id, provider) DO UPDATE` — one secret per `(org, provider)`. N servers per
org does not fit that shape, and adding an account axis to the shared function means touching
roughly 30 connectors.

So: provider key `"mcp"`, one secret, a list inside it.

```
{
  "servers": [
    {
      "label": "netbox",
      "url": "https://mcp.internal.example.com/mcp",
      "auth": {"type": "bearer", "token": "..."},
      "transport": "streamable_http",
      "read_only": true,
      "tools": [{"name": "...", "description": "...", "inputSchema": {...}}],
      "allow_in_background": ["list_zones", "get_analytics"],
      "validated_at": "..."
    }
  ]
}
```

What this buys:

- **Zero changes to `token_management.py`.** An unrecognised provider falls through to the
  generic `else` at `:452`, which inserts only `(user_id, org_id, secret_ref, provider)`.
  Exactly the columns needed. No new branch in the chain.
- No migration, no new table, no schema change.
- Credentials never leave Vault. The `user_tokens` row holds a secret reference only.

Identity is the **label**, slugified, unique per org. Tool names are namespaced
`mcp_<label>_<tool>` so two servers exposing `search` do not collide, and neither collides
with the existing unprefixed `mcp_<tool>` names from the built-in servers.

Caching the discovered `tools` in the blob is what makes tool assembly cheap: building the
agent's toolset is then a Vault read, not N network handshakes on every turn. Re-discovery is
explicit (a refresh button) or on registration.

## Guardrails

Four distinct risks. The third is the only one needing new logic.

### SSRF, and the self-hosted tension

A customer-supplied URL fetched server-side is an SSRF vector into the Aurora deployment —
cloud metadata at `169.254.169.254`, internal services, the Vault container. The repo already
has the right check: `_assert_public_url` in
`server/chat/backend/agent/tools/notion/workspace.py:382-414` resolves the host and rejects
private, loopback, link-local, reserved, multicast and unspecified addresses.

The tension is that **a self-hosted Aurora runs inside the customer's own network**, so their
MCP servers plausibly sit on private addresses that are legitimately reachable and legitimately
theirs. A blanket block ships a feature self-hosted installs cannot use — which is the
requesting customer's situation today (AKS).

Resolution: `MCP_ALLOW_PRIVATE_TARGETS`, server-side only, default `false`.

| Deployment | Setting | Rationale |
|---|---|---|
| Aurora SaaS | `false` | A tenant must not aim Aurora at Arvo's internal network |
| Self-hosted | `true` | The cluster is the customer's; private targets are the point |

Not `NEXT_PUBLIC_*` — the frontend has no business reading it. Needs adding to
`docker-compose.yaml`, `docker-compose.prod-local.yml` and `.env.example` together.

Also: resolve-then-connect is a TOCTOU window (DNS can change between check and request).
Accepted, noted, same as the existing Notion path.

### Credential handling

Headers only, built per call, never persisted outside Vault, never logged. Pass through
`sanitize`/`hash_for_log` from `utils.log_sanitizer` like every other connector. No
`os.environ` mutation — there is no subprocess to configure, which is most of why this is
easy.

Supported auth shapes, deliberately kept to what is protocol-generic rather than
vendor-specific: `bearer` (an `Authorization: Bearer` header), `header` (a caller-named header
and value, which covers proxy-fronted and gateway-fronted servers), and `none`. One token per
registered server, pasted once.

Explicitly **not** doing: mapping a registered URL's host back to an existing Aurora connector
credential to skip the paste. It would make one vendor's first-run nicer at the cost of a
host-to-provider table that rots as both sides change, and every self-built server still needs
the manual path. The generic path is the only path.

### Write gating: invert the heuristic for untrusted servers

This is the load-bearing design call.

The existing `is_destructive_mcp_tool` (`mcp_tools.py:57-66`) is a **denylist** of prefixes
(`create_`, `delete_`, `update_`, `push_`, `merge_`, ...) plus named GitHub tools. It is
tuned to servers whose tool names Aurora's authors have read. Against arbitrary customer
naming it under-detects, and it fails in the unsafe direction: `purge_cache`, `apply`,
`restart_pod`, `scale`, `execute` all match nothing and sail through as read-only.

Two existing behaviours make the under-detection worse than it looks:

- `gate_action` (`utils/auth/command_gate.py:208`) returns `BACKGROUND_DENIED` with no human
  present, so the human gate does nothing at all during RCA. Classification is the only
  control in background.
- `ModeAccessController.filter_tools` (`access/mode_access_controller.py:76-101`) ends by
  **appending any tool it does not recognise**. Unknown custom names are allowed in ask mode
  by default.

So for custom servers, classify by read-prefix **allowlist** and treat everything else as a
write:

| Context | Read-prefixed (`get_` `list_` `search_` `read_` `describe_` `query_` `fetch_` `check_`) | Everything else |
|---|---|---|
| Foreground chat | runs | `gate_action` → human confirms |
| RCA / background | runs | not offered to the agent at all |
| PR review | runs | withheld, matching the existing `is_pr_review` filter at `cloud_tools.py:3037` |

Per-server `read_only` (default **true**) drops non-read tools at discovery, so the agent
never sees them. `allow_in_background` is an explicit per-tool escape hatch for a customer
who knows a differently-named tool is safe.

Honest limit, stated for the customer-facing doc: Aurora cannot know a third-party tool's
side effects. Name-based classification is a backstop, not a guarantee. The real control is
the customer scoping the credential they hand Aurora. Say so in the UI next to the token
field.

### Prompt injection

Tool names and descriptions come from the customer's server and land in the system prompt.
A compromised MCP server can inject instructions. Mitigations that cost nothing: cap tool
count per server (25) and description length (1 KB), namespace every name under
`mcp_<label>_`, and keep the write gating above. Worth one line in the risks section of the
customer doc; not solvable here.

## Changes

### Backend

**1. `server/requirements.txt:113`** — `mcp[cli]>=1.27.0,<2`. Prerequisite, see above.

**2. `server/connectors/mcp_connector/client.py`** — new, ~90 lines. The only module that
touches the protocol.

| Function | Purpose |
|---|---|
| `assert_allowed_target(url)` | SSRF check honouring `MCP_ALLOW_PRIVATE_TARGETS` |
| `probe(url, auth, transport)` | handshake + `list_tools`, returns the tool list or raises |
| `call(url, auth, transport, tool, args)` | one `call_tool`, returns flattened text |

Async internally (the SDK is anyio-based); callers use the existing `run_async_in_thread`
helper (`mcp_tools.py:946`). SSE fallback is one `except` branch retrying with `sse_client`,
for servers that predate Streamable HTTP.

**The fallback must trigger on transport errors only, never on an auth failure.** A 401 from a
wrong token is not a transport problem, and retrying it over SSE converts a clear "bad
credential" into a misleading "could not connect". Hosted servers increasingly serve their
legacy `/sse` paths with a Streamable HTTP handler anyway, so the fallback is for genuinely old
servers, not a general retry.

**3. `server/routes/mcp/mcp_routes.py`** — new, ~140 lines. All on `@require_permission`,
`user_id` as first positional arg.

| Route | Permission | Behaviour |
|---|---|---|
| `GET /mcp/servers` | `connectors:read` | list, credential-free |
| `POST /mcp/servers` | `connectors:write` | validate URL, probe, store on success only |
| `POST /mcp/servers/<label>/refresh` | `connectors:write` | re-probe, update cached tools |
| `DELETE /mcp/servers/<label>` | `connectors:write` | remove one; last one removed deletes the secret |
| `GET /mcp/status` | `connectors:read` | `connected` = any server present |

`POST` **never stores an unvalidated server** — a failed handshake returns the error to the
UI so the customer fixes the URL or token immediately rather than discovering it mid-incident.

**4. `server/utils/secrets/secret_ref_utils.py:38`** — add `"mcp"` to
`SUPPORTED_SECRET_PROVIDERS`.

**Without this the feature is silently dead.** `get_user_token_data` (`:204-208`) and
`has_user_credentials` (`:174-175`) both open with:

```python
provider_base = provider.lower().split('_')[0]
if provider_base not in SUPPORTED_SECRET_PROVIDERS:
    return None
```

`store_tokens_in_db` has no such check and writes fine. `delete_user_secret` (`:362`) has no
such check and deletes fine. So registration returns 200, the server appears in the DB and in
Vault, disconnect works — and every read returns `None` with no error logged at any level. The
UI would look correct and the agent would have zero tools.

Note the `split('_')[0]`: this is why the provider key is `"mcp"` and not `"custom_mcp"`, which
would require the token `"custom"` in a list of cloud-provider names. The existing `"google"`
entry at `:71` documents the same gotcha for `google_chat`.

**5. `server/utils/providers.py:11`** — add `"mcp"` to `CONNECTOR_DIRS`. Note
`test_connector_rbac.py:20` imports that frozenset, so unlike what the CLAUDE.md checklist
implies, this is one edit, not two.

**6. `server/main_compute.py`** — register the blueprint at `url_prefix="/mcp"`.

**7. `server/chat/backend/agent/tools/custom_mcp_tools.py`** — new, ~110 lines.

- `is_mcp_connected(user_id)` — for the skill's `connection_check`
- `get_custom_mcp_tools(user_id, is_background, is_pr_review, ...)` — reads the blob, applies
  the classification table, builds `StructuredTool`s

Reused as-is, not reimplemented:

| Reused | From |
|---|---|
| `extract_mcp_tool_schema` — JSON Schema → Pydantic | `mcp_schema_extractor.py:51`, already provider-agnostic |
| `gate_action` | `utils/auth/command_gate.py:208` |
| `cap_tool_output` | `chat/backend/agent/utils/tool_output_cap.py:22` |
| `send_tool_start` / `_completion` / `_error` | passed in as today |

Also in this module: **tool-name sanitisation**, which does not currently exist anywhere.

The existing code does `name=f"mcp_{tool_name}"` (`mcp_tools.py:1560`) with no length or
charset check, because the three built-in servers have known-good names. Anthropic and OpenAI
both require tool names to match `^[a-zA-Z0-9_-]{1,64}$`, and a violation **fails the entire
API request**, not just that tool — so one badly named customer tool takes down every agent
turn for that user until they disconnect.

`mcp_<label>_<tool>` reaches the limit easily: label `observability-staging` plus tool
`get_zone_analytics_by_dimension` is 57 characters before the `mcp_` prefix. So: slugify the
label to `[a-z0-9-]`, cap the combined name at 64 by truncating the tool segment and appending
a short hash for uniqueness, and reject a server at registration if two of its tools collapse
to the same name. `introspection_tools.py:666` already uses exactly `^[a-zA-Z0-9_-]{1,64}$`
for agent IDs — same constant, same reason.

**Not** reused: `create_mcp_langchain_tools` (`mcp_tools.py:1110`). It is 470 lines whose
control flow is a hardcoded `if "aws" ... elif github ... elif context7 ... else: continue`
chain over a 140-entry GitHub allowlist, and it `continue`s on anything unrecognised
(`:1490`) — a custom tool would be silently dropped. Threading a fourth case through it
risks the three working connectors for no gain. A separate small builder is both smaller and
lower-risk.

**8. `server/chat/backend/agent/tools/cloud_tools.py` ~`:3060`** — one block after the
existing MCP block, same shape as the ~20 other `is_X_connected` gates in that function.
Uses the already-computed `is_background` and `is_pr_review` (`:1082`, `:1085`), both of which
are already in the toolset cache key at `:1093` — so a background toolset cannot be served
from a foreground cache entry, and the write-gating table below holds.

**9. `server/chat/backend/agent/skills/integrations/mcp/SKILL.md`** — new.
`connection_check` = `is_connected_function` → `is_mcp_connected` (the module prefix
`chat.backend.agent.tools.` is already on the allowlist at `registry.py:226-230`).

`rca_priority: 5`. Skills load in priority order until the 12,000-token RCA budget is
exhausted (`registry.py:441-451`), and the first-party telemetry connectors occupy 1-4
(datadog, elastic, splunk, sentry, github at 2-3). A higher number risks being cut before it
loads on an org with ten integrations connected — which would be backwards, since these servers
are precisely the telemetry for systems Aurora has no connector for. 5 puts them after
first-party telemetry and ahead of the secondary cloud skills at 8-10.

The static `tools:` frontmatter cannot enumerate per-user tools, so the body carries a
`{{mcp_servers_section}}` template variable rendered from the blob. `resolve_template` and
the context dict already support this; the hook is one entry in
`SkillRegistry._build_rca_context` (`registry.py:505`).

**10. `server/routes/connector_status.py`** — one `_check_mcp` reading the blob, added to the
`PROVIDER_CHECKERS` dict at `:828` alongside `_check_datadog` and `_check_elastic`, so the
connector card and `_count_connected_connectors` (~`:855`) report MCP. Local read, no network
call, unlike the other checks in that file.

This single entry also covers `/api/connected-accounts`:
`account_management.py:27-42` delegates to the same `PROVIDER_CHECKERS` dict, and its
`checker is None` branch returns `True` — meaning without an entry MCP would show as connected
without validation. One function, both call sites, no duplication.

Not needed: anything for `get_connected_providers` (`stateless_auth.py:502`). It reads
`user_tokens` generically, so `"mcp"` appears there as soon as the row exists.

### Frontend

**11. `client/src/components/connectors/ConnectorRegistry.ts`** — one `register({...})` with
`id: "mcp"`, `category: "Infrastructure"`, `path: "/mcp/auth"`,
`storageKey: "isMcpConnected"`, `useCustomConnection: true` (the flag Datadog uses at `:37`).

`category` is a free-form `string` in `types.ts`, and `ConnectorsClient.tsx:82-88` derives the
filter tabs from the distinct values present. So inventing `"Other"` would silently add a tab
containing one card — reuse an existing category instead.

**12. `client/src/lib/services/mcp.ts`** — new, ~60 lines. Typed client for the five routes.

**13. `client/src/app/mcp/auth/page.tsx`** — new, ~140 lines. Add form (label, URL, auth type,
token), list of servers with discovered tool counts and a read-only badge, per-server refresh
and remove. Modelled on the Elastic auth page, not the AWS onboarding page — the latter
carries ~26 state variables and is the wrong template.

**14. `client/src/app/api/mcp/[...path]/route.ts`** — new, ~35 lines, `forwardRequest` from
`@/lib/backend-proxy`. One catch-all rather than five files. Required by the client/backend
boundary rule; the browser never reaches Flask directly.

### Test

**15. `server/tests/chat/test_custom_mcp.py`** — pure functions, no network, no fixtures:

- `assert_allowed_target` rejects `169.254.169.254`, `127.0.0.1`, `10.x`; accepts them with
  `MCP_ALLOW_PRIVATE_TARGETS=true`
- read-prefix classification: `list_zones` read, `purge_cache` write (the case the existing
  denylist gets wrong)
- background filtering drops writes; `allow_in_background` re-admits a named tool
- blob round-trip: two servers, label collision rejected, removing the last deletes the secret
- `is_mcp_connected` false on empty blob, true with one server
- tool names come out as `mcp_<label>_<tool>`, match `^[a-zA-Z0-9_-]{1,64}$`, and stay unique
  and within 64 chars for a long label plus a long tool name
- `"mcp" in SUPPORTED_SECRET_PROVIDERS` — a one-line guard against the silent-death failure in
  item 4, which no other test would catch

### Documentation

**16. `server/connectors/mcp_connector/README.md`** and the public mirror in
`website/docs/integrations/connectors.md`. Must state: supported transports, that stdio is
not supported and why, the read-only default, the name-based classification limit, and the
instruction to scope the token on their side.

## Risks

1. **Name-based write classification is a heuristic.** A customer tool called `apply` reads
   as a write (safe, annoying); one called `get_and_reset_counters` reads as a read (unsafe).
   Mitigated by the read-only default and by documenting that credential scoping is the real
   control. Not solved.
2. **Unpinning `mcp` has already broken `FastMCP` on fresh builds.** Pinning `<2` fixes it but
   defers a migration that gets more expensive. Worth its own ticket after Friday.
3. **Per-call handshake latency.** An agent turn making six calls to one server pays six
   handshakes. Acceptable at single-digit server counts; the cache upgrade path is noted inline.
4. **Prompt injection via tool descriptions.** Capped and namespaced, not eliminated.
5. **`ModeAccessController` default-allows unknown names.** Custom tools reaching ask mode
   rely on the new classification rather than on that filter. If someone later refactors the
   classification out, ask mode silently loosens. The test covers it.
6. **Discovery drift.** Cached tool lists go stale when the customer redeploys their server;
   a removed tool errors at call time. Refresh is manual by design — automatic re-probing on
   every turn is the latency cost this plan avoids.
7. **Reachability, not Aurora, will be the first support ticket.** A server on an address
   Aurora cannot route to fails at registration with a connection error. That is the correct
   behaviour and the DEV-1600 Phase 1 caveat, but it will read to the customer as a bug.
8. **A malformed tool name from a customer server can break every agent turn.** Model APIs
   reject the whole request on an invalid tool name, not just the offending tool. Sanitisation
   in item 7 is therefore not cosmetic; it is the difference between one broken tool and a
   user who cannot chat at all. Covered by the test.
9. **Vault blob size.** Caching full `inputSchema` JSON for up to 25 tools across several
   servers could reach tens of KB in one secret. Vault's default `max_entry_size` is 1 MB, so
   there is headroom, but store descriptions and schemas only — not example payloads.

## Deliberately not touched

`server/chat/backend/agent/tools/mcp_preloader.py` warms the built-in MCP tool cache on a
5-minute loop. It is **not** extended to custom servers: a background loop handshaking into
customer networks every 5 minutes per active user is an unnecessary traffic pattern to explain
in a security review, and discovery is already cached in the blob so there is nothing to warm.
Worth a sentence in the PR description since a reviewer will reasonably ask.

## Known ceilings

One Vault blob per org means no per-server key rotation and no per-server RBAC; the blob grows
with server count. Upgrade path, if ever needed, is the same as the Datadog plan's: rows in
`user_connections` plus one Vault secret per server.

## Size

Roughly 420 lines backend across three new files plus seven small edits, 240 lines frontend
across three new files plus one edit, one test file, two doc files. One dependency pin.

Three of the seven backend edits are one-liners (`SUPPORTED_SECRET_PROVIDERS`,
`CONNECTOR_DIRS`, the blueprint registration) and are the easiest to forget. The first one
fails silently.
