# Aurora Cost Optimization Plan (data-driven)

> Rewritten against **real prod data** pulled from `llm_usage_tracking` (GKE Autopilot cluster `aurora-prod`, 63 orgs) on 2026-06-30.

## The one fact that matters


| Cost center                   | Monthly run-rate                                            | Share |
| ----------------------------- | ----------------------------------------------------------- | ----- |
| **LLM API**                   | **~$10,600/mo** (last 30d = $11,403; last 7d = $2,471)      | ~97%  |
| Infra (Autopilot pod compute) | ~$100/mo (+ Cloud SQL/Redis/LB/egress → low hundreds total) | ~3%   |


LLM is **~20-30x** infra. **Infra optimization is a rounding error — ignore Tier 2/3 from the old plan for now.** Every dollar of effort goes to LLM tokens.

### Where the LLM money actually goes

By model (all-time, $13,753 total):


| Model                                | Cost           | Share |
| ------------------------------------ | -------------- | ----- |
| `claude-opus-4.6`                    | $11,336        | 82%   |
| `claude-opus-4.8`                    | $2,126         | 15%   |
| `claude-sonnet-4.6`                  | $213           | 1.5%  |
| everything else (gemini, gpt, haiku) | <$100 combined | <1%   |


**97% of all spend is Opus.** By request type: `agent_workflow` $11,574 (84%), `tool_output_summarization` $1,718 (12.5%), `visualization_extraction` $342, `suggestion_extraction` $33.

Note: `agent_workflow` includes **prediscovery** — $2,944 all-time (~21% of total) across 671 sessions at ~$4.39/session.

### Root cause (verified in prod env)

Prod has `**MAIN_MODEL=anthropic/claude-opus-4.8**` and `**RCA_OPTIMIZE_COSTS=false**`. Since every auxiliary task (`tool_output_summarization`, `visualization_extraction`, `suggestion_extraction`, `incident_*_summary`) falls back to `MAIN_MODEL`, **all of it runs on Opus** — the most expensive model — including mechanical tasks that don't need reasoning.

### The single biggest mechanic

`agent_workflow` on Opus: **2.18B input tokens vs 20M output tokens.** Input is **96% of agent cost**. Average **58,607 input tokens per call** across 37,259 calls. That's the system prompt + tool schemas + accumulated context being **re-sent and re-billed at Opus input rates ($0.005/1K) on every turn**, with no prompt caching (verified: zero `cache_control` in codebase).

So two levers explain ~97% of the bill: **(A) it's all Opus, (B) the huge input prefix is re-billed every turn uncached.**

---

## Lowest-hanging fruit (in execution order)

### #1 — Turn off the visualization feature (disable, don't delete) — effort S, **savings ~$285+/mo, kills wasted Opus calls**

The infrastructure visualization graph fires an LLM extraction **every ~30s during every RCA** plus once at completion — `visualization_extraction` is 7,601+ calls on Opus. It's pure overhead on the critical path of every incident.

Disable it (keep the code so it can be re-enabled later):

- **Backend (kill the LLM calls):** gate the two trigger sites behind an env flag `VISUALIZATION_ENABLED` (default off):
  - `server/chat/backend/agent/tools/cloud_tools.py:414` — the 30s incremental trigger (`update_visualization.delay(...)`).
  - `server/chat/background/task.py:824` — the final `force_full` trigger (`update_visualization.apply_async(...)`).
  Wrapping both in `if os.getenv("VISUALIZATION_ENABLED","false").lower()=="true":` stops 100% of the spend immediately and is reversible via env.
- **Frontend (remove from UI):** hide the toggle + panel in `client/src/app/incidents/components/IncidentCard.tsx` (the "Visualization" button at lines 679–687 and the `InfrastructureVisualization` block at 873–880). Leave `InfrastructureVisualization.tsx`, `useVisualizationStream.ts`, and the SSE route in place.
- Net: zero viz LLM calls, no UI entry point, fully reversible by flipping the flag + un-hiding the button.

### #2 — Summarize large tool outputs with a cheaper model — effort S (config only, **no code**), **savings ~$1.3–1.7K/mo**

`tool_output_summarization` is **$1,718 on Opus** (6,919 calls) — a mechanical "shrink this log dump" task that does not need a frontier model.

The plumbing already exists and is correct — `TOOL_OUTPUT_SUMMARIZATION_MODEL` and `INCIDENT_REPORT_SUMMARIZATION_MODEL` already read `SUMMARIZATION_MODEL` → fall back to `MAIN_MODEL` (`server/chat/backend/agent/llm.py:57-58`). Nothing is hardcoded. The only reason it lands on Opus is that `**SUMMARIZATION_MODEL` is unset in prod**.

**The only change: set `SUMMARIZATION_MODEL=google/gemini-2.5-flash` (or `claude-haiku-4.5`) in prod** (`helm upgrade --reuse-values --set ...`). Zero code.

This fans out to all 8 lightweight summarization sites at once, moving them off Opus:

- `tool_output_cap.py:65` + `tool_context_capture.py:332` — mid-RCA tool-output capping (the $1,718)
- `summarization.py:608` — initial alert summary (`incident_initial_summary`)
- `chat_context_manager.py:154` — conversation compression when context fills
- `task.py:1869` — severity determination
- `github_repo_metadata.py:171` + `repo_metadata.py:196` — repo metadata (code fetches README/tree; model only summarizes)
- `dispatcher.py:241` — notification report generation

Untouched (correctly stay heavy / separate vars): the deep RCA writeup `incident_rca_summary` uses `EMAIL_REPORT_MODEL`; the agent loop uses `MAIN_MODEL`. gemini-flash ~50x cheaper than Opus, haiku ~5x — both fine for these tasks.

- Validate: `tool_output_summarization` cost/call should drop ~50x.

### #3 — Enable Anthropic prompt caching (via OpenRouter automatic caching) — effort S–M, **savings ~$4–7K/mo (biggest lever)**

The 58K-token/call input prefix is re-billed at full Opus input price every turn. Anthropic cache *reads* are **0.1x base input price**. This is the largest single lever (input = 96% of agent cost, agent = 84% of the bill).

**Approach: use OpenRouter's automatic caching (one field, no per-block markers).** Add a single top-level `cache_control: {"type":"ephemeral"}` on the Anthropic-bound request. OpenRouter/Anthropic auto-place the breakpoint at the last cacheable block and advance it as the conversation grows — no need to manually mark the system prompt or each tool definition. Implement on the OpenRouter path in `agent.py` (and `anthropic_provider.py` for `direct` mode), gated to Anthropic-family models only (no-op for OpenAI/Gemini, which cache automatically).

Caveats that shape the real savings:
- **Routing pin:** top-level `cache_control` is **only supported when OpenRouter routes to Anthropic *direct*** — Bedrock and Vertex don't support it, and OpenRouter will exclude those endpoints when it's present. Acceptable for us (prod is Anthropic models), but it removes Bedrock/Vertex fallback. If cross-provider routing is needed later, switch to explicit per-block breakpoints (work across Bedrock/Vertex).
- **Opus minimum cacheable prefix = 4,096 tokens** (Sonnet 1,024). Our 58K avg is well over, so fine; small calls silently no-op.
- **TTL vs long RCA runs:** default cache TTL is **5 min** (write costs 1.25x input); a **1-hour TTL** is available at 2x write. RCA runs are long/bursty — if gaps between turns exceed 5 min waiting on slow tools, the cache expires and we re-pay the write. So realistic agent-workflow savings are **~50–80%**, not a clean 90%. Use the 1-hour TTL if validation shows low hit-rate.
- **20-block lookback:** if a single turn adds >~20 message blocks (many tool calls), the single auto breakpoint can miss; add an explicit second breakpoint only if hit-rate drops.
- **Deterministic serialization required:** tool-schema/system-prompt JSON must be byte-stable across requests or cache never hits. The existing prefix-cache canonicalization suggests this already holds — verify.

Validate: `cached_input_tokens` in `llm_usage_tracking` should jump from ~0; watch `agent_workflow` cost/call drop and confirm cache hit-rate >70%.

**Is prompt caching the only caching lever?** For LLM *cost*, effectively yes — 96% of spend is input tokens, which is exactly what prompt caching discounts. Other caches are either already done or not cost-relevant:
- *Already in place (no action):* Redis message-serialization cache (5 min), Vault secret cache (5 min, `secret_cache.py`), storage listing cache (60s), and Weaviate vector storage (no re-embedding of unchanged content).
- *Dormant:* `prefix_cache.py` only emits telemetry (`USE_REDIS_PREFIX_CACHE=False`) — #3 is what makes that infrastructure actually pay off.
- *Complementary, but it's "don't call the LLM" not "cache the call":* response/semantic caching of full RCA answers for repeated alerts overlaps with the auto-RCA gate / correlation dedup (#5-style). Avoiding the call entirely beats caching its output, so that's tracked as gating, not caching.

### #4 — Trim prediscovery cost — effort M, **savings ~$1–2K/mo (volume-dependent)**

Prediscovery is **$2,944 all-time (~21% of spend)**, ~$4.39/run × 671 runs. Each run (`run_prediscovery`, `prediscovery_task.py`) is a full autonomous agent session doing live tool calls against one org's integrations — a live multi-turn ReAct loop, so it can't be moved to an async batch queue. Real reductions:

- **Lower frequency (biggest win, trivial):** default interval is 24h (`DEFAULT_INTERVAL_HOURS=24`), but the beat check runs hourly. Infra topology rarely changes daily — moving the per-org default to **72h–weekly** cuts prediscovery runs (and cost) 3–7x. Already a per-user preference (`prediscovery_interval_hours`).
- **Only run on change:** trigger prediscovery on `new_connector` events (already a supported trigger) + a long periodic floor, instead of a fixed clock. Skip orgs whose connector set + discovery feed are unchanged since last run.
- **Off-peak scheduling (load, not price):** it's already a background task, so it can run at e.g. 3am to avoid competing with live incident traffic. Smooths load, doesn't lower per-token price.
- **Cheaper model for the discovery agent:** prediscovery is exploration/summarization, not deep reasoning — run it on Sonnet/haiku via a `PREDISCOVERY_MODEL` split rather than `MAIN_MODEL` (Opus). Sonnet roughly halves it, haiku cuts ~5x.
- Compounds with #3 (caching) since each run re-sends a large prediscovery prompt every turn.



---

Infra (image size, replicas, t2v-transformers, DB/storage retention) from the earlier audit is **deferred** — it's ~3% of spend and not worth the effort right now.

---

## Raw data (prod, 2026-04-19 → 2026-06-30)

- Total: 84,455 calls, $13,752.69, 2.68B input tok, 36.4M output tok
- 30d: $11,402.75 · 7d: $2,470.80
- `agent_workflow` opus: 37,259 calls, avg 58,607 input tok/call, 96% of cost is input
- prediscovery: $2,944 all-time, 671 sessions, ~$4.39/session (subset of `agent_workflow`)
- Prod env: `MAIN_MODEL=claude-opus-4.8`, `RCA_OPTIMIZE_COSTS=false`, `LLM_PROVIDER_MODE=openrouter`, guardrails already on `gemini-2.5-flash-lite` (good — only $16 total)

