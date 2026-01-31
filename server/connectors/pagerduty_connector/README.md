# PagerDuty Connector

OAuth 2.0 or API Token authentication for PagerDuty.

## Authentication

### Option A: API Token (Recommended for Development)

1. Go to [PagerDuty](https://app.pagerduty.com/) > **Integrations** > **API Access Keys**
2. Click **Create New API Key**
3. Select either **Read** or **Write** permissions (both work fine)
4. Copy the generated API token
5. Enter the token via the Aurora UI when connecting PagerDuty

### Option B: OAuth

#### 1. Create OAuth App

1. Go to [PagerDuty](https://app.pagerduty.com/) > **Integrations** > **Developer Mode** > **My Apps**
2. Click **Create New App**
   - Name: `Aurora`
   - Enable **OAuth 2.0**
   - Redirect URL: `http://localhost:5000/pagerduty/oauth/callback`
3. Copy the **Client ID** and **Client Secret**

#### 2. Configure `.env`

```bash
NEXT_PUBLIC_ENABLE_PAGERDUTY_OAUTH=true
PAGERDUTY_CLIENT_ID=your-client-id
PAGERDUTY_CLIENT_SECRET=your-client-secret
```

## Webhook Configuration

### Local Development Setup

For local development, PagerDuty webhooks cannot reach `localhost:5080` directly. You need to set up port forwarding using a tunnel service like ngrok.

#### 1. Generate API Token in PagerDuty

First, ensure you have generated an API token in PagerDuty (see [Authentication](#authentication) above). Either **Read** or **Write** permissions work fine.

#### 2. Set Up Port Forwarding

**Option A: Using ngrok (Recommended)**

1. Install [ngrok](https://ngrok.com/download)
2. Start ngrok tunnel:
   ```bash
   ngrok http 5080
   ```
3. Copy the HTTPS URL (e.g., `https://abc123.ngrok-free.app`)
4. Set the `NGROK_URL` environment variable in your `.env` file:
   ```bash
   NGROK_URL=https://abc123.ngrok-free.app
   ```
5. Restart your Aurora services

**Option B: Using cloudflared**

1. Install [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/tunnel-guide/local/)
2. Start tunnel:
   ```bash
   cloudflared tunnel --url http://localhost:5080
   ```
3. Copy the HTTPS URL and set it as `NGROK_URL` in your `.env` file

#### 3. Configure Webhook in PagerDuty

1. In Aurora, navigate to the PagerDuty integration page
2. Copy the webhook URL displayed (it will automatically use your `NGROK_URL` if set)
3. Go to [PagerDuty](https://app.pagerduty.com/) > **Integrations** > **Generic Webhooks (v3)** > **New Webhook**
4. Paste the webhook URL
5. Set scope to **Account** or specific services
6. Subscribe to events:
   - `incident.triggered`
   - `incident.acknowledged`
   - `incident.resolved`
   - `incident.custom_field_values.updated`
7. Click **Add Webhook** and send a test notification to verify

### Production Setup

In production, the webhook URL will automatically use your production backend URL. No port forwarding is needed.

Webhook URL format: `https://your-aurora-domain/pagerduty/webhook/{user_id}`

## How It Works

- When `NGROK_URL` is set and the backend URL is `localhost`, Aurora automatically uses the ngrok URL for webhook links
- The webhook URL is user-specific and displayed in the Aurora UI
- Webhooks are received at `/pagerduty/webhook/{user_id}` and processed asynchronously

## Troubleshooting

**"PagerDuty OAuth is not enabled"** — Set `NEXT_PUBLIC_ENABLE_PAGERDUTY_OAUTH=true`

**"Missing OAuth credentials"** — Verify all `PAGERDUTY_*` env vars are set

**Webhook not receiving events** — Verify:
- `NGROK_URL` is set correctly in `.env` (for local development)
- ngrok/cloudflared tunnel is running
- Webhook URL in PagerDuty matches the one shown in Aurora UI
- Test notification was sent from PagerDuty
