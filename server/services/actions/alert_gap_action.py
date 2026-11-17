"""
Built-in Alert Gap Audit Action

Default instructions for the system action that audits monitoring coverage
and opens PRs/MRs with alert definitions following SRE best practices.
"""

DEFAULT_ALERT_GAP_INSTRUCTIONS = """**Step 1: Gather context**

Understand what you're working with before judging anything.

- Call get_infrastructure_context to learn the org's services, environments, and dependencies.
- Call get_connected_repos (or equivalent) to find connected code repositories.
- Search those repos for existing alerting definitions. These could be in any format:
  Terraform (.tf), Pulumi (TypeScript/Python/Go), CloudFormation (YAML/JSON),
  Helm values, Jsonnet/Grafonnet, Ansible playbooks, raw monitoring provider config
  (Datadog monitors YAML, Grafana provisioning, Prometheus alerting rules, etc.)
- Read the existing alert definitions to understand current coverage, the team's chosen
  IaC language, naming conventions, file layout, and notification routing.
- Note the VCS provider (GitHub, GitLab, Bitbucket) so you use the correct tools later.

If you cannot find any repo with alerting config, update the living document explaining
what you looked for and stop. Do not guess or hallucinate a repo structure.

**Step 2: Assess coverage honestly**

Use the Four Golden Signals (latency, traffic, errors, saturation) as a *thinking tool*
to spot blind spots -- not as a checklist where every service must have all four.
A batch job does not need latency alerts. A stateless proxy rarely needs saturation alerts.
Think about what actually matters for each service.

Things worth looking for:
- Services with zero symptom-level alerting (only infra metrics, or nothing at all)
- Cause-based alerts with no corresponding symptom coverage (CPU page but no error-rate alert)
- Thresholds without duration -- these fire on transient spikes and erode trust
- Alerts no one would act on (informational noise masquerading as pages)
- Traffic-drop blindness -- load balancer looks green but a service receives zero requests
- Dependency paths with no health signal (caches, queues, session stores, external APIs)
- Resource exhaustion with no warning (disk, certs, quotas, connection pools)
- SLOs defined but no burn-rate alert defending them

If current alerting is reasonable for the system's complexity and tier, say so.
Good coverage is a valid and desirable outcome. Do not manufacture gaps to justify output.

**Step 3: Craft alerts (only if real gaps exist)**

Write alert definitions in whatever language/format the repo already uses. If they write
Terraform, you write Terraform. If they use Pulumi TypeScript, match that. Helm values,
Jsonnet, CloudFormation -- match the existing approach exactly.

Quality rules:
- Every alert must pass the 3am test: would an engineer woken for this thank you or curse you?
- Match the repo's existing style: naming, file organization, provider versions, module
  patterns, notification channel references.
- Where the repo uses burn-rate / multi-window patterns, follow that style. Where it uses
  simple thresholds with durations, follow that. Do not impose a pattern the team has not adopted.
- Keep the changeset small and reviewable. A focused set of well-justified alerts beats
  a large dump that reviewers will ignore.

Hard anti-slop rules:
- No threshold without a duration / `for:` clause
- No duplicating coverage that already exists
- No alerts on internal metrics when a user-facing symptom signal already has coverage
- No alerts that fire on normal operational variance
- No alerts without a clear "what do I do when this fires?" answer

**Step 4: Test Prometheus alert behavior before proposing it**

For native Prometheus alert-rule YAML, call verify_prometheus_alert with the
candidate rules_yaml and a promtool tests_yaml suite. This runs the real local
Prometheus evaluator; it does not connect to production or retrieve metric history.

- Write down the expected behavior BEFORE tuning the rule: which incident-like
  signal must fire, by what evaluation time, and which healthy or transient signal
  must stay quiet. Encode both firing and non-firing assertions in the suite.
- Base those expectations on the incident, SLO, or coverage gap you actually found.
  If timing or acceptable transient behavior is unknown, ask for that context or
  leave the proposal unverified; do not invent requirements just to obtain a pass.
- Label hand-written or reconstructed series evidence_kind="synthetic". Use
  "recorded" only for supplied recorded data, and state its source and limits;
  the tool does not authenticate that provenance. Never describe synthetic cases
  as a replay of real historical metrics.
- Keep tests_yaml unchanged while comparing candidate rules. The returned
  input_hashes.tests_sha256 must match between calls. Fix the rule when a test
  fails; do not weaken assertions to make it pass. If a requirement genuinely
  changes, explain it and rerun every candidate against the revised suite.
- Only status="passed" supports saying the rule passed these specific cases.
  A failure, timeout, unavailable promtool, or validation error is NOT a pass.
  Retain actual output and input hashes; do not substitute an LLM prediction.
- Passing fixtures is evidence of behavior on the supplied series, not proof of
  production coverage, historical detection, or an absence of false positives.
- Include the tool's exact exported rules.yml and tests.yml together in the PR/MR,
  with `promtool test rules tests.yml` as the reproduction command from their
  directory. If the rule is edited after validation, validate it again. Match the
  repository's layout while keeping the relative rule_files reference valid.
- This evaluator supports native Prometheus rules only. Do not claim it verified
  Terraform/Pulumi provider resources, Grafana-managed alerts, or unrendered Helm
  templates. Preserve those formats and explicitly report that they are outside
  this verification path.

If a Prometheus proposal does not pass, report the gap and the actual test failure
in the living document instead of opening a supposedly verified alert PR/MR.

**Step 5: Open PR/MR (only if you have alerts to propose)**

- First check for existing open PRs/MRs from prior runs of this action.
  Do not open a second one if a previous proposal is still pending review.
- Create a branch, commit the alert definitions, and open a PR (GitHub/Bitbucket)
  or MR (GitLab) using the appropriate VCS tools.
- When modifying an existing alert/rules file, use the provider's anchored edit
  operation where available (Bitbucket: `edit_file`) instead of re-uploading the
  whole file.
- PR/MR body: for each proposed alert, one sentence on the gap it fills and why it matters.
  For Prometheus proposals, add the evidence kind, expected firing/non-firing
  behavior, actual test result, input hashes, and reproduction command. Do not
  call an unexecuted or failed check verified. No filler, no essay. Reviewers are busy.
- If you found no gaps worth proposing, do not open an empty or low-value PR. Instead,
  update the living document noting that coverage looks healthy and when you last checked."""
