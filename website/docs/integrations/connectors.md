---
sidebar_position: 1
---

# Connectors

Aurora connects to cloud providers and observability tools through connectors. This page provides detailed setup instructions for each integration.

:::info Cloud Connectors Are Optional
Aurora works without any cloud provider accounts. You only need an LLM API key to get started. Add cloud connectors when you're ready to query your infrastructure.
:::

## Cloud Providers

### GCP (Google Cloud Platform)

OAuth 2.0 authentication for Google Cloud Platform.

#### 1. Create OAuth Credentials

1. Go to [GCP Console > Credentials](https://console.cloud.google.com/apis/credentials)
2. If this is your first OAuth app, configure the **OAuth consent screen**:
   - User Type: **External** (or Internal for Workspace)
   - App name: `Aurora`
   - User support email: Your email
   - Developer contact: Your email
   - Add your email as a test user (required for External apps)
3. Create OAuth credentials:
   - Click **+ CREATE CREDENTIALS** > **OAuth client ID**
   - Application type: **Web application**
   - Name: `Aurora`
   - Authorized redirect URIs: `http://localhost:5080/callback`
4. Copy the **Client ID** and **Client Secret**

#### 2. Configure Environment

Add to your `.env`:

```bash
CLIENT_ID=123456789-xxxxxxxxxxxxxxxxxxxxxxxxxxxxx.apps.googleusercontent.com
CLIENT_SECRET=GOCSPX-xxxxxxxxxxxxxxxxxxxxxxxxx
```

#### 3. Enable Required APIs

In GCP Console, enable these APIs for your project:
- Cloud Resource Manager API
- Compute Engine API
- Cloud Logging API
- Cloud Monitoring API

#### Troubleshooting

| Error | Solution |
|-------|----------|
| "Redirect URI mismatch" | Ensure redirect URI in GCP Console exactly matches `http://localhost:5080/callback` |
| "Access blocked: App has not been verified" | Add your email as a test user in OAuth consent screen |
| "API not enabled" | Enable required APIs in GCP Console |

---

### AWS (Amazon Web Services)

IAM Role with External ID for secure cross-account access.

#### How It Works

Aurora uses AWS STS AssumeRole to access customer AWS accounts. This requires:
1. Aurora's AWS credentials (for making STS calls)
2. An IAM Role in the customer's account with a trust policy

#### 1. Configure Aurora's AWS Credentials

Aurora needs its own AWS credentials to make STS AssumeRole calls. Add to `.env`:

```bash
AWS_ACCESS_KEY_ID=AKIAXXXXXXXXXXXXXXXX
AWS_SECRET_ACCESS_KEY=your-secret-access-key
AWS_DEFAULT_REGION=us-east-1
```

#### 2. Create IAM Role in Customer Account

Users create this role in their own AWS account:

1. Go to [IAM > Roles](https://console.aws.amazon.com/iam/home#/roles) > **Create role**
2. Select trusted entity:
   - **AWS account**
   - **Another AWS account**
   - Enter Aurora's AWS Account ID (displayed in Aurora onboarding UI)
   - Check **Require external ID**
   - Enter the External ID (displayed in Aurora onboarding UI)
3. Attach permissions:
   - `ReadOnlyAccess` for read-only access
   - `PowerUserAccess` for full access (excluding IAM)
4. Name the role: `AuroraRole`
5. Copy the **Role ARN** after creation

#### Trust Policy Example

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::AURORA_ACCOUNT_ID:root"
      },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {
          "sts:ExternalId": "EXTERNAL_ID_FROM_AURORA"
        }
      }
    }
  ]
}
```

#### Troubleshooting

| Error | Solution |
|-------|----------|
| "Aurora cannot assume this role" | Verify trust policy has correct Aurora Account ID and External ID |
| "Unable to determine Aurora's AWS account ID" | Set `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` in `.env` |
| "Access denied" | Check the IAM role has sufficient permissions |

---

### Azure (Microsoft Azure)

Service Principal authentication for Microsoft Azure.

#### 1. Create App Registration

1. Go to [Azure Portal > App registrations](https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade)
2. Click **+ New registration**
   - Name: `Aurora`
   - Supported account types: Single tenant (or multi-tenant if needed)
   - Redirect URI: **Web** > `http://localhost:5080/azure/callback`
3. After creation, note down:
   - **Application (client) ID**
   - **Directory (tenant) ID**

#### 2. Create Client Secret

1. In the app registration, go to **Certificates & secrets**
2. Click **+ New client secret**
   - Description: `Aurora`
   - Expires: Choose appropriate duration
3. **Copy the secret Value immediately** (it won't be shown again)

#### 3. Grant API Permissions

1. Go to **API permissions** > **+ Add a permission**
2. Select **Azure Service Management**
3. Check **user_impersonation**
4. Click **Grant admin consent for [your tenant]**

#### 4. Assign Role to Subscription

1. Go to [Subscriptions](https://portal.azure.com/#view/Microsoft_Azure_Billing/SubscriptionsBlade)
2. Select your subscription
3. Go to **Access control (IAM)** > **+ Add role assignment**
4. Role: **Reader** (or **Contributor** for write access)
5. Members: Select your `Aurora` app
6. Review + assign

#### Troubleshooting

| Error | Solution |
|-------|----------|
| "No enabled subscription found" | Assign Reader/Contributor role to the app in subscription IAM |
| "AADSTS50011: Reply URL mismatch" | Verify redirect URI exactly matches in App Registration |
| "Insufficient privileges" | Grant admin consent for API permissions |

---

### OVH Cloud

OAuth 2.0 authentication for OVH Cloud with multi-region support.

:::warning HTTPS Required
OVH OAuth2 only accepts **HTTPS** callback URLs. For local development, use ngrok or cloudflared to create an HTTPS tunnel.
:::

#### 1. Set Up HTTPS Tunnel (Local Development)

```bash
# Using ngrok
ngrok http 5080

# Note the HTTPS URL, e.g., https://abc123.ngrok-free.app
```

#### 2. Create OAuth App

1. Go to the API console for your region:
   - EU: https://eu.api.ovh.com/console/
   - CA: https://ca.api.ovh.com/console/
   - US: https://us.api.ovh.com/console/
2. Authenticate with your OVH account
3. Navigate to `/me` > `/me/api/oauth2/client`
4. Use **POST** to create a new client:

```json
{
  "callbackUrls": [
    "https://abc123.ngrok-free.app/ovh/oauth2/callback"
  ],
  "description": "Aurora Cloud Platform",
  "flow": "AUTHORIZATION_CODE",
  "name": "Aurora"
}
```

5. Copy the **Client ID** and **Client Secret** from the response

#### 3. Configure Environment

```bash
NEXT_PUBLIC_ENABLE_OVH=true

# EU Region
OVH_EU_CLIENT_ID=your-eu-client-id
OVH_EU_CLIENT_SECRET=your-eu-client-secret
OVH_EU_REDIRECT_URI=https://abc123.ngrok-free.app/ovh_api/ovh/oauth2/callback

# CA Region (optional)
OVH_CA_CLIENT_ID=your-ca-client-id
OVH_CA_CLIENT_SECRET=your-ca-client-secret
OVH_CA_REDIRECT_URI=https://abc123.ngrok-free.app/ovh_api/ovh/oauth2/callback

# US Region (optional)
OVH_US_CLIENT_ID=your-us-client-id
OVH_US_CLIENT_SECRET=your-us-client-secret
OVH_US_REDIRECT_URI=https://abc123.ngrok-free.app/ovh_api/ovh/oauth2/callback
```

#### Troubleshooting

| Error | Solution |
|-------|----------|
| "OAuth2 credentials not configured for [region]" | Set the corresponding `OVH_[REGION]_CLIENT_ID` and `OVH_[REGION]_CLIENT_SECRET` |
| "OVH connector not enabled" | Set `NEXT_PUBLIC_ENABLE_OVH=true` and restart Aurora |
| "Invalid redirect_uri" | OVH requires HTTPS. Use ngrok or cloudflared |

---

## Communication Tools

### GitHub

OAuth App authentication for GitHub repositories and issues.

#### 1. Create OAuth App

1. Go to [GitHub > Settings > Developer settings > OAuth Apps](https://github.com/settings/developers)
2. Click **New OAuth App**
   - Application name: `Aurora`
   - Homepage URL: `http://localhost:3000`
   - Authorization callback URL: `http://localhost:5080/github/callback`
3. Click **Register application**
4. Copy the **Client ID**
5. Click **Generate a new client secret** and copy it

#### 2. Configure Environment

```bash
GH_OAUTH_CLIENT_ID=your-github-client-id
GH_OAUTH_CLIENT_SECRET=your-github-client-secret
NEXT_PUBLIC_GITHUB_CLIENT_ID=your-github-client-id
```

#### Troubleshooting

| Error | Solution |
|-------|----------|
| "No authorization code provided" | Verify callback URL matches exactly: `http://localhost:5080/github/callback` |
| "Bad credentials" | Regenerate client secret and update `.env` |

---

### Slack

OAuth 2.0 authentication for Slack workspaces.

#### 1. Create Slack App

1. Go to [Slack API Apps](https://api.slack.com/apps) > **Create New App** > **From scratch**
   - App Name: `Aurora`
   - Select your workspace
2. Go to **OAuth & Permissions**
3. Add Redirect URLs:
   - Local: `http://localhost:5080/slack/callback`
   - With tunnel: `https://your-ngrok-url.ngrok-free.app/slack/callback`

#### 2. Add Bot Token Scopes

In **OAuth & Permissions** > **Scopes** > **Bot Token Scopes**, add:

| Scope | Purpose |
|-------|---------|
| `chat:write` | Send messages |
| `channels:read` | List channels |
| `channels:history` | Read channel messages |
| `channels:join` | Join channels |
| `app_mentions:read` | Receive @mentions |
| `users:read` | Get user info |

#### 3. Get Credentials

In **Basic Information**, copy:
- **Client ID**
- **Client Secret**
- **Signing Secret**

#### 4. Configure Environment

```bash
SLACK_CLIENT_ID=your-slack-client-id
SLACK_CLIENT_SECRET=your-slack-client-secret
SLACK_SIGNING_SECRET=your-signing-secret
```

#### Troubleshooting

| Error | Solution |
|-------|----------|
| "bad_redirect_uri" | Redirect URL must match exactly in Slack App settings |
| "Slack OAuth credentials not configured" | Set `SLACK_CLIENT_ID` and `SLACK_CLIENT_SECRET` in `.env` |

---

### Google Chat

Hybrid authentication for Google Chat spaces. User OAuth is used during setup
to create the incidents space in the customer's Google Workspace. A service
account handles all ongoing messaging so notifications and @Aurora replies
appear as the Chat app ("Aurora"), not as a human user.

#### 1. Create a Google Cloud Project

Go to [Google Cloud Console](https://console.cloud.google.com/projectcreate) and create a new project (or select an existing one).

#### 2. Enable the Google Chat API

In your project, go to **APIs & Services → Library**, search for "Google Chat API", and click **Enable**.

[Enable Google Chat API →](https://console.cloud.google.com/apis/library/chat.googleapis.com)

#### 3. Create OAuth Credentials

1. Go to **APIs & Services → Credentials → Create Credentials → OAuth client ID**
2. Select **Web application** as the type
3. Add an **Authorized redirect URI**: `https://your-domain.com/google-chat/callback` (or `http://localhost:5080/google-chat/callback` for local dev)
4. Copy the **Client ID** and **Client Secret**

[Create OAuth Client →](https://console.cloud.google.com/apis/credentials/oauthclient)

#### 4. Create a Service Account

1. Go to **IAM & Admin → Service Accounts → Create Service Account**
2. Name it something like `aurora-chat-bot`
3. Click **Create and Continue** (no extra roles needed)
4. On the service account page, go to **Keys → Add Key → Create new key → JSON**
5. The downloaded JSON content is your `GOOGLE_CHAT_SERVICE_ACCOUNT_KEY`

[Create Service Account →](https://console.cloud.google.com/iam-admin/serviceaccounts/create)

#### 5. Configure the Chat App

Go to the [Google Chat API Configuration page](https://console.cloud.google.com/apis/api/chat.googleapis.com/hangouts-chat) and set the following. Leave everything else as default.

> **Important:** Uncheck **Build this Chat app as a Workspace add-on** at the top of the page first.

**Application info:**
- **App name:** `Aurora`
- **Avatar URL:** `https://raw.githubusercontent.com/arvo-ai/aurora/main/client/public/arvologo.png`
- **Description:** `AI incident response assistant`

**Interactive features:**
- Enable **Interactive features**
- Under Functionality, check **Join spaces and group conversations**

**Connection settings:**
- Select the **HTTP endpoint URL** radio button
- Paste your publicly accessible HTTPS endpoint in the field:
  - Local (with tunnel): `https://your-ngrok-url.ngrok-free.app/google-chat/events`
  - Production: `https://your-domain.com/google-chat/events`
- Set **Authentication Audience** to **Project Number**

**Visibility:**
- Check **Make this Chat app available to specific people and groups** and add your email address (or a Google Group to let multiple people find and add the bot)
- This controls who can *find and add* the bot — once added to a space, all members of that space can interact with it. You don't need to add every user here.

#### 6. Configure Environment

```bash
GOOGLE_CHAT_CLIENT_ID=your-client-id
GOOGLE_CHAT_CLIENT_SECRET=your-client-secret
GOOGLE_CHAT_SERVICE_ACCOUNT_KEY='{"type":"service_account",...}'
```

Then rebuild and restart Aurora:

```bash
make down
make dev
```

#### Troubleshooting

| Error | Solution |
|-------|----------|
| `invalid_scope` | Ensure the service account has the `chat.bot` scope |
| "Google Chat OAuth credentials not configured" | Set `GOOGLE_CHAT_CLIENT_ID` and `GOOGLE_CHAT_CLIENT_SECRET` in `.env` |
| "bad_redirect_uri" | Redirect URI must match exactly in Google Cloud Console OAuth settings |
| Event verification failing | Ensure the Chat app's Authentication Audience is set to **HTTP endpoint URL** and the URL matches `NEXT_PUBLIC_BACKEND_URL` |
| Messages appear as your name | Set `GOOGLE_CHAT_SERVICE_ACCOUNT_KEY` to enable the Chat app identity |

---

## Documentation & Project Management

### Atlassian (Confluence + Jira)

OAuth 2.0 authentication for Atlassian Cloud (Confluence and/or Jira), or Personal Access Tokens for Data Center.

One OAuth app covers both products. You choose which to connect in the Aurora UI.

#### Option A: Atlassian Cloud (OAuth)

For Atlassian Cloud (`*.atlassian.net`):

##### 1. Create OAuth App

1. Go to [Atlassian Developer Console](https://developer.atlassian.com/console/myapps/)
2. Click **Create** > **OAuth 2.0 integration**
3. Name: `Aurora`
4. Click **Create**
5. Go to **Distribution**, set Distribution Status to **Sharing**, fill in the required vendor fields (name, privacy policy URL), set Personal Data Declaration to **Yes**, and save. Without this, non-owner users will see "You don't have access to this app."
6. Go to **Permissions** and add scopes for the products you want:
   - **Confluence API** > **Add** > **Configure** > click **Edit Scopes** then **Add granular scopes**:
     - `read:page:confluence`
     - `read:space:confluence`
     - `read:user:confluence`
     - `search:confluence`

     :::warning Use Granular Scopes
     You must add these as **granular scopes**, not classic scopes. Click "Add granular scopes" under Confluence API in the Permissions tab. If only classic scopes are added, the OAuth flow will fail with "scopes not added to the app."
     :::

   - **Jira platform REST API** > **Add** > **Configure**:
     - `read:jira-work`
     - `write:jira-work`
     - `read:jira-user`
7. Go to **Authorization** > **Add** callback URL:
   - `http://localhost:3000/atlassian/callback` (development)
   - `https://your-domain.com/atlassian/callback` (production)
8. Go to **Settings** and copy **Client ID** and **Secret**

##### 2. Configure Environment

```bash
NEXT_PUBLIC_ENABLE_CONFLUENCE=true
NEXT_PUBLIC_ENABLE_JIRA=true
ATLASSIAN_CLIENT_ID=your-client-id
ATLASSIAN_CLIENT_SECRET=your-client-secret
```

##### 3. Connect via Aurora UI

1. Navigate to **Connectors** > **Atlassian**
2. Select which products to connect (Confluence, Jira, or both)
3. Click **Connect with Atlassian**
4. Authorize Aurora in the Atlassian popup
5. Connection complete - the site URL is detected automatically
6. For Jira, choose the agent permission tier (Read Only or Full Access)

#### Option B: Data Center (PAT)

For self-hosted Confluence or Jira instances:

##### 1. Create Personal Access Token

**Confluence:**
1. In Confluence, go to your profile > **Settings** > **Personal Access Tokens**
2. Click **Create token**, name: `Aurora`, set expiry as needed
3. Copy the token

**Jira:**
1. In Jira, go to your profile > **Personal Access Tokens**
2. Click **Create token**, name: `Aurora`, set expiry as needed
3. Copy the token

##### 2. Connect via Aurora UI

1. Navigate to **Connectors** > **Atlassian**
2. Select the products you want and enter per-product:
   - **Base URL**: e.g. `https://confluence.yourcompany.com` or `https://jira.yourcompany.com`
   - **Personal Access Token**: The respective PAT
3. Click **Connect with PAT**

#### URL Limitations

:::warning Short Links Not Supported on Cloud
Confluence Cloud short links (e.g., `https://company.atlassian.net/wiki/x/ABC123`) cannot be resolved via API. Use full page URLs instead:
- `https://company.atlassian.net/wiki/spaces/SPACE/pages/123456/Page+Title`
- `https://company.atlassian.net/wiki/pages/viewpage.action?pageId=123456`

Data Center short links work correctly.
:::

#### Troubleshooting

| Error | Solution |
|-------|----------|
| "Unable to parse Confluence page ID from URL" | Use full page URL instead of short link (Cloud only) |
| "Confluence page URL does not match configured base URL" | Verify the page is from your connected Confluence instance |
| "Confluence credentials expired" | Reconnect via the Connectors page |
| "Failed to validate Confluence PAT" | Verify PAT is valid and not expired |
| "Jira credentials expired" | Reconnect via the Connectors page |
| "Failed to validate Jira PAT" | Verify PAT is valid and not expired |
| "Insufficient Jira scopes" | Ensure OAuth app has `read:jira-work`, `write:jira-work`, and `read:jira-user` scopes |
| "Atlassian OAuth configuration missing" | Set `ATLASSIAN_CLIENT_ID` and `ATLASSIAN_CLIENT_SECRET` in `.env` |
| "You don't have access to this app" | Enable **Sharing** in the Distribution tab of your Atlassian OAuth app |
| "Scopes not added to the app" | Add **granular** Confluence scopes (not classic) in the Permissions tab |

---

## Observability Tools

### PagerDuty

OAuth 2.0 or API Token authentication.

#### Option A: OAuth (Recommended)

1. Go to [PagerDuty](https://app.pagerduty.com/) > **Integrations** > **Developer Mode** > **My Apps**
2. Click **Create New App**
   - Name: `Aurora`
   - Category: Operations
   - Enable **OAuth 2.0**
   - Redirect URL: `http://localhost:5080/pagerduty/oauth/callback`
3. Copy **Client ID** and **Client Secret**

```bash
NEXT_PUBLIC_ENABLE_PAGERDUTY_OAUTH=true
PAGERDUTY_CLIENT_ID=your-client-id
PAGERDUTY_CLIENT_SECRET=your-client-secret
```

#### Option B: API Token

1. Go to [PagerDuty](https://app.pagerduty.com/) > **Integrations** > **API Access Keys**
2. Click **Create New API Key**
3. Users enter the token via the Aurora UI

#### Webhook Configuration

To receive PagerDuty alerts in Aurora:

1. In PagerDuty: **Integrations** > **Generic Webhooks (v3)** > **New Webhook**
2. Webhook URL: `https://your-aurora-domain/pagerduty/webhook/{user_id}`
3. Subscribe to events:
   - `incident.triggered`
   - `incident.acknowledged`
   - `incident.resolved`

---

### Datadog

API Key + Application Key authentication.

#### 1. Create API Key

1. Go to [Datadog](https://app.datadoghq.com/) > avatar > **Organization Settings** > **API Keys**
2. Click **+ New Key**
3. Name: `Aurora`
4. Copy the key

#### 2. Create Application Key

1. Go to **Organization Settings** > **Application Keys**
2. Click **+ New Key**
3. Name: `Aurora`
4. Copy the key

#### 3. Identify Your Site

| Site | API URL |
|------|---------|
| US1 | `datadoghq.com` |
| US3 | `us3.datadoghq.com` |
| US5 | `us5.datadoghq.com` |
| EU | `datadoghq.eu` |

Users enter API keys and site via the Aurora UI.

#### Webhook Configuration

1. In Datadog: **Integrations** > **Webhooks** > **+ New**
2. Name: `aurora`
3. URL: `https://your-aurora-domain/datadog/webhook/{user_id}`
4. In monitors, add `@webhook-aurora` to notifications

---

### Grafana

Webhook-based connection for Grafana Cloud or self-hosted instances. No API key required.

#### Setup

1. Open the **Grafana** integration page in Aurora
2. Copy the webhook URL shown on screen
3. In Grafana: **Alerts & IRM** > **Alerting** > **Notification Configuration** > **Contact points** > **New contact point**
   - Type: **Webhook**
   - URL: paste the Aurora webhook URL (`https://your-aurora-domain/grafana/alerts/webhook/{user_id}`)
4. Click **Test** to send a test notification
5. Aurora auto-connects when it receives the test webhook
6. Save the contact point, then add it to a notification policy under **Alerting** > **Notification Configuration** > **Notification policies**

#### Disconnect / Reconnect

Disconnecting in Aurora deactivates the connection — incoming webhooks are rejected until the user clicks **Reconnect**. The Grafana contact point does not need to be reconfigured.

#### How Aurora Processes Grafana Webhooks

Grafana sends grouped webhook payloads containing an `alerts[]` array. Each alert has a
**fingerprint** (hash of rule + labels) that uniquely identifies an alert instance.

Aurora processes each alert in the array individually:

- **Firing** (`status: "firing"`): Processed independently per fingerprint. AlertCorrelator
  checks whether the alert matches an existing open incident (by fingerprint, service
  similarity, and time proximity, selecting the newest by `started_at DESC`). If a match
  is found the alert is attached to that incident; otherwise a new incident is created
  and RCA is triggered.
- **Resolved** (`status: "resolved"`): Matches the original incident by fingerprint and
  attaches the resolution as a correlated alert. No new incident or RCA is created.

**Key behaviors:**

| Scenario | Behavior |
|----------|----------|
| Single alert fires then resolves | Matched by fingerprint, resolution grouped with original incident |
| Multiple alerts in one webhook | Each fingerprint is processed independently; correlated alerts attach to an existing incident, uncorrelated ones create a new incident and RCA |
| Partial resolution (some firing, some resolved) | Each alert handled independently by its status |
| Same alert re-fires weeks later | New incident created; resolution matches newest by `started_at DESC` |
| No matching incident for resolution | Logged and skipped; alert still persisted in `grafana_alerts` |
| Labels change mid-incident | Fingerprint changes, so resolution won't match (labels shouldn't change mid-incident) |

---

### New Relic

User API Key authentication for querying New Relic via NerdGraph (GraphQL).

#### 1. Create a User API Key

1. Log in to [one.newrelic.com](https://one.newrelic.com) and go to **Administration > API keys** (or visit [one.newrelic.com/admin-portal/api-keys](https://one.newrelic.com/admin-portal/api-keys/))
2. Click **Create a key** and select **User** as the key type
3. Name the key (e.g., `Aurora Integration`) and save it
4. Copy the key — it starts with `NRAK-`

#### 2. Find Your Account ID

Your Account ID is shown in the account dropdown or on the API keys page. It is a numeric value (e.g., `1234567`).

#### 3. Identify Your Region

| Region | NerdGraph Endpoint |
|--------|-------------------|
| US | `https://api.newrelic.com/graphql` |
| EU | `https://api.eu.newrelic.com/graphql` |

#### 4. (Optional) License Key

If you want Aurora to write annotations back to New Relic in the future, you can also provide a 40-character License (ingest) key. This is optional and not required for read-only RCA.

#### 5. Connect via Aurora UI

1. Navigate to **Connectors** > **New Relic**
2. Enter your **User API Key**, **Account ID**, and **Region** (US/EU)
3. Optionally provide a **License Key** for write-back capabilities
4. Click **Connect**

#### What Aurora Queries

Aurora uses NerdGraph to:
- Execute arbitrary **NRQL queries** against any telemetry type (metrics, logs, traces, events)
- Fetch **alert issues and incidents** with filtering by state, priority, and time window
- Search **entities** (services, hosts, applications)
- List **accessible accounts** for multi-account setups

All queries go through a single endpoint: `POST https://api.newrelic.com/graphql` with the `API-Key` header.

#### Webhook Configuration

To receive New Relic alerts in Aurora:

1. In New Relic: **Alerts > Destinations** > create a new **Webhook** destination
2. Webhook URL: `https://your-aurora-domain/newrelic/webhook/{user_id}`
3. Under **Workflows**, create or edit a workflow
4. Add a notification channel using the webhook destination
5. Configure the workflow filter for the issues you want Aurora to investigate

#### Polling (Alternative to Webhooks)

Aurora can also poll NerdGraph for active issues. Trigger manually via `POST /newrelic/poll-issues` or schedule via Celery Beat.

#### Troubleshooting

| Error | Solution |
|-------|----------|
| "Invalid API key" | Ensure the key starts with `NRAK-` and belongs to a user with read access to APM, Infrastructure, Logs, and Alerts |
| "Account not found" | Verify the Account ID is correct and the API key has access to that account |
| "EU region issues" | Make sure you selected "EU" in the region selector if your account is on the EU data center |

---

### Netdata

API Token authentication.

#### 1. Get API Token

1. Go to your Netdata Cloud dashboard
2. Navigate to **Space settings** > **API tokens**
3. Create a new token for Aurora

Users enter the token via the Aurora UI.

---

### Splunk

API Token authentication for Splunk Cloud or Enterprise.

#### 1. Create a Minimal Role (Recommended)

Aurora only needs the `search` capability. You can use the built-in **power** role, or create a minimal custom role:

1. In Splunk: **Settings** > **Roles** > **New Role**
   - Name: `aurora_readonly`
   - Capabilities: check **search** only
   - Under **Indexes**, set **Indexes searched by default** to `All non-internal indexes` (or specific indexes you want Aurora to access)
2. Create a user with this role, or assign it to an existing user

#### 2. Create an API Token

1. Go to **Settings** > **Tokens** > **New Token**
2. Select the user with the role above
3. Set an expiration and create the token
4. Copy the token

#### 3. Connect via Aurora UI

1. Navigate to **Connectors** > **Splunk**
2. Enter your Splunk instance URL (e.g., `https://your-splunk:8089`)
3. Paste the API token
4. Click **Connect**

#### What Aurora Queries

Aurora uses the Splunk REST API to:
- **Search logs** via `/services/search/jobs/export` (SPL queries)
- **List indexes** via `/services/data/indexes`
- **List sourcetypes** for targeted searches

All calls use Bearer token auth over HTTPS on port 8089.

#### Troubleshooting

| Error | Solution |
|-------|----------|
| "Authentication failed" | Token may be expired or the user lacks the `search` capability |
| "Connection refused" | Verify the URL includes port 8089 and is reachable from Aurora |
| "No results" | Check that the role has the correct indexes in "Indexes searched by default" |

---

## Kubernetes

Aurora can connect to Kubernetes clusters via the kubectl agent.

### Installing the kubectl Agent

The kubectl agent runs in your cluster and connects outbound to Aurora via WebSocket.

#### Prerequisites

- Kubernetes 1.19+
- Helm 3.x
- Cluster-admin access
- Aurora instance running

#### 1. Get Agent Token

1. Log into Aurora UI
2. Navigate to **Connectors** > **Kubernetes**
3. Click **Add Cluster**
4. Copy the generated agent token

#### 2. Build Agent Image

```bash
cd kubectl-agent/src/
docker build -t your-registry/aurora-kubectl-agent:1.0.3 .
docker push your-registry/aurora-kubectl-agent:1.0.3
```

#### 3. Create values.yaml

```yaml
aurora:
  backendUrl: "https://your-aurora-instance.com"
  wsEndpoint: "wss://your-aurora-instance.com/kubectl-agent"
  agentToken: "your-generated-token-here"

agent:
  image:
    repository: your-registry/aurora-kubectl-agent
    tag: "1.0.3"
```

#### 4. Install via Helm

```bash
helm install aurora-kubectl-agent ./kubectl-agent/chart \
  --namespace aurora --create-namespace \
  -f values.yaml
```

#### 5. Verify Installation

```bash
# Check pod status
kubectl get pods -n aurora -l app=aurora-kubectl-agent

# Check logs
kubectl logs -n aurora -l app=aurora-kubectl-agent --tail=50
```

The cluster should appear in Aurora UI with "Connected" status.

See [kubectl-agent README](https://github.com/arvo-ai/aurora/blob/main/kubectl-agent/README.md) for advanced configuration.

---

## Development Tools

### Bitbucket

OAuth App authentication for Bitbucket Cloud.

#### 1. Create OAuth Consumer

1. Go to **Bitbucket workspace settings** > **OAuth consumers** > **Add consumer**
   - Name: `Aurora`
   - Callback URL: `{NEXT_PUBLIC_BACKEND_URL}/bitbucket/callback` (e.g. `https://your-aurora-domain/bitbucket/callback`)
   - Permissions: **Repositories** (Read), **Pull requests** (Read)
2. Copy the **Key** and **Secret**

#### 2. Configure Environment

```bash
BB_OAUTH_CLIENT_ID=your-bitbucket-key
BB_OAUTH_CLIENT_SECRET=your-bitbucket-secret
```

---

## Credential Storage

All connector credentials are stored securely in HashiCorp Vault:

- Credentials are encrypted at rest
- Database stores only Vault path references
- Credentials resolved at runtime
- Never logged or exposed in responses

See [Vault Configuration](/docs/configuration/vault) for details.
