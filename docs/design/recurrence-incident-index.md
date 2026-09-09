# Recurrence Detection via a Memory-Backed Incident Index

**Status:** Proposed (design review before implementation)
**Branch:** `feat/port-recurrence-search-to-memory`
**Author/date:** design agreed 2026-09-09

## Problem

Recurrence detection (root-cause dedup layer 1) decides whether a just-finished
incident is a recurrence of a past one, then folds them (`incidents.recurrence_of_incident_id`)
so Slack threads and UI group together.

The original implementation used Weaviate for semantic RCA search. The
`feat/memory-system` branch removed Weaviate. The interim port
(`_search_similar_rcas_impl`) re-implemented search as an on-the-fly scan of the
`incidents` table scored with `SimilarityStrategy`. That has two problems:

1. **Cost blows up with history.** It scores up to 200 candidates per check, and
   `SimilarityStrategy.score()` embeds *both* the query and each candidate every
   call — ~400 embedding calls per check, re-embedding the query 200×. Grows with
   table size.
2. **Representation doesn't scale in-prompt.** Real data (151 incidents):
   `aurora_summary` averages ~1,456 chars (~365 tokens), p95 ~4,573, max ~7,308.
   Full summaries fit only ~40 in a manifest before overflow; titles alone
   under-discriminate (the prompt itself warns "same monitor ≠ same cause").

## Approach: a self-maintaining Incident Index (memory artifact)

Mirror the memory system's proven pattern (index → LLM selects → drill in).
Keep **one running artifact per org** — the *Incident Index* — a compact,
deduplicated, high-level list of recent/relevant incidents. It behaves exactly
like the memory index that is already injected into prompts today: a high-level
map the agent scans, then drills into interesting entries via `get_incident`.
The authoritative fold and all Slack threading stay DB-keyed and unchanged.

### Index shape
- Stored as a memory artifact: `category="incident_index"` (new agent-only
  category), `title="Incident Index"`, one per org (unique on org+category+title).
- Content = compact lines, one per incident (or per recurring cluster), each
  carrying the **`incident_id`** as the join key back to the DB:

  ```
  - [INC 4f2a… | 2026-09-08 | payments-api | resolved] DB connection pool exhaustion under load spike
    ↳ recurrences: 9c1b…, 22df… (3 total, last 2026-09-08)
  ```
- The `incident_id` lets the agent call `get_incident(id)` for the full
  conclusion, and lets the fold reference a real row.

## Who writes it: deterministic append at RCA completion (no second agent)

The synopsis line is written **deterministically in `summarization.py`**, at the
point where the RCA `aurora_summary` is produced — that path already has the
`incident_id`, the summary, and the title/service. This was chosen over
"extend the collector prompt" because:

- The memory **collector** (`extract_memories_from_session`) runs for *every*
  chat session, not just RCAs, and is only handed `session_id`/`user_id` — it
  does not know the `incident_id`. Making it maintain the index would be
  fragile (session→incident resolution) and LLM-dependent (unreliable coverage).
- The summarization path runs exactly once per completed incident and already
  has everything needed. Coverage is guaranteed and deterministic; no extra LLM
  round-trip (the synopsis is derived from the alert title, falling back to the
  first sentence of the summary — see `build_synopsis`).

The append happens **after** the recurrence check for the current incident, so
an incident is never a candidate for its own check but is available to future
ones. It is best-effort and never blocks the notify path.

## Who keeps it lean: the existing consolidation action

Extend the nightly `memory_consolidation` action prompt
(`DEFAULT_MEMORY_CONSOLIDATION_INSTRUCTIONS`) to also groom the Incident Index:
collapse recurring clusters into one line (40 related → 1 line), mark solved,
drop stale/closed groups, and enforce a hard cap (keep the most recent/relevant
N — target ≤ ~150 lines). This is where "40 related incidents take very little
space" happens. No new scheduler — reuses the action that already exists. The
index is agent-only (not in `MEMORY_CATEGORIES`), so `list_memories` does not
surface it; the prompt names the exact category/title so the groomer can
`read_memory` / `edit_memory` it directly.

## Retrieval (recurrence agent)

The recurrence agent (`recurrence_agent.py`) is a constrained sub-agent. The
candidate map is injected **deterministically into its input block** (like the
existing recent-incidents section), not fetched by a tool call:

1. **Read the index:** `_fetch_incident_context` calls
   `services.memory.incident_index.read_index(user_id)` — one artifact read,
   bounded to `INDEX_INJECTION_CHAR_BUDGET` (~24k chars ≈ 6k tokens), no
   embeddings. Rendered by `_incident_index_lines` as the primary candidate map.
2. **Fallback (bounded):** the existing recent-incidents section
   (`_recent_incidents_lines`, `RECENT_CANDIDATES_LIMIT = 15`, last
   `GROUP_IDLE_HOURS`) remains and doubles as the cold-start fallback — one line
   per row (id | date | service | title), never full `aurora_summary`. It is
   also the joinability guard (only these groups can actually be folded).
3. **Drill in:** the agent confirms candidates with the existing read-only
   `get_incident` / `list_incidents` tools.
4. **Verdict:** unchanged — `submit_correlation_verdict`.

This replaces the embedding-loop `_search_similar_rcas_impl` and its
`make_search_similar_rcas_tool`, both **removed**.

## Fold + Slack threading: UNCHANGED (stays DB-keyed)

The index is a *discovery map only*. Once the agent names an anchor `incident_id`:
- `recurrence_fold` sets `incidents.recurrence_of_incident_id` (authoritative).
- `dispatcher._get_incident_data` resolves the anchor row's `slack_message_ts`.
- `slack_threading` posts the recurrence under the anchor's thread.

The index MUST NOT become the source of truth for Slack routing — that remains
`recurrence_of_incident_id` → anchor `slack_message_ts`.

## Why this is efficient
- Retrieval = one `read_memory` of a compact artifact. No per-candidate
  embedding, no growth with table size.
- Index size is bounded by grooming (hard cap), not raw history.
- Reuses tested primitives and agents: the collector, the consolidation action,
  the existing `write_memory`/`append_to_memory`/`get_incident`/`list_incidents`
  tools, and the index→drill-in pattern.

## Tooling / helpers
- **No new agent write tools.** The consolidation agent grooms the index with
  its existing `read_memory` / `edit_memory` / `write_memory` tools.
- **One small module** — `services/memory/incident_index.py`:
  - `build_synopsis()` — deterministic, no LLM.
  - `format_index_line()` — canonical id-keyed line.
  - `append_incident_line()` — idempotent by `incident_id`, best-effort, never
    raises (used by `summarization.py`).
  - `read_index()` — bounded read (used by the recurrence agent's context fetch).
- `get_incident` and `list_incidents` already exist (`cloud_tools.py`) — no new
  drill-in tools.

## Tradeoffs / risks
- **Coverage is deterministic** — the append is in the summarization path, so a
  collector outage doesn't lose incidents.
- **Grooming lag:** between grooms the index may hold raw (un-clustered) lines;
  acceptable — still a valid candidate map, and `read_index` hard-caps injection.
- **Cold start:** handled by the bounded recent-incidents fallback.
- **Synopsis quality:** derived from the alert title / first summary sentence; if
  both are empty, falls back to the service name (line still id-keyed).

## Task breakdown (implemented on `feat/recurrence-incident-index`)
1. Add `incident_index` to `AGENT_CATEGORIES`; canonical category/title
   constants in `services/memory/__init__.py`.
2. `services/memory/incident_index.py`: synopsis/line/append/read helpers.
3. `summarization.py`: deterministic `append_incident_line` after the recurrence
   check (best-effort, never blocks notify).
4. Rework `recurrence_agent` retrieval: inject the index via `read_index` +
   `_incident_index_lines`; keep the recent-incidents fallback; delete
   `_search_similar_rcas_impl` / `make_search_similar_rcas_tool`.
5. Extend `DEFAULT_MEMORY_CONSOLIDATION_INSTRUCTIONS` to groom the index
   (cluster, mark solved, trim to cap, preserve `INC <id>`).
6. Fix stale `search_similar_rcas` / `knowledge_base_search` references in
   `recurrence_prompt.md`.
7. Tests: `test_incident_index.py` (synopsis, formatting, idempotent append,
   bounded read); updated `test_recurrence_candidates.py` (index section +
   context wiring). Fold/threading tests unchanged.

## Non-goals
- No changes to alert correlation (`AlertCorrelator`) — separate subsystem,
  never used Weaviate.
- No pgvector / new infrastructure.
- No change to fold or Slack-threading mechanics.
- No second recurrence/index agent — the write is a deterministic call in the
  existing summarization path; grooming reuses the existing consolidation action.
