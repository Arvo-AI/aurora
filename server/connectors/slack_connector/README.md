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
     - `member_joined_channel` — enables *instant* activation of channels
       Aurora is added to (e.g. incident.io-created channels). Without it,
       the connector page still reconciles membership every time it loads,
       just not in real time.
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

## Socket Mode (private / self-hosted deployments)

When Aurora runs somewhere Slack cannot reach with an inbound HTTP webhook — a
private VPC/Kubernetes cluster, behind a firewall, air-gapped from inbound
traffic, or on a laptop — enable **Socket Mode**. Aurora then opens an *outbound*
WebSocket to Slack and receives events/interactions over it, so no public URL,
ingress, or TLS endpoint is required. (Outbound message sending already works
over plain HTTPS regardless.)

1. In your Slack app, enable **Socket Mode** and generate an **App-Level Token**
   with the `connections:write` scope (it starts with `xapp-`).
2. Keep **Event Subscriptions** on and subscribe to the same bot events
   (`app_mention`, optionally `member_joined_channel`). No Request URL is needed
   while Socket Mode is on.
3. Configure `.env` — setting the token is all that's needed to enable it:

   ```bash
   SLACK_APP_TOKEN=xapp-...
   ```

The listener runs as its own process (`python -m services.slack.socket_mode`);
in Docker Compose it's the `slack_socket_mode` service, and in Helm it's the
`slack-socket-mode` deployment (rendered when `slackSocketMode.enabled=true` or
`secrets.backend.SLACK_APP_TOKEN` is set). It stays idle when `SLACK_APP_TOKEN`
is empty, and reuses the same event/interaction handlers as the HTTP webhook
path.

See `docs/integrations/connectors.md` for full setup details.

## Troubleshooting

**"redirect_uri did not match"** — The redirect URL sent to Slack must exactly match what's configured in your Slack App. Make sure `NGROK_URL` in `.env` matches the Redirect URL in OAuth & Permissions, and restart the server after changing it.

**Aurora doesn't reply to @mentions** — Confirm **Event Subscriptions** is enabled, the Request URL (`/slack/events`) verified successfully, and `app_mention` is listed under **Subscribe to bot events**. Reinstall the app after adding events.

**Channels Aurora is added to aren't auto-registered instantly** — Real-time pickup needs the `member_joined_channel` bot event; add it under **Subscribe to bot events** and reinstall the app. Without it, channels are still registered on connect and whenever the Slack manage page is loaded (it reconciles membership against Slack on every load) — just not the moment Aurora joins.
