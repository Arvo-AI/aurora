# RCA Agent Quality Improvement Roadmap

## Phase 1: Prompting (System Prompt Behavioral Sections)

**What it is**: Break all behavioral guidance into modular markdown sections loaded into the L7 system prompt. This is a single body of work — all prompt text lives in `server/chat/backend/agent/prompt/rca_sections/`.

**Pre-check: Verify alert data quality**
Before tuning prompts, confirm the agent is receiving complete data from each webhook source. Each platform has a normalizer in `rca_prompt_builder.py` (`build_datadog_rca_prompt`, `build_grafana_rca_prompt`, etc.) that maps raw payloads into a common `alert_details` dict. Audit: are we dropping useful fields? Are we triggering RCA on events that shouldn't get one (recovered, informational)? Is the normalized output rich enough per source for the agent to investigate?

**Sections already written (draft):**

| Section | Purpose |
|---------|---------|
| `identity.md` | "You are Aurora, an autonomous SRE agent..." |
| `investigation.md` | Hypothesis-first methodology, work in passes |
| `error_recovery.md` | What to do when queries return empty |
| `evidence_standard.md` | Never state root cause without evidence |
| `context_mgmt.md` | Write findings in text (they persist) |

**Sections still needed:**

| Section | Purpose |
|---------|---------|
| `conclusion_gate.md` | Names the agent's rationalization patterns to prevent premature conclusions |
| Updates to `context_mgmt.md` | Length discipline — concise findings between tool calls |
| Updates to `evidence_standard.md` | Faithful reporting — don't hedge confirmed results, don't overclaim hypotheses |

**Conclusion gate content** (the most impactful addition):

```markdown
# Before Concluding Root Cause

You will feel the urge to conclude early. Recognize these rationalizations:
- "The timing correlates" — correlation is not causation. Find the mechanism.
- "This is the most common cause of this error" — common does not mean actual.
  Verify for THIS incident.
- "I found a log line that matches" — one data point is not a root cause.
  Find the pattern.
- "The service restarted so it's probably resource exhaustion" — check actual
  resource metrics before concluding.

Before stating your root cause:
1. Name at least one alternative you considered and why you ruled it out
2. Cite the specific evidence (tool output) that supports your conclusion
3. Verify the timeline — does your root cause explain WHEN the alert fired?
```

**Length discipline addition** (append to `context_mgmt.md`):

```markdown
Keep text between tool calls concise — state what you found, what it means, and
what you'll check next. Do not narrate your reasoning at length.

Bad: "Based on my analysis of the logs, it appears that there might be a potential
issue with memory consumption, which could possibly be related to..."
Good: "3 OOMKilled events at 14:23, 14:45, 15:01. Memory grew from 50MB to 120MB
between restarts. Next: check memory limits and recent deployment changes."
```

**Faithful reporting addition** (append to `evidence_standard.md`):

```markdown
State confidence accurately. If evidence is strong, say so plainly — do not hedge
confirmed results. If it's a hypothesis with partial support, say that. Do not
hedgify everything into "might be" or overclaim "definitely is."
```

**Total system prompt cost**: ~800 tokens per API call (sent every turn).

**Effort**: Low — writing markdown files, no code changes
**Impact**: High — directly addresses every known RCA failure pattern

---

## Phase 2: Tool Descriptions (Evaluate After Phase 1)

**What it is**: Make the investigation methodology from SKILL.md files more prominent to the model during RCA.

**Current state**: SKILL.md files already contain detailed investigation workflows (e.g., Datadog SKILL.md has a 6-step RCA workflow). These are already pre-injected into the user message during background RCA via `load_skills_for_rca()`. So the methodology is already in context.

**Open question**: Is the existing injection sufficient? Or does the model lose track of it by turn 5-6 as it drifts further back in context? Tool descriptions (sent every turn in the tools schema) could reinforce it, but may be redundant.

**Approach**: Test Phase 1 first. If the agent follows SKILL.md workflows, this phase is unnecessary. If it doesn't, consider:
1. A one-line pointer in the tool description: "Refer to the Datadog Investigation Workflow in your instructions"
2. Inlining 3-4 key points from SKILL.md into the tool description (~100 tokens per tool per turn)

**Decision deferred until Phase 1 testing.**

---

## Phase 3: Memory (Aurora Learn at L7)

**What it is**: Aurora Learn retrieves past RCA summaries from Weaviate via semantic search and injects them as context. Currently disabled at L7.

**Open concern**: The knowledge base may have a signal-to-noise problem. Past incidents retrieved via semantic search might not actually be relevant — they could be superficially similar (same service name, different failure mode) and add noise rather than useful context. Before re-enabling, we should evaluate:
- How good is the retrieval quality? Are the "similar incidents" actually helpful?
- Should we revisit what we index and how we populate the knowledge base to improve signal-to-noise?
- Would stricter filtering (same service + same error type, not just semantic similarity) produce better results?
- Should we add a relevance threshold below which results aren't injected?

**What we'd change (once KB quality is addressed)**:
1. **Re-enable at L7**: Move the `strip_level >= 6` early return so context gets injected
2. **Add staleness warning**: "Context from past incidents is below. Verify relevance — infrastructure may have changed."
3. **Improve population** (TBD): Revisit what gets indexed, filtering criteria, and whether negative signals (corrections) should be stored alongside successes

**Effort**: Low (re-enable) but the KB quality work is a separate investigation
**Impact**: High for repeat incidents IF retrieval quality is good; harmful if it's noise

---

## Phase 4: Manage Long Sessions & Compaction (Future)

**What it is**: Infrastructure for investigations with 10+ tool calls where earlier tool results get truncated and the model loses prior findings.

**Current state**: Tool results are truncated to 4,000 characters on history rebuild. The model isn't told this happens.

**Possible improvements** (evaluate after Phase 1 testing):
- **Microcompact**: Keep only last 3-5 tool results, clear older ones, tell the model this will happen
- **RCA-tuned summary template**: If full summarization is triggered, use sections specific to RCA (hypotheses, evidence, dead ends) instead of a generic summary
- **Post-compact restoration**: Re-inject the original alert data after compaction

**Decision deferred**: Most RCAs complete in 5-8 tool calls. Only invest here if testing shows context pressure is actually degrading quality.

---

## Stretch: Conclusion Gate Enforcement (Code-Level)

**What it is**: Beyond just telling the model in the prompt (Phase 1), actually enforce it in code — check the agent's final response before returning it.

**How it would work**: In the agent loop, before the RCA agent produces its final output, parse the response and check:
1. Does it mention at least one ruled-out alternative?
2. Does it cite specific tool output as evidence?
3. Does the proposed timeline align with the alert time?

If not met, inject a follow-up message: "Your conclusion needs verification. What evidence rules out the next most likely cause?" and let the model continue for 1-2 more turns.

**Effort**: Medium — requires agent loop changes and heuristic parsing
**Impact**: High — but Phase 1's prompt text may already solve most of this. Test first.

---

## Stretch: Output Token Recovery (Not super important because we don't hit the context window for now)

**What it is**: When the model hits max output tokens mid-investigation (gets cut off), inject a recovery message so it continues seamlessly instead of restarting with a recap. 

**Recovery message**: "Resume investigation directly. Do not recap. Continue from where you left off."

**Effort**: Medium — requires detecting the truncation and injecting the follow-up
**Impact**: Low-Medium — only happens in very long RCA responses

---

## Summary

| Phase | Type | Effort | Impact | Status |
|-------|------|--------|--------|--------|
| 1. Prompting | Prompt text | Low | **High** | Focus now |
| 2. Tool Descriptions | Evaluate | TBD | TBD | After Phase 1 testing |
| 3. Memory | KB quality + re-enable | Low-Med | Depends on KB quality | Open |
| 4. Compaction | Infrastructure | High | Medium (long sessions) | Future |
| Stretch: Conclusion Gate | Agent loop code | Medium | High | If prompt alone isn't enough |
| Stretch: Output Recovery | Agent loop code | Medium | Low-Med | If truncation is observed |

**Recommended approach**: Finish Phase 1 (prompt sections), test, evaluate. Phases 2-4 are informed by what we observe.

---

## Future: Revisit Interactive Chat Prompts

The current focus is on the autonomous background RCA agent. However, the interactive chat (human-sent messages) uses a separate system prompt (`skills/core/`) that hasn't been audited for quality. Once the RCA prompt work is validated, apply the same rigor — conciseness, behavior-driven instructions, evidence standards — to the interactive prompt. Lower priority since a human in the loop compensates for weak prompting, but worth revisiting.
