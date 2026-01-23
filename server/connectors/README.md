# Aurora Connectors

Backend configuration guides for Aurora connectors.

## Cloud Providers

| Connector | Auth Type | Guide |
|-----------|-----------|-------|
| **GCP** | OAuth 2.0 | [Setup](./gcp_connector/README.md) |
| **AWS** | IAM Role + External ID | [Setup](./aws_connector/README.md) |
| **Azure** | Service Principal | [Setup](./azure_connector/README.md) |
| **OVH** | OAuth 2.0 (multi-region) | [Setup](./ovh_connector/README.md) |

## Source Control & Collaboration

| Connector | Auth Type | Guide |
|-----------|-----------|-------|
| **GitHub** | OAuth App | [Setup](./github_connector/README.md) |
| **Slack** | OAuth 2.0 | [Setup](./slack_connector/README.md) |

## Monitoring & Observability

| Connector | Auth Type | Guide |
|-----------|-----------|-------|
| **PagerDuty** | OAuth / API Token | [Setup](./pagerduty_connector/README.md) |
| **Grafana** | API Token | [Setup](./grafana_connector/README.md) |
| **Datadog** | API Key + App Key | [Setup](./datadog_connector/README.md) |
| **Netdata** | API Token | [Setup](./netdata_connector/README.md) |

## Quick Start

1. Create credentials in the provider's console (OAuth app, API keys, etc.)
2. Add environment variables to `.env`
3. Restart Aurora: `make dev`
