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
NEXT_PUBLIC_ENABLE_SLACK=true
SLACK_CLIENT_ID=your-slack-client-id
SLACK_CLIENT_SECRET=your-slack-client-secret
SLACK_SIGNING_SECRET=your-signing-secret
```

#### Troubleshooting

| Error | Solution |
|-------|----------|
| "bad_redirect_uri" | Redirect URL must match exactly in Slack App settings |
| "Slack connector not enabled" | Set `NEXT_PUBLIC_ENABLE_SLACK=true` and restart |

---

## Documentation Tools

### Confluence

OAuth 2.0 authentication for Confluence Cloud, or Personal Access Token for Data Center.

#### Option A: Confluence Cloud (OAuth)

For Atlassian Cloud (`*.atlassian.net`):

##### 1. Create OAuth App

1. Go to [Atlassian Developer Console](https://developer.atlassian.com/console/myapps/)
2. Click **Create** > **OAuth 2.0 integration**
3. Name: `Aurora`
4. Click **Create**
5. Go to **Permissions** > **Confluence API** > **Add** > **Configure**
6. Add scopes:
   - `read:page:confluence`
   - `read:space:confluence`
   - `read:user:confluence`
7. Go to **Authorization** > **Add** callback URL:
   - `http://localhost:3000/confluence/callback` (development)
   - `https://your-domain.com/confluence/callback` (production)
8. Go to **Settings** and copy **Client ID** and **Secret**

##### 2. Configure Environment

```bash
NEXT_PUBLIC_ENABLE_CONFLUENCE=true
CONFLUENCE_CLIENT_ID=your-client-id
CONFLUENCE_CLIENT_SECRET=your-client-secret
```

##### 3. Connect via Aurora UI

1. Navigate to **Connectors** > **Confluence**
2. Click **Connect with Atlassian**
3. Authorize Aurora in the Atlassian popup
4. Connection complete - the site URL is detected automatically

#### Option B: Confluence Data Center (PAT)

For self-hosted Confluence instances:

##### 1. Create Personal Access Token

1. In Confluence, go to your profile > **Settings** > **Personal Access Tokens**
2. Click **Create token**
3. Name: `Aurora`
4. Set expiry as needed
5. Copy the token

##### 2. Connect via Aurora UI

1. Navigate to **Connectors** > **Confluence**
2. Select **Confluence Data Center (PAT)**
3. Enter:
   - **Base URL**: `https://confluence.yourcompany.com`
   - **Personal Access Token**: Your PAT
4. Click **Connect with PAT**

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

API Token authentication for Grafana Cloud or self-hosted.

#### 1. Create Service Account Token

**Grafana Cloud:**

1. Go to [Grafana Cloud](https://grafana.com/) > your stack
2. **Administration** > **Service accounts** > **Add service account**
   - Name: `Aurora`
   - Role: `Viewer`
3. Click **Add service account token**
4. Copy the token

**Self-hosted:**

1. Go to **Administration** > **Service accounts**
2. Create account with `Viewer` role
3. Generate token

Users enter the token and Grafana URL via the Aurora UI.

#### Webhook Configuration

1. In Grafana: **Alerting** > **Contact points** > **+ Add contact point**
2. Type: **Webhook**
3. URL: `https://your-aurora-domain/grafana/alerts/webhook/{user_id}`
4. In **Notification policies**, route alerts to the Aurora contact point

---

### Netdata

API Token authentication.

#### 1. Get API Token

1. Go to your Netdata Cloud dashboard
2. Navigate to **Space settings** > **API tokens**
3. Create a new token for Aurora

Users enter the token via the Aurora UI.

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

## Credential Storage

All connector credentials are stored securely in HashiCorp Vault:

- Credentials are encrypted at rest
- Database stores only Vault path references
- Credentials resolved at runtime
- Never logged or exposed in responses

See [Vault Configuration](/docs/configuration/vault) for details.
