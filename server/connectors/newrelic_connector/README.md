# New Relic Connector

User API Key authentication for querying New Relic via NerdGraph (GraphQL).

## Setup

### 1. Create a User API Key

1. Log in to [one.newrelic.com](https://one.newrelic.com) and go to the profile menu > **API keys** (or visit `one.newrelic.com/api-keys`)
2. Click **Create a key** and select **User** as the key type
3. Name the key (e.g., `Aurora Integration`) and save it
4. Copy the key — it starts with `NRAK-`

### 2. Find Your Account ID

Your Account ID is shown in the account dropdown or on the API keys page. It is a numeric value (e.g., `1234567`).

### 3. Identify Your Region

| Region | NerdGraph Endpoint |
|--------|-------------------|
| US | `https://api.newrelic.com/graphql` |
| EU | `https://api.eu.newrelic.com/graphql` |

### 4. (Optional) License Key

If you want Aurora to write annotations back to New Relic in the future, you can also provide a 40-character License (ingest) key. This is optional and not required for read-only RCA.

> All keys are entered by users via the UI and stored securely in Vault.

## Authentication Flow

1. User provides **User API Key** + **Account ID** + **Region** (US/EU) via the Aurora UI
2. Aurora validates the key by querying `{ actor { user { email } } }` on NerdGraph
3. Credentials are stored in HashiCorp Vault; only an encrypted reference is saved in the database

## What Aurora Queries

Aurora uses NerdGraph to:
- Execute arbitrary **NRQL queries** against any telemetry type (metrics, logs, traces, events)
- Fetch **alert issues and incidents** with filtering by state, priority, and time window
- Search **entities** (services, hosts, applications)
- List **accessible accounts** for multi-account setups

All queries go through a single endpoint: `POST https://api.newrelic.com/graphql` with the `API-Key` header.

## Webhook Configuration

Webhook URL format: `https://your-aurora-domain/newrelic/webhook/{user_id}`

In New Relic:
1. Go to **Alerts > Destinations** and create a new **Webhook** destination with the Aurora webhook URL
2. Under **Workflows**, create or edit a workflow
3. Add a notification channel using the webhook destination
4. Configure the workflow filter for the issues you want Aurora to investigate

## Polling (Alternative to Webhooks)

Aurora can also poll NerdGraph for active issues. Trigger manually via `POST /newrelic/poll-issues` or schedule via Celery Beat.

## Troubleshooting

- **Invalid API key** — Ensure the key starts with `NRAK-` and belongs to a user with read access to APM, Infrastructure, Logs, and Alerts
- **Account not found** — Verify the Account ID is correct and the API key has access to that account
- **EU region issues** — Make sure you selected "EU" in the region selector if your account is on the EU data center
