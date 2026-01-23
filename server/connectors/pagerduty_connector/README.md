# PagerDuty Connector

OAuth 2.0 or API Token authentication for PagerDuty.

## Option A: OAuth

### 1. Create OAuth App

1. Go to [PagerDuty](https://app.pagerduty.com/) > **Integrations** > **Developer Mode** > **My Apps**
2. Click **Create New App**
   - Name: `Aurora`
   - Enable **OAuth 2.0**
   - Redirect URL: `http://localhost:5000/pagerduty/oauth/callback`
3. Copy the **Client ID** and **Client Secret**

### 2. Configure `.env`

```bash
NEXT_PUBLIC_ENABLE_PAGERDUTY_OAUTH=true
PAGERDUTY_CLIENT_ID=your-client-id
PAGERDUTY_CLIENT_SECRET=your-client-secret
```

## Option B: API Token

1. Go to [PagerDuty](https://app.pagerduty.com/) > **Integrations** > **API Access Keys**
2. Click **Create New API Key**, copy it
3. Users enter the token via the UI

## Webhook Configuration

Webhook URL format: `https://your-aurora-domain/pagerduty/webhook/{user_id}`

In PagerDuty: **Integrations** > **Generic Webhooks (v3)** > **New Webhook**
- Subscribe to: `incident.triggered`, `incident.acknowledged`, `incident.resolved`

## Troubleshooting

**"PagerDuty OAuth is not enabled"** — Set `NEXT_PUBLIC_ENABLE_PAGERDUTY_OAUTH=true`

**"Missing OAuth credentials"** — Verify all `PAGERDUTY_*` env vars are set
