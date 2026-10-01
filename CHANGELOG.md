# Changelog

All notable changes to Aurora will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
