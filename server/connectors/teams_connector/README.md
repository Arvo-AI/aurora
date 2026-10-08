# Microsoft Teams Connector

Hybrid setup like Google Chat: **Entra OAuth** lets an org admin connect Aurora
(for Graph channel list/history and the manage UI). **Bot Framework** delivers
@mentions and carries every **outbound** message as the Teams app ("Aurora"), not
as the connecting user.

## Setup overview

| Piece | Purpose |
|-------|---------|
| Entra app registration | OAuth connect in Aurora; delegated Graph read (channels, history) |
| Azure Bot + Teams channel | `@mention` handling; all posts as the bot |
| Teams app install | Bot must be in teams/channels where Aurora should speak |

## 1. Register an Entra application

1. [Azure Portal](https://portal.azure.com) → **Microsoft Entra ID** → **App registrations** → **New registration**
   - Name: `Aurora`
   - Supported account types: per your deployment (single tenant or multitenant)
2. **Authentication** → **Add a platform** → **Web**
   - Redirect URI (local with tunnel): `https://your-tunnel.example.com/teams/callback`
   - Redirect URI (direct backend): `https://your-api.example.com/teams/callback`
   - Microsoft does not allow bare `localhost` for production; use a tunnel for local dev ([ngrok](https://ngrok.com), etc.) and set `NGROK_URL` in `.env` (same pattern as Slack).
3. **Overview** (same app registration) → copy **Application (client) ID** → set `TEAMS_CLIENT_ID` in `.env`.
4. **Certificates & secrets** → **New client secret** → copy the **Value** column **immediately** (Azure shows it only once) → set `TEAMS_CLIENT_SECRET` in `.env`.
5. **API permissions** → **Microsoft Graph** → **Delegated permissions** — add scopes that match `TEAMS_SCOPES` in `oauth.py`:
   - `Team.ReadBasic.All`
   - `Channel.ReadBasic.All`
   - `ChannelMessage.Read.All`
   - `Chat.Read`
   - `openid`, `profile`, `offline_access`
6. **Grant admin consent** (Entra admin only — e.g. Global Administrator):
   - Stay on **API permissions** for this app.
   - Click **Grant admin consent for [tenant name]** at the top of the permissions table.
   - Confirm in the dialog.
   - Each permission should show **Granted for [tenant name]** with a green status. If the button is missing or fails, your account lacks consent rights — ask a tenant admin.
   - Without this step, each user sees a full consent prompt on **Connect** in Aurora.

## 2. Create an Azure Bot

1. Azure Portal → **Create a resource** → **Azure Bot**
2. Link it to the **same app registration** from step 1 (or create a single app used for both OAuth and bot).
3. **Configuration** → **Messaging endpoint** (must be HTTPS and reachable from Microsoft):
   - `https://your-api.example.com/teams/messages`
4. Enable the **Microsoft Teams** channel for the bot.

On the bot **Configuration** page, copy **Microsoft App ID** → set `TEAMS_APP_ID` in `.env` (usually the same UUID as `TEAMS_CLIENT_ID` when the bot uses the same app registration).

## 3. Configure `.env`

```bash
# Tunnel for local OAuth redirect (optional; same as Slack)
NGROK_URL=https://your-tunnel.example.com

# Entra → App registration → Overview → Application (client) ID
TEAMS_CLIENT_ID=

# Entra → Certificates & secrets → client secret Value (save when you create it)
TEAMS_CLIENT_SECRET=

# Azure Bot → Configuration → Microsoft App ID (often same as TEAMS_CLIENT_ID)
TEAMS_APP_ID=

TEAMS_TENANT_ID=common
```

**`TEAMS_TENANT_ID`** controls which Microsoft sign-in endpoint Aurora uses when someone clicks **Connect** in the UI. It does **not** replace the tenant stored on the connection after OAuth — that comes from whoever signed in.

| Value | What it means | Typical use |
|-------|----------------|-------------|
| `common` | Any work or school (Entra) account from any organization may sign in. | Multitenant Aurora deployments; default for most setups. |
| `organizations` | Work/school accounts only; personal Microsoft accounts (`@outlook.com`, etc.) are blocked. | B2B product, no consumer logins. |
| `{tenant GUID}` | Only users in that one Entra directory (e.g. `a1b2c3d4-…`). Copy **Tenant ID** from **Microsoft Entra ID** → **Overview**. | Single-tenant app registration, or Aurora instance dedicated to one customer org. |

Pick a value that matches your app registration under **Supported account types** (multitenant vs single tenant). Wrong combinations produce login errors at Connect time, not in channel sync.

Restart `aurora-server` after changes.

## 4. Install the app in Teams

1. Publish or sideload your **Teams app manifest** that references this bot (Teams Developer Portal or zip upload).
2. Install the app into the **teams** where Aurora should work.
3. In each **channel**, ensure the app is available (team install + channel scope as required by your manifest).

Users **@mention the bot** by its **display name** in the manifest (e.g. `@Aurora`) — same idea as `@Aurora` in Slack. In channels, only @mentions start a chat; DMs to the bot work without a mention.

## 5. Connect in Aurora

1. **Connectors** → **Microsoft Teams** → **Connect** (admin completes Entra sign-in).
2. **Teams → Manage**: refresh channels, **activate** channels Aurora should route to, set the **incident card** channel, edit **Teams memory** (tone / which channels to use).

Delegated OAuth identifies the tenant and powers Graph **reads**. **Posts** (replies, routing, incident cards) go through the **bot**.

## Troubleshooting

**Redirect URI mismatch** — Redirect in Entra must match exactly what Aurora sends (`{backend}/teams/callback`). With local dev, set `NGROK_URL` and use the tunnel URL in Entra.

**@mention gets no reply** — Confirm the messaging endpoint URL is public HTTPS, `/teams/messages` is allowed through your proxy, and the Teams app is installed. Check server logs for Bot Framework JWT verification (`TEAMS_APP_ID` / secret must match the bot registration).

**Bot posts fail (cards / routing)** — The Teams app must be **installed in the team/channel**. Proactive messages use Bot Framework; Graph delegated tokens are not used for outbound chat.

**"Unknown tenant" on @mention** — An org admin must **Connect Teams** in Aurora first so the tenant is linked to your org.

**Messages appear as a person, not the bot** — Outbound traffic should use `connectors/teams_connector/bot_client.py`. If you see a user name, report a regression — that path should not use delegated Graph send.

See also [Microsoft Teams on the connectors guide](../../../website/docs/integrations/connectors.md#microsoft-teams).
