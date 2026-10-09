# Microsoft Teams Connector
## Setup overview

| Piece | Where you do it | Purpose |
|-------|-----------------|--------|
| Entra app registration | **Azure Portal** | OAuth **Connect** in Aurora; Graph read (channels, history) |
| Azure Bot + **Channels → Microsoft Teams** | **Azure Portal** | Messaging endpoint; Bot Framework receives @mentions |
| **Teams client app** (manifest) | **[Teams Developer Portal](https://dev.teams.microsoft.com/)** — **not Azure Portal** | What users see under **Teams → Apps** and add to a team |
| Install app per team | **Teams client** (desktop/web) | Microsoft only delivers @mentions where this app is installed |

> **Common mistake:** Finishing **Azure Bot** (including **Channels → Microsoft Teams → Apply**) does **not** put an app in **Teams → Apps**. Azure wires the *backend*; you still need step **3** in the [Teams Developer Portal](https://dev.teams.microsoft.com/) so people can install the bot in a team.

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

### 2c. Enable Teams (Azure — not the same as “install in a team”)

1. Left menu → **Channels** (under **Settings** on some layouts).
2. Click **Microsoft Teams** → **Apply** / save so the channel shows as enabled.

This only tells Microsoft’s bot service that Teams *may* talk to your endpoint. It does **not** create an entry in the **Teams → Apps** store/catalog. Go to **§3** next.

### 2d. Map to `.env`

| If you used… | Set in `.env` |
|--------------|----------------|
| **Use existing app registration** (recommended) | `TEAMS_CLIENT_ID` only — leave `TEAMS_APP_ID` empty. Aurora uses the client ID for the bot too. |
| OAuth app **and** bot on **different** Entra apps | `TEAMS_CLIENT_ID` = OAuth app; `TEAMS_APP_ID` = bot’s **Microsoft App ID** from **Settings → Configuration**. |

`TEAMS_CLIENT_SECRET` remains the **client secret Value** from the Entra app that owns the bot (step 1 when you use one app for both).

**Why two env vars?** OAuth and Bot Framework are two APIs; some enterprises split them across two app registrations. One app for both (this guide) needs only `TEAMS_CLIENT_ID` — `TEAMS_APP_ID` is an optional override when the bot’s Microsoft App ID differs.

## 3. Create the Teams app (Developer Portal — required to see the bot in Teams)

Do this in **[Teams Developer Portal](https://dev.teams.microsoft.com/)**, not in Azure Portal. Until this step is done, **Teams → Apps** will not list your bot for users to add to a team.

1. Sign in → **Apps** → **+ New app**.
2. **App name** and **Package name** (e.g. Aurora) → **Create**.
3. Left nav → **App features** → **Bot** → **Set up** (or **Existing bot** / enter bot ID manually).
4. Link the bot: paste the **Microsoft App ID** manually if the dropdown is empty (normal for Azure Bot resources). Use the same GUID as Entra step 1 / Azure Bot **Configuration** (`TEAMS_CLIENT_ID`) — **not** the Teams app’s **App ID** on the **Basic** tab.
5. **What can your bot do?** (leave all of these **unchecked** for Aurora):

   | Option | Aurora |
   |--------|--------|
   | Upload and download files | Off — not used today |
   | Only send notification | **Off** — if enabled, users cannot @mention the bot and Aurora cannot handle channel messages |
   | Supports audio calls | Off |
   | Supports video calls | Off |

6. **Select the scopes where people can use your bot:** enable **Team** (required for channel @mentions and incident cards). Enable **Personal** only if you want DMs; **Group chat** is optional.
7. **Save** (top).
8. **Distribute** (left) → **Publish to your org** (needs Teams admin approval), **or** use **Preview in Teams** / **Download** zip and **Upload a custom app** in Teams if tenant policy allows.

After approval, users find the app under **Teams → Apps → Built for your org** (name from step 2), not under Azure Portal.

### Validate the app package (debug upload / parsing failures)

Teams often shows **Manifest parsing error message unavailable** with no detail. Use Microsoft’s validator first:

1. **[App package validation](https://dev.teams.microsoft.com/tools/store-validation)** → upload the same `.zip` from **Distribute → Download** (or your sideload package).
2. Fix **errors** before uploading in Teams; warnings are worth fixing too.

**Package shape** — at the **root** of the zip (no subfolder): `manifest.json`, `color.png` (192×192), `outline.png` (32×32). Do **not** upload a manifest from **Entra** or re-zip on macOS with an extra parent folder.

**Developer Portal (Basic)** — set **Full name** as well as short name; missing `name.full` in the exported manifest can fail upload even when the portal saves.

**Common validator findings (Aurora bot, Team scope):**

| Finding | Fix |
|--------|-----|
| **`supportsChannelFeatures` required** (manifest **1.25+** and `team` scope) | Add at the root of `manifest.json`: `"supportsChannelFeatures": "tier1"`. Save in the portal **Advanced** manifest editor if available; if the property disappears after reload, edit the downloaded `manifest.json`, re-zip (three files at top level), validate again. |
| **`webApplicationInfo` / missing `resource`** | Aurora does **not** need bot SSO for @mentions. **Remove** the whole `webApplicationInfo` block, **or** set `"resource": "api://botid-{bot-microsoft-app-id}"` with the same GUID as `bots[].botId` / `TEAMS_CLIENT_ID`. |
| Full description repeats short description | Use a longer **full** description on **Basic** (or in the manifest). |

Prefer **Distribute → Publish to your org** when policy allows; use the validator + sideload only when you must.

## 4. Configure `.env`

```bash
# Tunnel for local OAuth redirect (optional; same as Slack)
NGROK_URL=https://your-tunnel.example.com

# Entra → App registration → Overview → Application (client) ID
TEAMS_CLIENT_ID=

# Entra → Certificates & secrets → client secret Value (save when you create it)
TEAMS_CLIENT_SECRET=

# Optional: only if the Azure Bot uses a *different* Entra app than OAuth (else leave blank)
# TEAMS_APP_ID=

TEAMS_TENANT_ID=<your Entra tenant GUID>
```

**`TEAMS_TENANT_ID`** (in `.env` only) controls which Microsoft **sign-in** endpoint Aurora uses when someone clicks **Connect** (`login.microsoftonline.com/{TEAMS_TENANT_ID}/...`). It is **not** the **App tenant ID** field on the Azure Bot create form (that form always uses your Entra **Tenant ID** GUID). It also does **not** replace the tenant stored on the connection after OAuth — that comes from whoever signed in.

| App registration **Supported account types** | Set `TEAMS_TENANT_ID` to |
|---------------------------------------------|---------------------------|
| **Single tenant** (most self-hosted Aurora) | Your **Tenant ID** GUID — **do not use `common`** (Microsoft returns `invalid_request` / AADSTS about `/common` not supported) |
| **Multitenant** | `common`, `organizations`, or a specific tenant GUID |

| Value | What it means | Typical use |
|-------|----------------|-------------|
| `common` | Any work or school (Entra) account from any organization may sign in. | **Multitenant app registration only** — not valid with single-tenant apps. |
| `organizations` | Work/school accounts only; personal Microsoft accounts (`@outlook.com`, etc.) are blocked. | B2B product, no consumer logins. |
| `{tenant GUID}` | Only users in that one Entra directory (e.g. `a1b2c3d4-…`). Copy **Tenant ID** from **Microsoft Entra ID** → **Overview**. | Single-tenant app registration, or Aurora instance dedicated to one customer org. |

Pick a value that matches your app registration under **Supported account types** (multitenant vs single tenant). Wrong combinations produce login errors at Connect time, not in channel sync.

Restart `aurora-server` after changes.

## 5. Install the app in Microsoft Teams (per team — end users / team owners)

**This is not Aurora OAuth.** **Connect** in Aurora (§6) must happen **before** a useful @mention test — until then Aurora logs “unknown tenant” and ignores the bot. Connect links your Entra tenant; it does **not** install the bot into any Microsoft Team. Teams only delivers @mentions after the **Teams client app** (manifest) is installed where people work — like Slack’s workspace app install, separate from Connect in Aurora.

| Who | What |
|-----|------|
| **Platform operator** | §1–§4 (Entra, Azure Bot, Developer Portal, `.env`). |
| **Team owner** | Install the app from **Teams → Apps** into each team (below). |
| **Org admin in Aurora** | §6 **Connect** + **Teams → Manage**. |
| **End users** | `@mention` the bot — no OAuth. |

1. In Teams: open a team → **···** → **Manage team** → **Apps** (or channel **+** → **Add an app**).
2. Search for the app name from §3, or **Apps** → **Built for your org**.
3. In a channel where the app is installed, **@mention** the bot once (e.g. `@Aurora hello`) to confirm Teams → Azure Bot → Aurora is wired. On **Teams → Manage**, **Check bot connection** should show recent activity (Aurora records the last delivered @mention/DM).

Users **@mention** the bot by its manifest **display name** (e.g. `@Aurora`). In channels, @mention is required; DMs are not.

## 6. Connect in Aurora

Recommended order: **§6 Connect** → **§5 install per team** → @mention verify → **Teams → Manage**.

1. **Connectors** → **Microsoft Teams** → **Connect** (org admin, Entra sign-in once per Aurora org).
2. **Teams → Manage**: refresh channels, **activate** channels, set **incident card** channel, edit **Teams memory**, use **Check bot connection** after an @mention.

OAuth powers Graph **reads** in Aurora. **Posts** (@mention replies, cards, routing) use the **bot** and still require §5 in each team.

## Troubleshooting

**I configured Azure Bot / enabled Teams channel but don’t see the app in Teams → Apps** — Azure is not where Teams apps are listed. Complete **§3** in [Teams Developer Portal](https://dev.teams.microsoft.com/) and **Distribute → Publish to your org** (or sideload). Enabling **Channels → Microsoft Teams** on the Azure Bot resource is necessary but not sufficient.

**Manifest parsing error / upload rejected** — See **§3 → Validate the app package**. Run [app package validation](https://dev.teams.microsoft.com/tools/store-validation) on your zip; typical fixes are `supportsChannelFeatures: tier1`, **Full name** in Basic, and removing or completing `webApplicationInfo`.

**Redirect URI mismatch** — Redirect in Entra must match exactly what Aurora sends (`{backend}/teams/callback`). With local dev, set `NGROK_URL` and use the tunnel URL in Entra.

**`invalid_request` / “not configured as a multi-tenant application” / `/common` endpoint** — The app registration is **single tenant** but `.env` has `TEAMS_TENANT_ID=common`. Set `TEAMS_TENANT_ID` to your **Tenant ID** GUID (Entra → Overview) and restart `aurora-server`, or change the app to multitenant in **Supported account types**.

**No “Configuration” or “Channels”** — You are probably on (a) the **Deployment** page (`Inputs` / `Outputs` / `Template` in the sidebar) → click **Go to resource**, or (b) the Entra **App registration** → search for the **Azure Bot** resource instead. Configuration is only on the bot resource under **Settings**.

**@mention gets no reply** — Confirm **Settings → Configuration → Messaging endpoint** is public HTTPS and ends with `/teams/messages`, the Teams **channel** is enabled, and the Teams app is installed. Check server logs for Bot Framework JWT verification (`TEAMS_APP_ID` / secret must match the bot registration).

**Bot posts fail (cards / routing)** — The Teams app must be **installed in the team/channel**. Proactive messages use Bot Framework; Graph delegated tokens are not used for outbound chat.

**"Unknown tenant" on @mention** — An org admin must **Connect Teams** in Aurora first so the tenant is linked to your org.

**Messages appear as a person, not the bot** — Outbound traffic should use `connectors/teams_connector/bot_client.py`. If you see a user name, report a regression — that path should not use delegated Graph send.

See also [Microsoft Teams on the connectors guide](../../../website/docs/integrations/connectors.md#microsoft-teams).
