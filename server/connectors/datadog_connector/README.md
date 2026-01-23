# Datadog Connector

API Key + Application Key authentication for Datadog.

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


> API and Application keys are entered by users via the UI.

## Webhook Configuration

Webhook URL format: `https://your-aurora-domain/datadog/webhook/{user_id}`

In Datadog: **Integrations** > **Webhooks** > **+ New**
- Name: `aurora`, URL: Aurora webhook URL

In monitors, add `@webhook-aurora` to notifications.

## Troubleshooting

**Datadog connector not working** — Check that the API and Application keys are correctly configured in the UI
