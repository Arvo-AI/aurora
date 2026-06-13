# DEV-1256: Next Steps v2 — Design Document

## Problem

Aurora's current next steps are a report appendix — a static list generated from a prose summary at the end of an investigation. This caps the ceiling of the feature regardless of prompt quality:

1. **Lossy input**: `_build_suggestions_prompt()` consumes the *summary* (a narrative compression of the investigation) and asks an LLM to reverse-engineer actions from it. The structured investigation state — hypotheses, evidence weights, ruled-out branches, unexplored paths — is discarded.
2. **Fixed count**: "2-4 suggestions" regardless of incident complexity.
3. **No separation of action types**: immediate mitigation mixed with root cause fixes mixed with diagnostics.
4. **Suggests work Aurora could have done itself**: read-only diagnostics like "check logs for OOMKilled events" are delegated to a human when Aurora has kubectl access and could have run them.

## Core Insight

Next steps are not a summary appendix. They are **the frontier of the partially-explored search tree, ranked, with the safe branches already executed and a named human attached to the riskiest one.**

Every competitor (Rootly, FireHydrant, PagerDuty) generates advice from a summary or runs pre-built runbook automation. None consume the structured investigation state. None execute their own safe suggestions before presenting them. This is Aurora's architectural moat — the agent was *in* the investigation with real tool access, not reading about it afterward.

## Research Findings

### Competitor Landscape (adversarially verified)

The market splits into two camps, neither solving this well:

| Approach | Tools | What they do | Gap |
|----------|-------|--------------|-----|
| Executable automation | PagerDuty Automation, Resolve.io | Pre-built runbooks triggered by pattern matching | Not intelligent; if/then workflows |
| Text advisory | Rootly, FireHydrant | AI-generated action items in retrospective docs | No confidence scoring, no time horizons, no adaptive depth |

**No competitor publicly demonstrates**: categorization by time horizon, adaptive recommendation count, confidence-driven suggestion types, or self-execution of safe diagnostics. Every claim about structured output formats from these tools was refuted under adversarial verification (marketing copy only).

### SRE Literature

| Framework | Categories | Key Insight |
|-----------|-----------|-------------|
| Google SRE Workbook | Prevent / Detect / Mitigate / Repair | Verifiable end states, systemic over behavioral |
| NIST SP 800-61 | Contain / Eradicate / Recover / Lessons Learned | Phase-sequenced, confidence drives next phase |
| GitHub (Oct 2018) | Immediate / Medium-term / Foundational | Urgency-tiered |

Critical principles:
- **"Customers do not care whether you fully understand what caused an outage. They want to stop receiving errors."** — Google SRE
- **Verifiable end state** is the quality bar: "add alert on p99 > 500ms for /checkout" not "improve monitoring"
- **Spotify's lesson**: deprioritizing a root cause fix after mitigation led to a second, larger outage weeks later. Immediate mitigation is necessary but not sufficient.
- **Action items with single owners and tracking get completed**; vague shared-responsibility items rot (Google, Airbnb, New Relic all confirm)

## Architecture

### Current Flow (what's wrong)

```
Orchestrator → Sub-agents → Findings → Synthesizer → Summary (prose)
                                                          ↓
                                                   SuggestionExtractor
                                                   (reads summary, generates suggestions)
```

The SuggestionExtractor operates on the lossy summary, not the structured investigation state.

### New Flow

```
Orchestrator → Sub-agents → Findings → Synthesizer → Summary (prose, for humans)
                                  ↓
                            Aggregator (structured)
                                  ↓
                            Recommender
                              ↓     ↓
                    Self-Execute    Suggest
                    (safe diags)   (human-required actions)
```

The Recommender consumes the **Aggregator's structured output** (hypothesis ledger, evidence graph, unexplored branches) — not the Synthesizer's prose. It then:
1. Runs any safe diagnostic it can execute itself
2. Generates suggestions only for actions that require human judgment, privilege, or knowledge

### The Three-Class Invariant

**Hard rule: if a suggestion is (a) risk=safe, (b) executable with tooling Aurora already has access to, it must not appear as a suggestion. Aurora must have run it.**

This is testable, enforceable, and the single biggest differentiator. It collapses the suggestion space to exactly three legitimate classes:

| Class | When | Example |
|-------|------|---------|
| **Privilege gap** | Aurora lacks tooling/access | "Check Cloudflare dashboard — no connector available" |
| **Non-trivial risk** | Requires human judgment/authorization | "Roll back deploy v2.3.1 (undo: redeploy v2.3.1)" |
| **Human knowledge** | Requires org context or decision | "Contact jsmith (authored suspect change abc123, 4h before alert)" |

Every privilege-gap suggestion is simultaneously a **product signal** — log them and you have a ranked roadmap of which connectors to build next, generated by real incidents.

## The Hypothesis Ledger

Replace the confidence string-match (`"root cause undetermined" in summary`) with a structured hypothesis ledger emitted by the Synthesizer alongside its summary:

```python
@dataclass
class Hypothesis:
    statement: str                      # "Connection pool exhaustion from config abc123"
    probability: str                    # "high" | "medium" | "low"
    supporting_evidence: list[str]      # citation indices into investigation evidence
    contradicting_evidence: list[str]
    discriminating_test: Optional[str]  # what would confirm/kill this
    mitigation_class: str               # "rollback" | "restart" | "scale" | "config_revert" | "code_fix" | "unknown"
    status: str                         # "confirmed" | "open" | "ruled_out"

@dataclass
class InvestigationState:
    hypotheses: list[Hypothesis]
    ruled_out: list[Hypothesis]         # with evidence that killed them
    unexplored: list[str]               # branches never opened, and why
```

This exists because the current architecture already captures the raw material. Sub-agent findings include:
- `self_assessed_strength`: strong/moderate/weak/inconclusive
- `## Reasoning`: hypothesis evaluation
- `## What I ruled out`: dead branches with evidence
- `follow_ups_suggested`: unexplored paths

The Synthesizer (`synthesis.py`) already reads all of this. Adding a `hypothesis_ledger` field to `SynthesisDecision` extracts what's already implicit:

```python
class SynthesisDecision(BaseModel):
    needs_more_research: bool = False
    follow_up_inputs: list[SubAgentInput] = []
    rationale: str = ""
    summary: str = ""
    hypothesis_ledger: Optional[InvestigationState] = None  # NEW
```

### What the Ledger Buys

**1. The differential.** Medicine solved this presentation decades ago. An incident handoff should look like:
> Hypothesis A (high): evidence [3, 7, 11]. Hypothesis B (medium): evidence [5], contradicted by [9]. Ruled out: C, because [12]. Not examined: network policies (no symptom pointed there).

An engineer who sees this *trusts the system* because they can audit the reasoning and disagree with a specific step.

**2. The negative space.** `ruled_out` and `unexplored` are the two highest-trust artifacts you can ship. "I did not examine network policies because no symptom pointed there" saves an engineer twenty minutes of re-deriving coverage. Every experienced responder's first question mid-incident is "what have you ruled out?"

**3. Action-equivalence reasoning.** The killer non-obvious insight for suggestion quality:

> **You only need to discriminate between hypotheses that have different mitigations.**

If hypothesis A (60%) and hypothesis B (35%) are *both* fixed by rolling back the last deploy — don't diagnose. Suggest the rollback. Defer discrimination to daylight hours. Conversely, if the top hypotheses require *opposite* actions (scale up vs. roll back, where scaling under a poison-pill deploy makes things worse), then the discriminating diagnostic is the single most urgent action.

Prompt instruction for the Recommender:
```
For each pair of leading hypotheses, determine whether they require the same or
different immediate action. Only generate diagnostic steps that discriminate between
hypotheses requiring DIFFERENT mitigations. If all leading hypotheses share the same
mitigation, suggest that mitigation directly — do not waste time diagnosing which
specific cause is active.
```

## Suggestion Schema

```python
@dataclass
class Suggestion:
    title: str
    description: str
    type: str               # 'mitigation' | 'diagnostic' | 'remediate' | 'prevent' | 'escalation'
    risk: str               # 'safe' | 'low' | 'medium' | 'high'
    command: Optional[str]
    rationale: str          # why this action matters, tied to specific evidence
    undo: Optional[str]     # how to reverse if it makes things worse
    hypothesis_ids: list[str]  # which hypotheses this addresses
    expected_outcomes: Optional[list[OutcomeBranch]]  # one-level decision tree
    parallel_group: Optional[int]  # diagnostics with same group can run simultaneously
    privilege_gap: Optional[str]   # if type is privilege-gap, which connector is missing

@dataclass
class OutcomeBranch:
    observation: str        # "OOMKilled events present in pod logs"
    implication: str        # "confirms hypothesis 2 (memory leak from v2.3.1)"
    then: str              # "proceed with suggestion #1 (rollback)"
```

Key additions over current:
- **`undo`**: every medium/high risk suggestion answers "if this makes it worse, how do I get back?" Engineers execute risky actions 5x faster when the escape hatch is printed next to it.
- **`expected_outcomes`**: one-level conditionals. "Run X; if you see A, it's the memory leak, roll back; if you see B, it's the config, revert abc123." This is exactly how a senior engineer hands off mid-investigation.
- **`parallel_group`**: diagnostics with no dependency get a shared group tag. At 3am with 1-3 min per diagnostic, parallel vs sequential is a real MTTR difference.
- **`hypothesis_ids`**: links suggestions to the ledger, enabling future statefulness (re-rank when evidence arrives).

## Rendering by Incident Phase

The three-tier taxonomy (Mitigate/Remediate/Prevent) is derived from *postmortem* literature. During a live incident, it's the wrong framing. Split rendering by incident state:

### Active Incident (SEV1/SEV2, not resolved)
- Show **one** top mitigation, full width, with its undo path
- Collapse everything else behind a disclosure
- Diagnostics show expected_outcomes inline
- Parallel groups rendered as "run these simultaneously"

### Post-Resolution / Analysis Phase
- Full grouped view:
  - **Immediate**: type = mitigation
  - **Fix**: type = remediate
  - **Prevent**: type = prevent
  - **Escalation**: type = escalation (with named person and reason)
- Show hypothesis differential as context above suggestions

### Change Correlation (first-class suggestion type)

The majority of production incidents are caused by changes. The highest-value correlation Aurora can do:

> **Revert change abc123 and contact its author.** Merged 4h12m before alert onset by jsmith (deploy log). Owning team: platform-db. On-call: mchen (PagerDuty schedule).

This requires surfacing: deploy logs, config diffs, git history within the incident window, intersected with affected services. The suggestion should include the person, the reason *they specifically*, and the channel to reach them. Even the degraded version ("author of suspect change per git log") is shippable.

## The Self-Execution Round

Before generating suggestions, the Recommender gets one more tool-use round:

```python
async def _self_execute_safe_diagnostics(
    recommender_state: InvestigationState,
    tool_access: ToolRegistry,
    max_calls: int = 5,
) -> tuple[InvestigationState, list[ExecutionResult]]:
    """
    For each diagnostic the Recommender would suggest:
    - If risk=safe AND Aurora has the tooling: execute it
    - Update the hypothesis ledger with new evidence
    - Return updated state + execution results
    
    The remaining suggestions are only actions Aurora CANNOT do itself.
    """
```

Cost: additional latency (capped at N tool calls) and tokens. Worth it. This is the moment the system goes from "here's what you should check" to "I checked these 3 things — here's what I found, and here's what only you can do."

Cap at 5 tool calls for Phase 1. Track execution time and adjust.

## Implementation Phases

### Phase 1: Hypothesis Ledger + Recommender Architecture
1. Add `hypothesis_ledger: Optional[InvestigationState]` to `SynthesisDecision`
2. Update synthesis prompt to emit the ledger alongside the summary
3. Build `Recommender` that consumes the ledger + raw findings (not the summary)
4. Add action-equivalence instruction to Recommender prompt
5. Add `rationale`, `undo`, `expected_outcomes`, `hypothesis_ids` fields to Suggestion
6. DB migration: new columns on `incident_suggestions`
7. String-match fallback when ledger parsing fails (graceful degradation)

### Phase 1.5: Self-Execution Round
1. Recommender gets tool access (same registry as sub-agents)
2. Before generating suggestions, execute safe diagnostics (capped at 5 calls)
3. Update ledger with new evidence
4. Generate suggestions only for human-required actions
5. Log every privilege-gap suggestion as a product signal

### Phase 2: Frontend + Rendering
1. Update `SuggestionType` union: add `'remediate'`, `'prevent'`, `'escalation'`
2. Phase-aware rendering (active vs resolved incident)
3. Hypothesis differential display above suggestions
4. Expected outcomes rendered inline with diagnostics
5. Parallel group tags rendered as "run simultaneously"
6. Undo paths displayed next to medium/high risk actions

### Phase 3: Change Correlation
1. Deploy log correlation within incident time window
2. Git history intersection with affected services
3. Author/owner resolution (CODEOWNERS, PagerDuty schedule)
4. Escalation suggestions with named person + reason

### Phase 4: Incident Memory (per-org)
1. Embed past incident summaries
2. kNN retrieval on current incident signature
3. Surface "2 similar incidents in past 90 days; resolution was X"
4. Not ML learning — just lookup. But it's the only version of cross-incident patterns Aurora can ever have (self-hosted forecloses cross-customer learning permanently).

### Phase 5+: Statefulness
1. Suggestions linked to hypothesis IDs enable live re-ranking
2. Engineer runs a mitigation → Aurora observes via connectors → Recommender re-runs against updated ledger
3. "Next steps" becomes a live frontier, not a static document

## Validation

### Adversarial Tests
- Feed incidents where correct output is 1 suggestion or 0 diagnostics — verify no padding
- Feed incidents where two hypotheses share the same mitigation — verify no unnecessary diagnostics
- Feed incidents where Aurora has tool access for a diagnostic — verify it runs it rather than suggesting it
- Feed incidents with undetermined root cause — verify diagnostics have expected_outcomes

### Metrics

Primary (gold metric — **we have a [Run] button, use it**):
- **Verbatim execution rate**: fraction of suggestions where engineer clicked Run or pasted command unmodified
- **Command edit distance**: for commands modified before running (measures specificity failures)
- **Time-to-first-action**: after Aurora posts next steps

Secondary:
- **Discrimination hit rate**: for diagnostics with expected_outcomes, did observed outcome match a predicted branch?
- **Resource specificity**: >90% of suggestions reference specific resources from investigation evidence (checkable via regex against evidence corpus)
- **Privilege-gap log**: volume and categories feed connector roadmap

### What Not to Measure
- "No incidents produce exactly the same count" — trivially gameable by randomness, measures variance not quality
- Completion rates of prevent-tier items — out of Aurora's control, belongs to incident management process

## Non-Goals
- **Full decision trees** — combinatorial cost, stale the moment reality diverges. One-level conditionals capture 90% of value.
- **Cognitive state modeling** — unfalsifiable, creepy. Incident-phase-aware rendering achieves the defensible 90%.
- **Cross-customer pattern learning** — architecturally impossible for self-hosted. Per-org incident memory (Phase 4) is the only version we'll ever have.
- **Automated execution of risky mitigations** — requires approval workflow, separate feature.

## Risks

| Risk | Mitigation |
|------|-----------|
| Ledger parsing fails | String-match fallback to current behavior; ledger is additive |
| Self-execution round adds latency | Cap at 5 calls; track and adjust; show "Aurora is checking..." state |
| LLM pads suggestions despite "no minimum" | Adversarial test suite with degenerate cases; explicit prompt: "1 suggestion is valid. 0 diagnostics when root cause is confirmed is correct." |
| `undo` field is wrong/dangerous | Same safety validation as commands; undo is informational, not auto-executed |
| Hypothesis probabilities are confabulated | Discrimination hit rate metric catches this automatically; probabilities are coarse (high/medium/low) not numeric |

## Appendix: Why This Can't Be Prompt-Only

The v1 instinct ("just rewrite the prompt in suggestion_extractor.py") fails because:
1. The summary is lossy — hypotheses, ruled-out branches, and evidence weights are compressed away
2. The SuggestionExtractor has no tool access — it can't self-execute
3. It has no access to investigation history — it can't correlate changes or identify privilege gaps
4. It can't link suggestions to hypotheses — blocking future statefulness

The Recommender must be a new component that consumes the Aggregator's structured output and optionally has tool access. The SuggestionExtractor becomes a legacy fallback for when the orchestrator path isn't available (e.g., non-orchestrated single-agent investigations).
