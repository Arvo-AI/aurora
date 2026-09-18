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
4. Add **Bot Token Scopes** (these must match `SLACK_SCOPES` in `server/connectors/slack_connector/oauth.py`):
   - `app_mentions:read` — listen for @Aurora mentions
   - `chat:write` — send messages
   - `channels:join` — join public channels
   - `channels:manage` — create channels, invite users, set topics
   - `channels:read` — list public channels
   - `channels:history` — read public channel history
   - `groups:read`, `groups:history`, `groups:write` — private channels
   - `im:write`, `im:history` — direct messages
   - `mpim:write`, `mpim:history` — group direct messages
   - `users:read`, `users:read.email` — read user info / email
5. Go to **Event Subscriptions** and toggle **Enable Events** on
   - **Request URL**: `https://xxxx.ngrok-free.app/slack/events`
     (Slack sends a one-time `url_verification` challenge; the server must be
     running and reachable for the URL to verify.)
   - Under **Subscribe to bot events**, add:
     - `app_mention` — required so Aurora replies when @mentioned
     - `member_joined_channel` — enables *instant* registration of channels
       Aurora is added to (e.g. incident.io-created channels). Without it,
       those channels are still picked up on connect and via **Refresh
       channels**, just not in real time.
   - Save changes. If you already installed the app, Slack will prompt you to
     **reinstall** so the new events/scopes take effect.
6. Go to **Basic Information** and copy:
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

**Aurora doesn't reply to @mentions** — Confirm **Event Subscriptions** is enabled, the Request URL (`/slack/events`) verified successfully, and `app_mention` is listed under **Subscribe to bot events**. Reinstall the app after adding events.

**Channels Aurora is added to aren't auto-registered instantly** — Real-time pickup needs the `member_joined_channel` bot event; add it under **Subscribe to bot events** and reinstall the app. Without it, channels are still registered on connect and via the **Refresh channels** button — just not the moment Aurora joins.
