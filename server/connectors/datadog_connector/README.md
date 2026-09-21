# Datadog Connector

API Key + Application Key authentication for Datadog.

Several Datadog organizations can be connected at once. Each needs its own API +
application key pair, so repeat the setup below for every organization and give each one a
label (e.g. `prod`, `dev`) that Aurora uses to pick the right one during an investigation.

## Setup

### 1. Create API Key

1. Go to [Datadog](https://app.datadoghq.com/) > avatar > **Organization Settings** > **API Keys**
2. Click **+ New Key**, name it `Aurora`, copy the key

### 2. Create Application Key

1. Go to **Organization Settings** > **Application Keys**
2. Click **+ New Key**, name it `Aurora`, copy the key

### 3. Identify Your Site

| Site | URL |
|------|-----|
| US1 | `datadoghq.com` |
| US3 | `us3.datadoghq.com` |
| US5 | `us5.datadoghq.com` |
| EU | `datadoghq.eu` |

Site is recorded per organization, so a dev org on `datadoghq.eu` and a prod org on
`datadoghq.com` can both be connected.

> API and Application keys are entered by users via the UI.

### 4. Repeat For Each Organization

Switch organization from the bottom-left **Accounts** menu in Datadog and repeat steps 1-3.
In Aurora, use **Add another organization** on the Datadog integration page and set a label
for each one.

## Webhook Configuration

Webhook URL format: `https://your-aurora-domain/datadog/webhook/{user_id}`

The URL is per Aurora **user**, not per Datadog organization. When several organizations are
connected, create this same webhook inside **each** of them, otherwise alerts from the
others never reach Aurora.

In Datadog: **Integrations** > **Webhooks** > **+ New**
- Name: `aurora`, URL: Aurora webhook URL

In monitors, add `@webhook-aurora` to notifications. Using the name `aurora` in every
organization keeps `@webhook-aurora` working uniformly across all of them.

## Troubleshooting

**Datadog connector not working** — Check that the API and Application keys are correctly configured in the UI

**Investigation used data from the wrong environment** — Check that every organization is
connected (Aurora's Datadog page lists them) and that each has a distinct label. An
organization whose keys are missing cannot be queried, and one whose webhook was never
created in Datadog sends no alerts at all.

**Alerts from one organization never arrive** — The webhook is per Aurora user but must be
created separately inside each Datadog organization. Confirm it exists in the organization
that is not reporting.
