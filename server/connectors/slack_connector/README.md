# Slack Connector

OAuth 2.0 authentication for Slack workspaces.

## Setup

### 1. Create Slack App

1. Go to [Slack API Apps](https://api.slack.com/apps) > **Create New App** > **From scratch**
   - App Name: `Aurora`
   - Select your workspace
2. Go to **OAuth & Permissions**
   - Add Redirect URL: `http://localhost:5080/slack/callback` (for local dev)
   - OR use ngrok URL: `https://your-ngrok-url.ngrok-free.dev/slack/callback` (when using ngrok)
3. Add **Bot Token Scopes**:
   - `chat:write`, `channels:read`, `channels:history`, `channels:join`
   - `app_mentions:read`, `users:read`
4. Go to **Basic Information** and copy:
   - **Client ID**
   - **Client Secret**
   - **Signing Secret**

### 2. Configure `.env`

```bash
NEXT_PUBLIC_ENABLE_SLACK=true
SLACK_CLIENT_ID=your-slack-client-id
SLACK_CLIENT_SECRET=your-slack-client-secret
SLACK_SIGNING_SECRET=your-signing-secret
```

## Troubleshooting

**"bad_redirect_uri"** — Redirect URL must match exactly in Slack App. Use ngrok URL when tunneling externally.

**Slack connector not enabled** — Ensure `NEXT_PUBLIC_ENABLE_SLACK=true` and restart Aurora
