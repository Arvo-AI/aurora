# Changelog

All notable changes to Aurora will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- Helm chart: in-cluster MinIO no longer fails with `ImagePullBackOff`. Upstream deleted
  `minio/minio` and `minio/mc` from Docker Hub in September 2026 and closed anonymous
  pulls on quay.io, so the chart now uses [`pgsty/silo`](https://github.com/pgsty/silo),
  a maintained AGPL fork that preserves the S3 API, `MINIO_*` variables, health routes,
  and the `.minio.sys` on-disk format — existing `minio-data` volumes are readable by
  either server, so rollback works. Pinned by digest. The bucket-creation hook moved
  from the equally-removed `minio/mc` to `amazon/aws-cli`, and now also runs on
  `post-upgrade` so a failed install-time hook can recover.

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
