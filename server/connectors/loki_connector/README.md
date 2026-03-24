# Loki Connector

API Token / Basic Auth authentication for Grafana Loki (Cloud or self-hosted).

## Setup

### Grafana Cloud

1. Go to [Grafana Cloud](https://grafana.com/) > your stack > **Loki** details
2. Note the **URL** (e.g. `https://logs-prod-us-central1.grafana.net`) and **Instance ID**
3. Go to **Security** > **Access Policies** > create a policy with `logs:read` scope
4. Generate a token from that policy

In Aurora: enter the Loki URL, Instance ID as username, and the token.

### Self-hosted

1. Note your Loki URL (e.g. `https://loki.internal:3100`)
2. If behind a reverse proxy with basic auth, use those credentials
3. For multi-tenant setups, enter the tenant/org ID as the username

> Credentials are entered by users via the UI and stored in Vault.

## Authentication

- **Grafana Cloud**: Basic auth (`username=instance_id`, `password=access_policy_token`)
- **Self-hosted (with reverse proxy)**: Basic auth (reverse proxy credentials)
- **Self-hosted (no auth)**: Bearer token or no auth header
- **Multi-tenant**: `X-Scope-OrgID` header set automatically when username/tenant ID is provided

## Troubleshooting

**Connection validation fails** — Check that the URL is reachable and the token has `logs:read` scope.

**"Unable to reach Loki"** — Verify the URL includes the correct protocol (https://) and port.
