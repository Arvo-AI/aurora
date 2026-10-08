# Microsoft Teams Connector
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
   Also copy **Directory (tenant) ID** from **Microsoft Entra ID** → **Overview** → **Tenant ID** (you need it when creating the Azure Bot in step 2).
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

This is a **separate Azure resource** from the Entra app in step 1. If you only registered an app and never created **Azure Bot**, you will not see a messaging endpoint or Teams channel settings — go back and create the bot resource below.

### 2a. Create the bot resource

1. [Azure Portal](https://portal.azure.com) → **Create a resource** → search **Azure Bot** → **Create**.
2. On the create form:
   - **Bot handle** — any name (e.g. `aurora-teams`).
   - **Subscription** / **Resource group** — your usual choices.
   - **Pricing tier** — F0 is fine for dev.
   - **Microsoft App ID** — choose **Single tenant** (or match how you registered the app in step 1).
   - **Creation type** — **Use existing app registration**.
   - **App ID** — paste the **Application (client) ID** from step 1 (same value as `TEAMS_CLIENT_ID`).
   - **App tenant ID** — paste your org’s **Tenant ID** GUID from **Microsoft Entra ID** → **Overview** → **Tenant ID** (Directory ID). This is the Entra directory where the app registration lives — **not** the literal word `common`.  
     Do **not** confuse this with `TEAMS_TENANT_ID` in `.env`: the Azure form always wants your real tenant GUID; `TEAMS_TENANT_ID=common` is a separate OAuth sign-in setting (see §3).
3. **Review + create** → wait for **Your deployment is complete**.

### 2a (continued). Open the bot resource (not the deployment page)

Azure often lands you on a **Deployment** screen first (`Microsoft.AzureBot-… | Overview` with left menu **Overview / Inputs / Outputs / Template** only). That page has **no** Configuration or Channels — it is not the bot.

1. Click the blue **Go to resource** button on that page (under **Next steps**), **or**
2. Portal top search → type your **bot handle** → open the result whose type is **Azure Bot** (not “Deployment”).

You should now see a left menu that includes **Settings** (expand it) → **Configuration** and **Channels**. The page title is your bot name, not `Microsoft.AzureBot-… | Deployment`.

### 2b. Messaging endpoint

Microsoft sends @mentions and channel traffic to this URL. It must be **public HTTPS** (use `NGROK_URL` + `/teams/messages` for local dev, same idea as OAuth).

1. On the **Azure Bot** resource → **Settings** → **Configuration**
2. Set **Messaging endpoint** to:
   - Production: `https://your-api.example.com/teams/messages`
   - Local + tunnel: `https://your-tunnel.example.com/teams/messages`
3. **Apply** / **Save**.

On the same **Configuration** page you will see **Microsoft App ID** — it should match the app registration ID from step 1.

### 2c. Enable Teams

1. Left menu → **Channels** (under **Settings** on some layouts).
2. Click **Microsoft Teams** → **Apply** / save so the channel shows as enabled.

### 2d. Map to `.env`

| If you used… | Set in `.env` |
|--------------|----------------|
| **Use existing app registration** (recommended) | `TEAMS_CLIENT_ID` only — leave `TEAMS_APP_ID` empty. Aurora uses the client ID for the bot too. |
| OAuth app **and** bot on **different** Entra apps | `TEAMS_CLIENT_ID` = OAuth app; `TEAMS_APP_ID` = bot’s **Microsoft App ID** from **Settings → Configuration**. |

`TEAMS_CLIENT_SECRET` remains the **client secret Value** from the Entra app that owns the bot (step 1 when you use one app for both).

**Why two env vars?** OAuth and Bot Framework are two APIs; some enterprises split them across two app registrations. One app for both (this guide) needs only `TEAMS_CLIENT_ID` — `TEAMS_APP_ID` is an optional override when the bot’s Microsoft App ID differs.

## 3. Configure `.env`

```bash
# Tunnel for local OAuth redirect (optional; same as Slack)
NGROK_URL=https://your-tunnel.example.com

# Entra → App registration → Overview → Application (client) ID
TEAMS_CLIENT_ID=

# Entra → Certificates & secrets → client secret Value (save when you create it)
TEAMS_CLIENT_SECRET=

# Optional: only if the Azure Bot uses a *different* Entra app than OAuth (else leave blank)
# TEAMS_APP_ID=

TEAMS_TENANT_ID=common
```

**`TEAMS_TENANT_ID`** (in `.env` only) controls which Microsoft **sign-in** endpoint Aurora uses when someone clicks **Connect**. It is **not** the **App tenant ID** field on the Azure Bot create form (that form always uses your Entra **Tenant ID** GUID). It also does **not** replace the tenant stored on the connection after OAuth — that comes from whoever signed in.

| Value | What it means | Typical use |
|-------|----------------|-------------|
| `common` | Any work or school (Entra) account from any organization may sign in. | Multitenant Aurora deployments; default for most setups. |
| `organizations` | Work/school accounts only; personal Microsoft accounts (`@outlook.com`, etc.) are blocked. | B2B product, no consumer logins. |
| `{tenant GUID}` | Only users in that one Entra directory (e.g. `a1b2c3d4-…`). Copy **Tenant ID** from **Microsoft Entra ID** → **Overview**. | Single-tenant app registration, or Aurora instance dedicated to one customer org. |

Pick a value that matches your app registration under **Supported account types** (multitenant vs single tenant). Wrong combinations produce login errors at Connect time, not in channel sync.

Restart `aurora-server` after changes.

## 4. Install the app in Microsoft Teams (per Team)

**This is not Aurora OAuth.** **Connect** in Aurora (step 5) links your org’s Entra tenant and Graph access in Aurora. It does **not** add the bot to any Microsoft Team or channel. Teams only delivers @mentions and bot messages after the **Teams client app** (manifest) is installed where people work — like installing Aurora’s Slack app into a workspace, separate from clicking Connect in Aurora.

| Who | What |
|-----|------|
| **Platform operator** (you) | Create/publish a **Teams app package** (manifest) for your Azure Bot — once per Aurora deployment ([Teams Developer Portal](https://dev.teams.microsoft.com/) or zip sideload). Aurora does not ship this package; it must reference *your* bot. |
| **Teams admin / team owner** | **Install** that app into each **Microsoft Team** where Aurora should appear — once per Team, not every Aurora user. |
| **Org admin in Aurora** | Step 5 **Connect** + **Teams → Manage**. |
| **End users** | `@mention` the bot or DM it — no install, no OAuth. |

1. Publish or sideload a manifest whose bot ID matches your Entra / Azure Bot app.
2. In Teams: open a team → **Apps** → install your app for that team (or org catalog if published tenant-wide).
3. Confirm the app is available in channels where you @mention or receive incident cards.

Users **@mention** the bot by its manifest **display name** (e.g. `@Aurora`). In channels, @mention is required; DMs are not.

## 5. Connect in Aurora

1. **Connectors** → **Microsoft Teams** → **Connect** (org admin, Entra sign-in once per Aurora org).
2. **Teams → Manage**: refresh channels, **activate** channels, set **incident card** channel, edit **Teams memory**.

OAuth powers Graph **reads** in Aurora. **Posts** (@mention replies, cards, routing) use the **bot** and still require step 4 in each Team.

## Troubleshooting

**Redirect URI mismatch** — Redirect in Entra must match exactly what Aurora sends (`{backend}/teams/callback`). With local dev, set `NGROK_URL` and use the tunnel URL in Entra.

**No “Configuration” or “Channels”** — You are probably on (a) the **Deployment** page (`Inputs` / `Outputs` / `Template` in the sidebar) → click **Go to resource**, or (b) the Entra **App registration** → search for the **Azure Bot** resource instead. Configuration is only on the bot resource under **Settings**.

**@mention gets no reply** — Confirm **Settings → Configuration → Messaging endpoint** is public HTTPS and ends with `/teams/messages`, the Teams **channel** is enabled, and the Teams app is installed. Check server logs for Bot Framework JWT verification (`TEAMS_APP_ID` / secret must match the bot registration).

**Bot posts fail (cards / routing)** — The Teams app must be **installed in the team/channel**. Proactive messages use Bot Framework; Graph delegated tokens are not used for outbound chat.

**"Unknown tenant" on @mention** — An org admin must **Connect Teams** in Aurora first so the tenant is linked to your org.

**Messages appear as a person, not the bot** — Outbound traffic should use `connectors/teams_connector/bot_client.py`. If you see a user name, report a regression — that path should not use delegated Graph send.

See also [Microsoft Teams on the connectors guide](../../../website/docs/integrations/connectors.md#microsoft-teams).
