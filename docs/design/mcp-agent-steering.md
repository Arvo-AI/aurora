# MCP tool discovery & steering

**Status:** implemented (branch `sms10221/dev-1208-mcp-interaction-is-bad`)
**Scope:** MCP surface + steering only — no new backend endpoints, no change to the
security allowlist boundary.

## Problem

External agents (Cursor, Claude Desktop/Code, Codex, Windsurf) connect to Aurora's
self-hosted MCP server (`aurora-mcp`, :8811) and:

1. **Over-use `chat_with_aurora` / `ask_incident`** for things a cheap direct read
   could answer. The steering text was chat-maximalist in three layers — the server
   `instructions` string, the `chat_with_aurora` docstring ("Default tool for any
   question…"), and the gated-tool descriptions ("for investigations prefer
   `chat_with_aurora`") — so the model funnelled everything into chat.
2. **Fail to discover obvious read-only tools.** `get_infrastructure_context`, the
   service graph, DORA metrics, postmortems, and the agent-execution timeline were
   all Tier-3 (dispatch-only), reachable only if the agent first called
   `search_tools` — and nothing told it to.
3. **The exact data the screenshot needed wasn't on the MCP surface at all.** The
   per-sub-agent RCA findings (`tools_used`, `citations`, `tool_call_history`) exist
   as REST endpoints but weren't exposed via MCP.

The tiered design itself is correct (semantic `search_tools` for the long tail keeps
the upfront surface token-lean and under client tool-count caps). We keep the
architecture and fix this with **(a) re-steering the prompt text**, **(b) promoting a
curated set of high-value reads to the always-visible surface**, and **(c) fixing the
`search_tools` matcher** so the remaining long tail is findable.

## Cross-client steering mechanics (verified)

- **Per-tool `description` is the primary, cross-client-reliable steering channel.**
  Every client loads tool descriptions upfront, so the routing rubric must live
  there.
- **The server `instructions` string is spec-OPTIONAL** ("MAY", MCP spec 2025-06-18).
  Honored by **Claude Code** (primary routing signal, 2KB truncation) and **Codex**
  (front-load first ~512 chars). **Cursor, Windsurf, VS Code Copilot, Cline, Zed** —
  unverified; do not rely on it. Treated as a bonus, not the load-bearing path.
- **Tool-count caps** (tools silently dropped past the cap; no lazy loading except
  Claude Code): Cursor **40**, Windsurf **100**, VS Code Copilot **128**. Aurora's
  upfront surface goes from ~16 → ~22 tools with the 6 promotions — safely under
  every cap, leaving room for the user's other servers (the 40-tool cap is shared).
- **`search_tools` / `call_tool` is hand-rolled** (plain JSON, not MCP
  `tool_reference` blocks) so it works in every client regardless of client-side
  tool-search support. The agent learns to use it from the *descriptions*.

## Changes

### 1. Re-steer the prompt text (chat-first → direct-read-first)

Routing rubric, now explicit everywhere: **direct read for factual lookups;
`search_tools` to find a direct tool that isn't visible; `chat_with_aurora` only for
open-ended multi-source investigation/synthesis (heavier/slower).**

- **`server/mcp_server.py` `instructions`** — replaced the "prefer chat" text with a
  routing guide naming the direct-read categories, telling the agent to call
  `search_tools` for tools not shown upfront, and framing `chat_with_aurora` as the
  escalation path. Critical routing sentence front-loaded in the first ~512 chars
  (Codex); total 742 bytes (under the 2KB Claude Code cap). Scrubbed the phantom
  `github_rca` the old string name-dropped.
- **`chat_with_aurora` docstring** — dropped "Default tool for any question";
  reframed as the agentic investigator for open-ended/multi-source work; added a
  scope/limits note that it is **NOT a structured-data lookup** (to list/fetch
  incidents incl. the most recent via `list_incidents(limit=1)`, alerts, topology,
  metrics, or a postmortem, call the direct tool). Kept the load-bearing SESSION
  THREADING block; folded the verbose "Concretely" examples into `Args`. Cleaned
  docstring is 1752 bytes (under the 2KB cap that was truncating the old 2065-byte
  docstring).
- **`ask_incident` docstring** — points at `get_incident` / `incident_findings` /
  `incident_finding_detail` / `incident_list_alerts` for factual lookups first; kept
  for genuine free-text follow-ups.
- **Tier-2 descriptions** (`query_logs`, `query_metrics`) — softened so they no
  longer redirect raw-data requests into chat.
- **`search_tools` docstring + output `hint`** — made discovery loud: states plainly
  that Aurora exposes many more tools than shown upfront, enumerates the families
  (logs, metrics, traces, deployments, Jira, GitHub, Sentry, Grafana, postmortems,
  DORA metrics), and directs the agent to "search before assuming a capability is
  missing or defaulting to chat_with_aurora."
- **`prompts.py`** — `investigate_incident` reordered to direct reads first
  (`get_incident` → `incident_findings` → only then chat); `blast_radius_analysis`
  repointed from `call_tool('graph_service_impact', …)` to the new first-class
  `service_impact` tool.

### 1b. Fix the `search_tools` matcher

The old `_entry_matches_search` matched the entire query as one underscore-joined
substring (`q.replace(" ", "_")`), so a natural-language search like
`"rca tools steps"` → `"rca_tools_steps"` matched nothing — the agent got an empty
result and fell back to chat, defeating the re-steer.

The matcher now tokenizes the query on whitespace, drops tokens `<3` chars, and
matches an entry if **any** token is a substring of its name or description; entries
are ranked by number of matching tokens (more first), with the original allowlist
order as a stable tie-breaker. Empty query keeps the prior behavior (all entries up
to `limit`). `search_dispatch_entries` collects all matches, sorts, then truncates
(it can no longer break early at `limit`); the dispatch layer still re-partitions
visible-first.

### 2. Promote 6 curated reads to Tier 1

Registered as first-class `@mcp.tool()` functions in `tools_always_on.py` (typed
params + rich docstrings that load upfront in every client):

| New Tier-1 tool | Backend (GET) |
|---|---|
| `get_infrastructure_context` | `/api/graph/infrastructure/context` |
| `list_services` | `/api/graph/services` |
| `service_impact` | `/api/graph/services/{name}/impact` |
| `incident_findings` | `/api/incidents/{incident_id}/findings` |
| `incident_finding_detail` | `/api/incidents/{incident_id}/findings/{agent_id}` |
| `incident_list_alerts` | `/api/incidents/{incident_id}/alerts` |

`get_infrastructure_context`, `graph_list_services`, and `graph_service_impact` were
**removed** from `DISPATCH_ALLOWLIST` to avoid double-exposure (the rest of the graph
family — `graph_get_full`, `graph_get_service` — stays in dispatch). Findings are
brand-new (first-class only, no dispatch entry). **DORA metrics and postmortems are
deliberately NOT promoted** — they stay in `search_tools`.

`service_impact` URL-encodes `name` itself (`urllib.parse.quote(name, safe="")`)
since first-class helpers bypass dispatch's `_build_path`/`quote` and `_api` only
blocks `..`, not arbitrary chars like `/` or spaces.

### 2b. Phantom allowlist entry removed

`github_list_repos` → `GET /github/repos` 404'd (the github blueprint only defines
`/user-repos`, `/user-branches/...`, `/repo-selections`). Removed; it duplicated the
working `github_list_user_repos` → `/github/user-repos`.

### 2c. Connector coverage — CI/CD + Sentry + Grafana reads

Jenkins, CloudBees, Spinnaker, Sentry, and Grafana had zero references in the MCP
surface despite having RBAC-decorated read endpoints. Added 11 connector-gated
`DispatchEntry` rows (each gated by its skill, so they only appear once connected —
no upfront bloat):

- `jenkins_list_deployments`, `cloudbees_list_deployments`,
  `spinnaker_list_deployments` / `spinnaker_list_applications` /
  `spinnaker_list_pipelines` / `spinnaker_list_pipeline_configs` /
  `spinnaker_app_health` (category `cicd`)
- `sentry_list_projects`, `sentry_list_issues`, `sentry_list_events`
  (category `monitoring`)
- `grafana_list_alerts` (category `alerts`)

All GET reads. The Spinnaker pipeline-trigger POST is intentionally excluded
(write, out of the read-only posture). Deferred for a later pass: agent
monitor/waterfall, actions/runbooks, postmortem version history.

## Latency

The customer "MCP feels slow" complaint traces to the agent **defaulting to
`chat_with_aurora`**, which runs the full background agent workflow and polls
~40–45s before returning. A direct read (`get_incident`, `list_incidents`, the
promoted graph/findings/alerts tools) is a single proxied GET — sub-second. **The
re-steer + promotion is itself the latency fix:** common factual queries that took
~40s via chat drop to ~1s via a direct tool. We do not change chat's inherent
polling (it's the agent runtime). No other hotspot in the MCP path: the shared
`httpx` client uses keepalive pooling, token resolution is cached 60s, connector
status 30s.

## Testing

- **Unit/registry** (`server/tests/mcp/`): the 6 promoted tools are first-class
  Tier-1; the 3 graph/infra entries and the phantom `github_list_repos` are removed
  from the allowlist; the 11 new connector entries are present and skill-gated; the
  tokenized matcher handles multi-word queries while preserving single-keyword and
  empty-query behavior; `service_impact` URL-encodes the name.
- **Live MCP end-to-end**: mint a throwaway `mcp_tokens` row for a real
  `user_id`/`org_id`, drive `http://localhost:8811/mcp` with `Authorization: Bearer`,
  confirm `list_tools` includes the 6 promoted tools directly and they return real
  data; spot-check metrics/postmortems and the new CI/CD/Sentry/Grafana entries via
  `search_tools` + `call_tool`.
- **Tool-selection (hit-rate) eval**: feed the rendered tool list to an LLM and score
  the tool it picks per SRE prompt, run twice (with `instructions`, and
  descriptions-only). Success = every non-chat prompt routes to its direct/connector
  tool and only the open-ended "dig into it" escalates to chat.
- **Latency check**: time a direct promoted tool vs an equivalent `chat_with_aurora`
  call to quantify the win.
