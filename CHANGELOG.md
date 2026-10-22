# Changelog

All notable changes to Aurora will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Prompt caching that reaches Bedrock. In native mode every Claude call carries
  Converse `cachePoint` checkpoints after the tool definitions, after the static
  part of the system prompt and at the end of the last message (three per
  request, under Bedrock's cap of four, default 5-minute TTL), including explicit
  `bedrock/<geo>.anthropic.*` model ids. The system prompt is now assembled with
  the static part first (core rules, provider constraints, regional rules) and the
  per-org / per-incident / time-dependent content (command policy, rendered
  skills, memories and their ages) after the checkpoint, so that prefix is shared
  across sessions. The Anthropic API and OpenRouter keep per-block `cache_control`
  markers, now on both the static and the dynamic block when the static part
  reaches the model's cacheable minimum, and the previous single end-of-system
  breakpoint otherwise (Haiku 4.5 / Opus 4.5–4.7 on background RCA). The Bedrock
  gateway path is unchanged: markers only for `anthropic/` ids, a plain string for
  explicit `bedrock/` ids.
- Cost observability for prompt caching: `llm_usage_tracking` gains
  `cache_read_input_tokens` and `cache_write_input_tokens` (added at startup by an
  idempotent migration), `input_tokens` is the whole prompt (cache tokens
  included) and `estimated_cost` prices non-cached input, cache reads and cache
  writes at their own rates. Every Claude row now carries a 5-minute cache-write
  rate, and Claude Sonnet 5.5, Opus 5 and Opus 4.8 are priced. The Bedrock
  geo-profile premium applied to the static list prices is configurable with
  `BEDROCK_REGIONAL_PREMIUM` (default 1.10; `global.` profiles never carry it).
- Previously untracked model calls are recorded in `llm_usage_tracking`: the
  prompt-injection input rail (`input_rail`), the per-turn memory selector
  (`memory_selector`), the recommender's hypothesis extraction and diagnostics
  plan (`hypothesis_extraction`, `diagnostics_plan`) and the PR-review verdict
  extractor (`verdict_extraction`).
- Background RCA cost knobs: `RCA_CONTEXT_TOKEN_BUDGET` (default 200000) trims an
  investigation's conversation history at that many tokens instead of 80% of the
  model window (the system prompt and tool definitions are sent on top), and
  `RCA_TOOL_OUTPUT_PASS_THROUGH_CHARS` (default 20000) summarizes tool outputs
  above that size during background runs. Interactive chat keeps its previous
  thresholds.

### Changed
- The cheap model tier is now Claude Haiku 5.5 (`anthropic/claude-haiku-5.5`,
  `eu.anthropic.claude-haiku-5-5` on Bedrock in EU regions). It is selectable in
  the chat model picker and is the default for background RCA and for every
  auxiliary call: the command-safety judge and input rail, summaries, the memory
  agent and selector, severity, suggestions, email reports, visualization, the
  recurrence agent, the Jira judge and Next Steps enrichment. The
  `anthropic/claude-haiku-4.5` alias keeps resolving for existing configs.
- Background RCA cost optimization (`RCA_OPTIMIZE_COSTS`) is on by default in
  every deployment file; unset or empty now also mean "on".
- Each auxiliary call has its own model variable with a cheap-tier default:
  `TOOL_OUTPUT_SUMMARIZATION_MODEL`, `SUGGESTION_MODEL`, `EMAIL_REPORT_MODEL`,
  `VISUALIZATION_MODEL`, `SEVERITY_MODEL`, `MEMORY_AGENT_MODEL` and
  `MEMORY_SELECTOR_MODEL` are new; `SUMMARIZATION_MODEL`, `RECURRENCE_AGENT_MODEL`,
  `JIRA_RCA_JUDGE_MODEL`, `ENRICHMENT_MODEL` and `GUARDRAILS_LLM_MODEL` default to
  the cheap tier instead of `MAIN_MODEL` / `RCA_MODEL`. The cheap tier is only the
  default when the provider settings can route a Claude model
  (`LLM_PROVIDER_MODE` openrouter / anthropic / bedrock, or `ANTHROPIC_API_KEY`
  under direct routing); otherwise these variables keep falling back to
  `MAIN_MODEL`, so OpenAI / Google / Vertex / Ollama-only deployments and Bedrock
  gateways pinned to `bedrock/<id>` values are unaffected by the upgrade. A
  `MAIN_MODEL` pinned to an explicit `bedrock/<id>` keeps the cheap tier on that
  Bedrock route in every mode (`bedrock/<geo>.anthropic.claude-haiku-5-5` on the
  same geo in native mode for `us`/`eu`/`au`/`jp`/`global` pins, `MAIN_MODEL`
  otherwise) — never another provider; under `LLM_PROVIDER_MODE=bedrock` in an
  `ap-*` region with clean ids the variables follow `MAIN_MODEL`, since AWS
  publishes no `apac.` Haiku 5.5 profile.
- Extended thinking is switched off for classifier and summary calls on Claude
  models that allow it (Haiku 5.5, Sonnet 5, Opus 5, Opus 4.7/4.8); agent loops
  keep thinking on.
- `langchain-aws` 1.3.1 → 1.9.0 (with `langchain-core` 1.6.13 and `boto3`
  1.43.117): Claude Haiku 5.5 support on Bedrock, request-level prompt-cache
  points, real `timeout` / `max_retries` fields and cache-aware usage metadata.
- Bedrock docs and examples use EU inference profiles, IAM roles instead of
  static keys, and carry a data-residency warning next to `BEDROCK_REGION=global`.
- Every model call now carries an output-token cap by call type, configurable
  with `LLM_MAX_TOKENS_JUDGE` (400), `LLM_MAX_TOKENS_SEVERITY` (32),
  `LLM_MAX_TOKENS_SUMMARY` (1500), `LLM_MAX_TOKENS_REPORT` (4000),
  `LLM_MAX_TOKENS_AGENT` (16000, Claude agent turns on Bedrock / Anthropic) and
  `LLM_MAX_TOKENS_MEMORY_AGENT` (2000). The auxiliary caps apply only where
  thinking is really off for the call (Claude models that accept `thinking:
  disabled` on Bedrock native / the Anthropic API, and models that never think
  unless asked); on a model that keeps thinking — Opus 5.5 / Sonnet 5.5, OpenAI
  reasoning models, Gemini 2.5+, anything through a Bedrock gateway or
  OpenRouter, Ollama — the cap is dropped and the model keeps its default output
  limit, so reasoning tokens cannot truncate a verdict or summary.
- The command-safety judge sends a trimmed copy of the user message (first line
  plus a head and tail window instead of the whole alert payload), runs with a
  small output cap and a client request timeout bounded to its 10 s budget, and
  remembers its verdict per (session, message, command) for
  `COMMAND_SAFETY_VERDICT_TTL_SECONDS` (default 900 s; keys hold only digests,
  errors and timeouts are never cached). It still runs for every command that
  passes the signature layer, and still fails closed.
- The memory extraction agent runs on `MEMORY_AGENT_MODEL` with at most 8 turns,
  a 2K output cap, and only when the session has a new user message since the
  last extraction; its model calls are recorded as `memory_extraction`.
- Oversized tool outputs are summarized once per output on the cheap
  summarization model from a head + tail window (40K + 40K chars) with a 1.5K
  output cap, and the result is shared by the context store and the agent loop.
  When the configured summarization model has no provider in the deployment
  (for example a `SUMMARIZATION_MODEL=google/gemini-2.5-flash` kept from an older
  install under `LLM_PROVIDER_MODE=bedrock` without Google credentials — the
  value `TOOL_OUTPUT_SUMMARIZATION_MODEL` inherits), the shared summarizer logs
  one warning per model and runs the summary on the cheap-tier default instead
  of hard-truncating the tool output; network and model errors still fail the
  summary as before. The provider registry raises a typed
  `ProviderUnavailableError` (a `RuntimeError` subclass) for that case.
- The cheap tier's Bedrock routability check applies in gateway mode too: a
  Bedrock gateway configured with an `ap-*` region receives the same derived
  `apac.` profile id as native mode (no Haiku 5.5 profile exists there), so the
  auxiliary variables follow `MAIN_MODEL` in that case instead of handing the
  fail-closed judge and input rail a model the gateway cannot serve.
- Bedrock native clients use botocore `standard` retries (`BEDROCK_MAX_ATTEMPTS`,
  default 3), a 10 s connect timeout and a `BEDROCK_READ_TIMEOUT` read timeout
  (default 300 s); the gateway client retries twice. Non-streaming auxiliary
  calls (judge, summaries, severity, memory selector and agent) share one model
  instance per construction signature instead of building new clients per call.
- Celery summarization tasks retry only on transient model / transport errors
  (throttling, every 5xx except 501/505 — including Anthropic's 529 Overloaded —
  and timeouts), and a retry of the post-RCA summary reuses the summary a
  previous attempt already stored instead of calling the model again.
- The agent restarts a whole run at most `AGENT_STREAM_RESTART_ATTEMPTS` times
  (default 1) and only on a genuine mid-stream transport error; errors the SDK
  already retried (`ModelTimeoutException`, `ThrottlingException`, 5xx) no
  longer trigger a restart that re-bills every previous turn.

### Fixed
- `ENRICHMENT_MODEL` and `RCA_OPTIMIZE_COSTS` passed through Docker Compose as an
  empty string were taken literally (an empty model id; cost optimization off).
  Every model variable now treats empty as unset.
- Per-call `request_timeout` / `timeout` kwargs reached Bedrock native mode as
  unknown request-body fields (ValidationException). Call options are now
  provider-neutral (`max_tokens`, `request_timeout`, `disable_thinking`) and each
  provider translates them to its own client fields.
- Claude Haiku 5.5 is priced with its two tiers (prompts up to 100K tokens vs.
  above) instead of the generic default rate.
- The memory extraction agent was enqueued twice per background RCA (once by the
  workflow, once by the RCA task) and ran on `MAIN_MODEL` for up to 30 turns.
- Large tool outputs were summarized twice per call (once for the context store,
  once for the agent loop) by `MAIN_MODEL` with no output cap.
- The command-safety judge recorded its usage as model `main` with zero output
  tokens; it now records the resolved model, the serving provider and the real
  token counts. Conversation compression is labelled `context_compression`
  instead of `tool_output_summarization`.
- A per-call `request_timeout` on Bedrock native mode would have switched the
  retry policy off (langchain-aws merges its `timeout` field over the botocore
  config with `retries=None`); the timeout now lives in the botocore config
  itself.
- Bedrock model ids (`bedrock/eu.anthropic.claude-opus-5-5`, bare geo profiles,
  dated `…-20251001-v1:0` ids) were priced at the generic default rate and given
  the conservative 180K context window; they now resolve to their Anthropic
  pricing row and real window. Cache writes were ignored in the cost estimate and
  cached tokens were subtracted from a prompt count that did not contain them on
  Bedrock.
- Usage rows of the orchestrator, the `LLMManager` fallback path and the
  `tracked_invoke` helper were labelled with the model-id prefix (`anthropic`)
  even when Bedrock served the call; they now carry the serving provider, which
  also drives the regional premium for pinned `bedrock/` ids under
  `LLM_PROVIDER_MODE=direct`. Jira judge rows were labelled `openrouter` and
  visualization rows with the routing mode (`direct`); a call tracked without a
  label now defaults to the serving provider as well.
- The context safety net trimmed the history to its most recent messages, so a
  trimmed request could open with an assistant turn (rejected by Bedrock) and
  lose the alert / task message, and the cut slid by one tool turn per call,
  invalidating the cached prompt prefix every turn. It now always keeps the
  leading task message, cuts at a tool-call boundary, trims down to 60% of the
  limit and reuses that cut until the limit is exceeded again.
- The Bedrock embedding client (`EMBEDDING_MODEL=bedrock/amazon.titan-embed-text-v2:0`)
  required static `BEDROCK_ACCESS_KEY_ID` / `BEDROCK_SECRET_ACCESS_KEY` (an IAM
  role silently disabled embeddings), passed `BEDROCK_REGION=global` to boto3 as a
  region, and re-embedded both titles on every comparison (up to 40 `InvokeModel`
  calls per alert). It now shares the chat provider's region and credential
  resolution (default credential chain, standard retries, short timeouts) and
  caches embeddings per text, so an alert is embedded once per correlation run.

## [1.5.10] - 2026-10-16

### Added
- Microsoft Teams connector. Connect with a Microsoft work account, install the
  Aurora app in each team, and @mention it in a channel or a direct message to
  start an investigation. Incident cards, failure notices, and post-investigation
  routing post as the Aurora bot. Channels can be activated, described, and
  chosen as the incident-card channel, and Teams behaviour is an editable policy
  memory. Until the app is in the org catalog, setup offers a package to upload
  as a custom app.
- One routing pass per completed investigation covers every connected chat
  platform. With Slack and Teams both connected, that run reads both policies
  and posts where each one says to.

### Changed
- Slack and Teams share the channel and memory manage pages. A Slack-only org
  keeps the same Slack policy text and @mention behaviour.
- Post-investigation routing no longer depends on the Slack incident-card
  toggle. It runs once when Slack or Teams is connected, and a card refresh
  does not run it again.

## [1.5.9] - 2026-10-16

### Added
- Jira: automatic RCA starts only for a saved project and ticket type. Until a
  rule is saved, webhooks are ignored, including the previous hardcoded types
  (Bug, Incident, Problem, Defect, Production Issue). Rules are edited on the
  Jira connector page. Asking Aurora to investigate a specific ticket is
  unchanged, and commenting back stays a separate switch.

### Fixed
- SSH: managed SSH keys are visible to the rest of the org. List, rename, and
  private-key load follow the same org check as manual VMs, and terminal
  sessions mount those keys. Private-key load stays limited to `aurora_ssh`
  rows.
- Slack: replies mention the asker with Slack's `<@USERID>` token, so the
  mention notifies them. Aurora's own mention is stripped from the question;
  other people's mentions are kept.
- SSO: SAML login stores the member's first and last name. A `name` claim that
  is only the given name no longer hides the surname. A later login fills in a
  surname only when the stored name is that given name; any other stored name
  is left alone.

## [1.5.8] - 2026-10-15

### Added
- Models: Gemini 4 Argon (`gemini-4-argon`) in the Google and Vertex pickers.
  Introductory price is $2/$10 per 1M tokens through November 30, 2026.
- GCP: Ask-mode and agent investigations can query BigQuery. The read-only
  identity gets `bigquery.dataViewer` and `bigquery.jobUser` (existing OAuth
  connections are synced once), and `bq` receives the access token and project
  at exec time.

### Fixed
- GCP: `bq query` SELECTs are no longer a false-positive HITL when the org
  already allows `bq`. The command is gated as `bq`, not `gcloud bq`. Write
  SQL and mutating `bq` subcommands stay denied, and a token on the command
  is not logged.

## [1.5.7] - 2026-10-13

### Fixed
- Org: removing a member who had saved onboarding selections failed, because
  `onboarding_selections` has no `ON DELETE CASCADE`. Those rows are deleted
  with the other foreign-key cleanup before the user is removed.
- Actions and connectors: deleting a user left scheduled actions pointing at a
  missing owner, so later runs failed at session creation, and removed the
  connectors they had connected, so the rest of the org lost them. Both are
  reassigned to the oldest other admin before the user row is removed. A
  connector that admin already has is dropped instead of duplicated. The
  scheduler retries the same reassignment for owners who are already gone.
- Azure: `cloud_exec` prefixed every command with `az` before the org
  allowlist, so `kubectl logs` and `kubectl top` were checked as `az kubectl
  ...` and rejected even when the Observability Only template allows them.
  Standalone CLIs (`kubectl`, `oc`, `helm`, `terraform`, `gsutil`, `bq`) are
  now gated as written. Provider subcommands are still prefixed.

## [1.5.6] - 2026-10-08

### Added
- Auth: per-organization SAML 2.0 single sign-on (Entra ID, Okta, Google
  Workspace, Keycloak, or any SAML IdP). Admins configure it under
  Settings → Single Sign-On, verify their email domain with a DNS TXT record,
  and can optionally require SSO for verified-domain members (admins exempt).
  Domain verification is enforced only when `SSO_ENFORCE_DOMAIN_VERIFICATION`
  is `true`.
- Connectors: register a custom remote MCP server by name and URL. Auth
  (none, OAuth, or token) and transport are detected, tools are exposed to the
  agent, and tools not known to be read-only need confirmation in chat and are
  withheld from background RCA. Per-tool overrides are available.
- Hooks: `after_user_created` fires after password, GitHub, and admin signups
  so deployments can track new users without patching routes. Fails open.

### Fixed
- Cloudflare: firewall and rate limiting rules are read through the Rulesets
  API, since Cloudflare retired the old endpoints (`410 Gone`). Adds managed,
  redirect, cache, config, origin, and transform rules, covers every account
  the token can see, and reports totals when a list is truncated. Firewall
  rule output renames `filter_expression` to `expression`.

### Security
- Next.js 15.5.27 (cache poisoning and metadata route disclosure advisories).

## [1.5.5] - 2026-10-07

### Added
- Auth: users who forgot their password can request a 6-digit reset code by
  email and set a new one without an admin. Signup and reset code emails now
  say to check the spam folder.
- incident.io: when an alert-triggered RCA has no open incident to update
  (escalation-only alerts), Aurora posts the root cause as a note on the alert
  itself. Gated by the existing org-wide post-back toggle.

### Fixed
- Secrets backend: a Vault that was sealed, unreachable, or holding an expired
  token no longer stays marked unavailable until the process restarts. Failed
  init is retried, and credential errors name the secrets backend when that is
  the actual cause instead of the cloud provider.
- Slack: every background reply includes a link back to the Aurora session, and
  the link is kept when the message is trimmed to Slack's length limit.
- Slack: channel descriptions converge on a 15-minute backfill, so member
  channels past the per-pass enqueue cap become visible to agent routing
  without anyone reloading the Manage page.
- Jira: RCA comment-back is now an org setting, off until someone turns it on.
  Existing orgs stop posting on upgrade until they opt in on the connector
  page. Comment-only vs create-and-comment is unchanged once the switch is on.

### Security
- Closed open dependency and workflow alerts: Werkzeug 3.1.9, axios, and the
  remaining npm findings (postcss-selector-parser, tinypool, katex), plus
  replacing `wget` with pinned-protocol `curl` in image and linter builds.

### Added
- incident.io: completed RCAs are now posted back onto the incident itself as an
  incident update, so the summary rides incident.io's own notification flow
  (incident channel, followers, app notifications) instead of a separate Aurora
  ping. Alert-triggered RCAs resolve to the incident the alert is attached to;
  declined/merged/cancelled incidents are skipped, and several alerts of one
  group on one incident post only once. Gated by the existing org-wide post-back
  toggle. Replaces the previous polling task, which missed any RCA longer than
  ~6 minutes and skipped alert-triggered incidents entirely.

### Fixed
- AWS: `AssumeRole` no longer fails with `AccessDenied` for org members who
  hadn't run onboarding. AWS connectors are org-shared but ExternalIds are
  per-workspace, and every AssumeRole path read the ExternalId from the
  *caller's* workspace — minting a fresh random one as a side effect on a read
  path, so there was no missing-workspace error, just a secret no trust policy
  had ever seen. The ExternalId is now resolved read-only from the connection's
  own `workspace_id` and fails closed. Fixed in all three AssumeRole paths
  (single-account, multi-account fan-out, and `get_credentials_from_db`), and
  the `AccessDenied` message now names both possible causes.

## [1.5.3] - 2026-10-01

### Fixed
- Helm chart: `Chart.yaml` `appVersion` was left at `1.5.1` when 1.5.2 bumped the
  chart version. Because `image.tag` defaults to `appVersion`, **chart 1.5.2
  deployed 1.5.1 application images** — anyone who installed or upgraded to chart
  1.5.2 without pinning `image.tag` has been running 1.5.1 application code and
  should upgrade to 1.5.3. It silently omitted the five app-code fixes in the
  release, including the case-insensitive email login fix that prevented account
  lockout (#672). `version` and `appVersion` are now asserted equal in CI, so a
  chart-only bump can no longer ship a stale `appVersion`.
- Helm chart: `helm upgrade --reuse-values` no longer fails to render with
  "Postgres connection budget ... but the in-cluster Postgres allows 100". The
  connection-budget guard added in 1.5.2 read `services.postgres.maxConnections`,
  whose new default of 300 lived only in `values.yaml` — and `--reuse-values`
  skips new chart defaults, so it arrived unset and the guard compared against the
  hardcoded fallback of 100. The default is now inline in the
  `aurora.postgresMaxConnections` helper, matching the existing `aurora.minioImage`
  pattern. Only affected in-cluster Postgres (`services.postgres.enabled: true`).

## [1.5.2] - 2026-09-30

### Fixed
- Helm chart: in-cluster MinIO no longer fails with `ImagePullBackOff`. `minio/minio`
  and `minio/mc` were deleted from Docker Hub in September 2026, so the chart now uses
  [`pgsty/silo`](https://github.com/pgsty/silo) (digest-pinned) — a maintained fork
  with the same on-disk format, so existing volumes need no migration. The
  bucket-creation hook moved to `amazon/aws-cli` and can now recover on upgrade.

## [1.1.1] - 2026-03-06

### Fixed
- Slack environment variable raising a ValueError

## [1.1.0] - 2026-03-06

### Added
- SharePoint connector
- BigPanda connector
- CloudBees connector
- ThousandEyes connector
- Jenkins connector
- Bitbucket Cloud connector with agent tools (human approval for destructive actions)
- Dynatrace connector
- Coroot connector
- AWS multi-account STS AssumeRole support
- Postmortem generation (backend and frontend)
- VM deployment guide

### Changed
- Slack, Bitbucket, Confluence, BigPanda, and ThousandEyes connectors enabled by default
- Dynatrace promoted out of feature flag

### Fixed
- Dynatrace authentication
- Summary model name resolution
- 30 GitHub security alerts resolved
- Dependency updates (minimatch, pip packages)

## [1.0.1] - 2026-01-22

Initial open source release.

[1.1.1]: https://github.com/Arvo-AI/aurora/releases/tag/v1.1.1
[1.1.0]: https://github.com/Arvo-AI/aurora/releases/tag/v1.1.0
[1.0.1]: https://github.com/Arvo-AI/aurora/releases/tag/v1.0.1
