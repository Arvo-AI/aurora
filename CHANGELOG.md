# Changelog

All notable changes to Aurora will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
