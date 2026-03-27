# Slack Connector

OAuth 2.0 authentication for Slack workspaces.

## Setup

### 1. Create Slack App

1. Go to [Slack API Apps](https://api.slack.com/apps) > **Create New App** > **From scratch**
   - App Name: `Aurora`
   - Select your workspace
2. **Set up port forwarding** — Slack does not allow `localhost` redirect URIs. Use a tunnel like [ngrok](https://ngrok.com):
   ```bash
   ngrok http 5080
   ```
   Copy the `https://xxxx.ngrok-free.app` URL.
3. Go to **OAuth & Permissions**
   - Add Redirect URL: `https://xxxx.ngrok-free.app/slack/callback`
4. Add **Bot Token Scopes**:
   - `chat:write`, `channels:read`, `channels:history`, `channels:join`
   - `app_mentions:read`, `users:read`
5. Go to **Basic Information** and copy:
   - **Client ID**
   - **Client Secret**
   - **Signing Secret**

### 2. Configure `.env`

```bash
NGROK_URL=https://xxxx.ngrok-free.app
SLACK_CLIENT_ID=your-slack-client-id
SLACK_CLIENT_SECRET=your-slack-client-secret
SLACK_SIGNING_SECRET=your-signing-secret
```

The `NGROK_URL` env var tells the backend to use the tunnel URL for the OAuth redirect instead of `localhost`.

## Troubleshooting

**"redirect_uri did not match"** — The redirect URL sent to Slack must exactly match what's configured in your Slack App. Make sure `NGROK_URL` in `.env` matches the Redirect URL in OAuth & Permissions, and restart the server after changing it.
