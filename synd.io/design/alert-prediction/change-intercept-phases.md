# Change-Intercept System — Phase Overview

Aurora's change-intercept system detects and assesses risky changes before they become incidents. It targets the ~70% of outages that are change-induced (Google SRE Book Ch. 1; quantified as 68% binary+config pushes in SRE Workbook Appendix C).

The system ships in three phases. Phase 1a and 1b together cover the "change-gating" layer — John Googler's "two complementary halves" of catching changes (Apr 30 weekly). Phase 2 addresses runtime signals.

Full literature backing: [change-induced-incidents-literature-review.md](change-induced-incidents-literature-review.md).

## Phase map

```
Phase 1a — PR gate (pre-merge, real-time)
  Catches: code changes, Terraform/IaC, config-as-code, schema migrations —
           anything staged in a PR before it merges.
  Mechanism: GitHub App webhook on pull_request events.
  Output: GitHub PR review (approve or request_changes with rationale).
  Status: planned, detailed implementation plan exists.

Phase 1b — Cloud resource-index diff (post-apply, periodic)
  Catches: console/CLI edits, SDK scripts, drift from Terraform state,
           out-of-band IAM/policy changes — anything that changes cloud
           infrastructure without going through a PR.
  Mechanism: periodic polling of AWS Config / GCP Cloud Asset Inventory,
             diff against last known state, correlate with recent PRs.
  Output: alert via Slack / PagerDuty (no PR to post on).
  Status: high-level summary below; detailed plan deferred until 1a ships.

Phase 2 — Runtime signal analysis
  Catches: the ~30% of incidents not caused by a discrete change —
           gradual performance decay, traffic shifts, capacity exhaustion,
           dependency failures that manifest at runtime.
  Mechanism: TBD. John (Apr 30 weekly) strongly cautioned against building
             custom anomaly detection ("that is like PhD level research
             project"). Likely approach: leverage existing observability
             (Datadog, CloudWatch) rather than deploy custom agents.
  Status: not yet planned. Requires outcome data from Phase 1 first.
```

## Phase 1a — PR gate (pre-merge risk assessment)

### What it does

Every PR on a repo where the Aurora GitHub App is installed gets an LLM-driven risk assessment. Aurora fetches the diff, PR body, commits, and linked tickets at webhook time, then runs an investigation using Aurora's existing RCA tools (Datadog, Memgraph topology, postmortem search, Jenkins recent deploys). The investigation produces a binary verdict: approve or request changes. Aurora posts a GitHub PR review with the verdict and rationale.

Engineers can reply to Aurora's review to trigger a follow-up investigation with their context added.

### What it catches

Surfaces from our taxonomy (literature review, section 5) that go through PRs:

- 5.1 Source code changes
- 5.2 Binary / artifact promotions (when driven by repo changes like image tag bumps)
- 5.3 Runtime configuration (when managed as code — feature flags in config files, env vars in manifests)
- 5.4 Infrastructure changes (Terraform, k8s manifests, CloudFormation)
- 5.5 Data changes (schema migrations in a migrations folder)
- 5.8 Security / policy changes (IAM-as-code in Terraform)
- 5.9 Observability / monitoring changes (terraformed monitors/dashboards)

### What it misses

Changes that bypass the code repository:

- Console/CLI edits to cloud infrastructure
- Feature flags toggled in a SaaS UI (LaunchDarkly, Optimizely)
- IAM/policy changes made via cloud console
- Cert rotations, secret value changes
- OS/dependency auto-updates on running hosts (5.6)
- Traffic/load shifts (5.7)

These are Phase 1b and Phase 2 territory.

### Key design decisions

- **Risk assessment, not prediction.** Per John's advice (Apr 30): "you wouldn't necessarily be able to say this deployment is going to cause an incident. What you might be able to say is this deployment is a high risk deployment." The language throughout is "high risk" not "will cause an incident."
- **Advisory posture.** Customers are told not to add Aurora as a required reviewer. A `CHANGES_REQUESTED` review surfaces as a red X but does not block merge. Revisit when outcome data proves verdict quality.
- **Investigation stays inside Aurora.** GitHub's role is narrow: ingest webhooks, provide the diff (one-shot fetch at webhook time), receive review submissions. The investigator never calls back to GitHub; it works off the stored snapshot plus Aurora's internal data.
- **Vendor-neutral core.** The adapter protocol (six methods) supports GitLab, Bitbucket, etc. Only the GitHub adapter ships in Phase 1a.
- **Calibration before going live.** Per John's advice: run the investigator against the customer's last 100-200 historical PRs in dry-run mode to verify the score distribution isn't degenerate (rare-disease-test problem). Only enable live reviews after calibration passes.

### Detail plan

Full implementation plan: `.cursor/plans/phase_1_—_change-intercept_investigation_pipeline_382a0275.plan.md`

### Timeline

~3.5-4 weeks from kickoff, including calibration dry-run.

---

## Phase 1b — Cloud resource-index diff (post-apply drift detection)

### What it does

Periodically polls the customer's cloud environment (AWS Config, GCP Cloud Asset Inventory) to detect infrastructure changes that did not come through a PR. Compares current resource state against last known state and correlates with recent PR merges to filter expected changes. Unexplained changes get investigated by the same LLM engine as Phase 1a and, if high risk, produce an alert.

### Why it's needed

Phase 1a only catches changes that go through the code repository. John (Apr 30 weekly) identified the gap:

> "Infrastructure could change without anybody touching the terraform. The terraform plan only describes a particular set of infrastructure covered by the plan. But people could add other infrastructure to the subscription and the GCP project or whatever that's not covered by terraform, but which may nevertheless be problematic."

The resource-index approach catches this class of change.

### How it complements 1a

| | Phase 1a (PR gate) | Phase 1b (resource-index diff) |
|---|---|---|
| Trigger | Real-time webhook on PR event | Periodic poll every N minutes |
| Timing | Pre-merge (preventive) | Post-apply (detective) |
| Catches | Repo-mediated changes | All cloud resource changes |
| Misses | Changes bypassing the repo | Nothing at resource layer, but it's after the fact |
| Output | GitHub PR review | Slack / PagerDuty alert |

Together they cover both halves of change detection: repo-gated changes (preventive) and everything else at the infrastructure layer (detective).

### Mechanism (high level)

1. **Celery beat task** runs every N minutes per customer.
2. **Fetch current state** from AWS Config (`batch_get_resource_config`) or GCP Cloud Asset Inventory (`SearchAllResources`). Aurora already has cross-account AWS role assumption via `server/routes/aws/auth.py` — the poller reuses the same auth path with additional `config:Get*`, `config:List*`, `config:BatchGet*` IAM permissions.
3. **Diff against last known state** stored in Aurora's DB (or use AWS Config's built-in change timeline).
4. **Filter expected changes:**
   - PR-correlation window: if a PR merged recently that explains the resource change, link it and suppress.
   - Resource-type noise filter: suppress known-noisy resource types (auto-scaling group membership, ephemeral ECS tasks, CloudWatch log events).
   - Optionally: Terraform state comparison (if accessible via S3/GCS) to detect drift.
5. **For unexplained changes:** create a `NormalizedChangeEvent` with `kind='infrastructure_drift'`, run an investigation with a drift-specific prompt variant.
6. **Alert if high risk** via configured channel (Slack, PagerDuty). No PR to post on.

### What John recommended watching (Apr 30)

Three things at the cloud resource-index level:

1. **Resources in the project** — what appeared, disappeared, changed.
2. **Resource metadata** — what changed about existing resources.
3. **IAM policies** that apply to those resources — permission changes.

He explicitly recommended **not going resource-type-specific** initially. Stay at the generic resource-index level; only drill into resource-specific detail if incident data later shows one resource type dominates.

### Noise filtering is the hard problem

John's central warning: raw change detection is useless without an expected-state reference.

> "If you just sort of instrument changes, you don't necessarily know if that's bad or not. What you really need is a reference against what you should have expected."

The PR-correlation window is the cheapest useful filter (was there a recent merge that explains this?). Terraform state comparison is the cleanest but requires access to the state file. Baseline suppression of noisy resource types is the minimum viable filter.

### What it requires from the customer

Heavier onboarding than Phase 1a (which is just "install the GitHub App"):

- AWS: IAM role with Config/CloudTrail read permissions (extends existing cross-account role)
- GCP: service account with Cloud Asset Inventory read permissions
- Optionally: read access to Terraform state (S3 bucket or GCS bucket) for drift detection

### Estimated scope

~2-3 weeks after Phase 1a ships. Heavily reuses the same core: `NormalizedChangeEvent`, `change_events` schema (new `kind` value), `change_investigations`, investigator engine, verdict validator. New work is the poller, noise filter, drift-specific prompt variant, and alert output channel.

### Detail plan

Deferred until Phase 1a is shipping. Will be created as a separate `.cursor/plans/` file.

---

## Phase 2 — Runtime signal analysis

### What it would cover

The ~30% of incidents not caused by a discrete change: gradual memory growth, traffic shifts, capacity exhaustion, dependency degradation, cascading failures.

### John's strong caution (Apr 30 weekly)

John spent the last third of the Apr 30 call pushing back on custom anomaly detection:

> "The idea of we're going to build a thing that does anomaly detection — that is like PhD level research project. It's not something that would be like here's a hundred lines in a TypeScript module."

Key concerns:
- **Spurious anomalies.** With 20-30 metrics and a 95% confidence interval, you'll get false positives constantly from random chance alone. Alert fatigue is the likely outcome.
- **Metric rise does not mean incident.** CPU doubling because traffic doubled is normal. You need to correlate system metrics with business function metrics (tickets sold, requests processed) to know if there's actually a problem — and business metrics are different for every application.
- **"Anomaly before it happens" is a contradiction.** You can detect that you're in an anomaly and predict it's going to get worse, but you can't detect a future anomaly that hasn't started manifesting.

### What Phase 2 should probably look like (tentative)

Based on John's guidance:

- **Don't build custom anomaly detection.** Use existing observability tools (Datadog, CloudWatch, Grafana) that customers already have.
- **Don't deploy agents on customer infrastructure.** John pushed back hard on the daemon approach. Use OTel configurations or existing log/metric pipelines instead.
- **Do use LLMs to make sense of signals.** If a set of metrics trips a threshold, have the LLM investigate why and whether it's concerning — that's where Aurora adds value, not in the detection itself.
- **Consider Bayesian testing for canary analysis.** John raised this as the "conventional way" to know if a deploy worked. Too sophisticated for Phase 1 but potentially valuable once there's enough data.

### Status

Not yet planned. Requires outcome data from Phases 1a and 1b to understand what incidents the change-gating layer catches and what falls through. Phase 2 design should be informed by real gaps observed in production, not speculative.

---

## Coverage map across the nine change surfaces

From the literature review taxonomy (section 5), mapped against each phase:

- **5.1 Source code changes** — Phase 1a (PR gate)
- **5.2 Binary / artifact promotions** — Phase 1a (if repo-driven); Phase 1b (if promoted outside repo)
- **5.3 Runtime configuration** — Phase 1a (if in code); Phase 1b (if changed via console/CLI/SaaS UI)
- **5.4 Infrastructure changes** — Phase 1a (Terraform PRs); Phase 1b (console/CLI drift)
- **5.5 Data changes** — Phase 1a (migration PRs); Phase 1b (ad-hoc DB edits via drift on dependent resources)
- **5.6 Dependency / OS upgrades** — Phase 1b (if the upgrade changes cloud resources); Phase 2 (runtime impact of auto-updates)
- **5.7 Traffic / load shifts** — Phase 2 (runtime signals)
- **5.8 Security / policy changes** — Phase 1a (IAM-as-code); Phase 1b (console IAM edits, cert rotations)
- **5.9 Observability / monitoring changes** — Phase 1a (terraformed monitors); Phase 1b (UI-edited monitors/dashboards)

Phase 1a + 1b together cover 8 of 9 surfaces (with varying depth). Only traffic/load shifts (5.7) are exclusively Phase 2 territory.
