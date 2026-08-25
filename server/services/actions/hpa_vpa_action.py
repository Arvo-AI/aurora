"""
Built-in Right-Sizing Audit Action

Default instructions for the system action that compares real CPU and memory
usage against the requests, limits, and autoscaler bounds declared in IaC, and
opens one PR per materially mis-sized workload.
"""

DEFAULT_HPA_VPA_INSTRUCTIONS = """**Step 1: Gather context**

Understand what the team actually declared before judging any of it.

- Call get_infrastructure_context to learn the org's services, environments, and clusters.
- Call get_connected_repos (or equivalent) to find connected code repositories.
- Search those repos for the config that sets resource requests, limits, and autoscaler
  bounds. This could be in any format: Terraform (.tf), Helm charts and values files,
  Kustomize overlays, raw Kubernetes manifests, Pulumi, Jsonnet, or an operator CRD.
- For each workload you find, record: current CPU request and limit, current memory request
  and limit, the autoscaler kind (HPA, KEDA, VPA, none), minReplicas and maxReplicas, the
  environment, and the exact file path and line the value lives on.
- Note which metrics provider and which VCS provider are connected, and use the matching
  tools for each. Match the repo's own conventions rather than imposing a layout.

If you cannot find any repo declaring resource requests, update the living document
explaining what you looked for and stop. Do not guess or hallucinate a repo structure.

**Step 2: Scope the audit**

A workload is in scope only if all three hold:
- The IaC *actually declares* the value you would change. Never propose a number for a
  field the team has not set -- adding a request where there was none is a behaviour change,
  not a right-sizing.
- The workload is identifiable in metrics by a stable grouping tag (a deployment or service
  label), so usage can be attributed to it unambiguously.
- It ran for the whole measurement window. A workload created last week has a 30-day *gap*,
  not a 30-day signal. Say so and exclude it.

**Step 3: Measure -- two separate computations, never conflated**

*Reconciliation -- does our view match the team's?*

Read the org's **own** monitor and alert definitions from whichever monitoring provider is
connected (for Datadog, `resource_type='monitors'`). Take their real query strings and
thresholds from those definitions -- **do not assume a formula**. Then reproduce it exactly:
same aggregation, same grouping, same filters, same evaluation window, same delay. Require
the firing counts to line up with what the team saw.

Read every part of their definition off the monitor rather than assuming a default, and state
each one you used in the living document:

- *Shape.* A resource-saturation monitor is usually a **ratio of two summed series** --
  `sum(usage) / sum(limits)`, grouped by the workload tag -- evaluated against a fractional
  threshold like `0.90`. Compare like with like: a raw usage figure tested against a
  fractional threshold will reconcile with nothing. Whatever shape their monitor actually
  uses, mirror it.
- *Grouping.* Group by the same tag they group by (commonly the deployment tag), so a
  workload's pods aggregate the way the monitor aggregates them.
- *Scaling.* Apply the same unit scaling the monitor applies -- a CPU ratio built from
  nanocore usage needs the `/1e9` the monitor has; a memory ratio in bytes/bytes does not.
  A ratio whose numerator and denominator are in different units is silently meaningless.
- *Filters.* Reuse their namespace and tag exclusions verbatim (for example excluding
  system namespaces, or ephemeral environments). Dropping an exclusion changes the counts.
- *Window and delay.* Use their evaluation window and apply the same evaluation delay, or
  samples will not line up even when the formula is right.
- *Per-environment variation.* Windows, thresholds, and filters commonly differ per
  environment. Read each environment's own values; never apply production's to staging.

If they do not line up, our view of this system disagrees with the team's, and every number
downstream is suspect. Stop, record the mismatch in the living document, and do not open
PRs off numbers you could not corroborate.

This step tells you *which workloads run hot*. It is **not** the basis of any recommended
number -- never derive a sizing figure from an alert threshold.

Note the asymmetry and keep it straight: a saturation monitor usually measures usage against
**limits**, while right-sizing changes **requests**. They are different fields with different
consequences -- requests drive scheduling and cost, limits drive throttling and OOM-kills. Do
not read a limits-based utilization figure as a statement about requests, and say which field
each number refers to whenever you report one.

*Sizing -- what should the value be?*

Ask for the 30-day p95 of actual usage, summed across pods and grouped by workload (for
Datadog, `resource_type='metric_stats'`, which computes percentiles server-side). Do CPU
and memory as separate queries. Also measure replica count over the same window.

Aggregate **across all pods of the workload** (`sum`, grouped by the workload tag) -- the
total the deployment consumes, not a hot-pod signal. A max-across-pods reading would size
every replica for the worst one and inflate every recommendation.

- **Check the unit before doing any arithmetic, and convert to the unit the IaC uses.**
  Container CPU usage is typically reported in *nanocores* (Datadog's
  `kubernetes.cpu.usage.total` is), so divide by 1e9 to get cores, then express as millicores
  to compare against a `500m`-style request. Memory usage is reported in *bytes* and needs no
  such scaling -- convert to Mi/Gi only for display. Every row carries a `unit` field: read
  it. Comparing a nanocore p95 against a millicore request is a ~1,000,000x error, and it
  looks like a plausible number all the way into the PR body.
- Group with `by {tag}` rather than one query per workload; a single grouped query returns
  every workload at once. Query one environment at a time to stay inside the request timeout.
- Check `points` and `nulls` on **every** row. A row with a null p95, or one whose points
  count falls well short of the window, means *no data* -- which is not the same as *low
  usage*. Never treat an empty series as an idle workload.
- Never reason from a truncated result set. If the response carries `truncated`,
  `truncated_all`, `series_truncated` or `series_dropped`, narrow the query and re-run.

**Step 4: Size the recommendation**

- `new_request = ceil(p95 x 1.3)` -- a 30% headroom band over sustained real usage. Compute the
  headroom on the p95 *in the IaC's own unit*, after any unit conversion from Step 3.
- Round *up*, never down: CPU to the next 50m, memory to the next 64Mi. Rounding up further to
  a conventional value a human would have written (`750m` rather than `700m`, `768Mi` rather
  than `704Mi`) is fine and preferred -- it reads as a deliberate choice rather than a
  machine artefact. Never round *down* to reach a tidy number: that silently cuts headroom.
- State the p95, the headroom multiple, and the rounded result in the PR body, so a reviewer
  can check the arithmetic instead of taking the number on trust.
- Preserve the existing request:limit ratio. If that ratio is itself unreasonable, say so in
  the PR body rather than silently normalizing it -- the ratio is someone's decision.
- **Movement under 20% is not a recommendation.** A 5% shave is measurement noise, and
  proposing it teaches reviewers to ignore us. Drop it.
- Convert to human units before writing anything anyone reads. Never write a raw 10+ digit
  integer (nanocores, bytes) in a PR body, a card, or a comment.

**Step 5: Decide, asymmetrically**

This is the most important step. Treating the two directions as symmetric is the classic way
to cause an outage while saving money.

*CPU* -- symmetric. Move it when the mis-size is at least 20% and has been sustained for at
least 7 continuous days. The failure mode of too little CPU is throttling, which is
recoverable. Before proposing any decrease, confirm there is no meaningful throttling
already occurring; if there is, the correct direction is *up*, not down.

*Memory* -- asymmetric. Increases follow CPU's terms. A **decrease** is allowed only when
`max` never exceeded the proposed request across the entire window. Check `max`, not `p95`:
the failure mode of too little memory is an OOM-kill, which is an outage, and a p95 that
looks comfortable tells you nothing about the peak that will kill the pod.

*maxReplicas* -- trim only when peak replicas stayed well below the current maximum for the
whole window, and only to a value that leaves clear room above that observed peak. The
maximum is a ceiling for a traffic event that has not happened yet, not a target.

**Step 6: Account for the autoscaler you actually found**

- *CPU or memory-scaled (HPA on resource metrics)*: all three dimensions are in play.
- *Externally-scaled (KEDA on queue depth, event lag, a custom metric)*: the resource
  request is not the scaling input. Keep the memory-p95 finding, drop any
  CPU-as-scaling-signal framing, and name the real scaling signal.
- *Scale-to-zero*: percentiles computed over long idle periods understate active demand.
  Measure only active periods, or state plainly that you cannot size it.
- *Node-level autoscaling only (Karpenter, cluster-autoscaler)*: this scales nodes, not
  replicas, so there is no replica bound to trim. Requests still matter here -- they are the
  input the node autoscaler bin-packs against, so right-sizing them is what actually reduces
  node count. Say that in the PR body.
- *Pinned replicas (min == max)*: be conservative -- a single pod absorbs every spike alone.
  Flag the pinning itself as a finding.

Always state the constraint on the card and in the PR body. A reviewer who is not told about
it will assume we simply missed it.

**Step 7: Check prior work before writing anything**

- Call list_hpa_vpa_recommendations **first**, before opening any PR.
- An open recommendation for a workload means *update* it -- do not open a second PR.
- A workload in cooldown that is still quiet gets skipped. A human said no; asking again in a
  week is how this action gets muted.
- Also search the VCS for prior right-sizing work, covering **open and recently merged**
  (roughly the last 4 weeks) -- a change merged days ago may already be in effect, and
  re-proposing it makes us look like we are not reading their repo. Search on all of:
  the workload name, the IaC file path you intend to touch, the branch-name pattern this
  action uses, and titles suggesting an earlier attempt (for example "consolidate", "tune",
  "right-size", or "supersedes").
- If a matching PR is open and unmerged, comment on it instead of opening a duplicate, and
  reference the earlier PR number in anything you write.

**Step 8: Open the PR**

- One workload per PR. A reviewer should be able to accept or reject a single decision.
- Change only what your evidence supports. Do not tidy adjacent config.
- Prefer `edit_file` on existing manifests; use `create_or_update_file` only for brand-new paths.
- Match the repo's branch naming, title style, and label conventions.
- At most one short inline comment, and only where a number would otherwise look arbitrary.
- PR body, a few lines per dimension: current value, recommended value, the p95, the max, the
  measurement window, the autoscaler constraint, and a link to this run. No essay.
- Before opening, grep your own diff for 10+ digit integers and human-readable units.

**Step 9: Notify**

- The PR is the deliverable and the living document is the permanent record. Notification is
  how a human finds out, and which surfaces exist depends on what the team has connected --
  so use what is available rather than assuming any particular one.
- If send_hpa_vpa_recommendation is available, call it once per workload you opened a PR for,
  passing the per-dimension current, recommended, and evidence values, and the autoscaler so
  the card can explain a partial recommendation. Omit any dimension you are not recommending
  a change to.
- If it is not available, the team has no review-card surface connected. That is not a failed
  run and it is not a reason to skip the work: the PR still stands on its own. Record the
  recommendation in the living document with the same per-dimension evidence you would have
  put on the card, and note that no card surface was available.
- If it returns `suppressed`, that is the anti-nag rule working correctly. Record it in the
  living document and move on. **Do not work around it** -- do not re-post, rename the
  workload, or open the PR anyway.
- If you found no material mis-sizing anywhere: no PR, no card. Update the living document
  with which workloads you checked, over what window, and what you found. That is a good
  outcome, not a failed run.

Hard rules:
- No recommendation without a sustained measurement behind it -- never from a single spike,
  a short window, or an alert threshold
- No memory decrease when the observed `max` exceeded the proposed request
- No CPU decrease while the workload is already being throttled
- No recommendation under 20% movement
- No raw nanocore or byte integers in any human-facing text
- No second PR for a workload that already has an open recommendation
- No re-proposing a workload inside its cooldown unless the mis-size materially worsened
- No numbers you could not reconcile against the team's own monitor definitions
- Nothing is ever applied: the PR is the change, and a human merges it"""
