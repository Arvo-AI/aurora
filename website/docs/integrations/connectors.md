---
sidebar_position: 1
---

# Aurora Connectors Setup Guide

This guide covers setting up integrations with 30+ cloud providers, observability tools, communication platforms, and CI/CD systems.

## Cloud Providers

### GCP

Google Cloud Platform integration for resource management, monitoring, and incident investigation.

#### Authentication

**Option A: Service Account Key (Recommended for Automation)**

1. Go to [Google Cloud Console](https://console.cloud.google.com/) > **Service Accounts**
2. Click **Create Service Account**
   - Name: `aurora`
   - Grant roles: `Viewer`, `Compute Admin`, `Kubernetes Engine Admin`, `Cloud SQL Client`, `BigQuery Data Viewer`, `BigQuery Job User`
3. Create a JSON key and download it
4. Users paste the JSON key via the Aurora UI

**Option B: OAuth 2.0 (Recommended for Users)**

1. Go to [Google Cloud Console](https://console.cloud.google.com/) > **APIs & Services** > **Credentials**
2. Click **Create Credentials** > **OAuth 2.0 Client ID**
   - Application type: Web application
   - Authorized redirect URIs: `http://localhost:5080/gcp/oauth/callback`
3. Copy **Client ID** and **Client Secret**

```bash
NEXT_PUBLIC_ENABLE_GCP_OAUTH=true
GCP_OAUTH_CLIENT_ID=your-client-id
GCP_OAUTH_CLIENT_SECRET=your-client-secret
```

#### Multi-Project Support

Aurora can query multiple GCP projects simultaneously. Each project is added separately via the UI with its own credentials.

#### BigQuery

When GCP is connected, the agent can query BigQuery datasets. The read-only identity gets `bigquery.dataViewer` and `bigquery.jobUser` roles.

---

### AWS

Amazon Web Services integration for EC2, EKS, RDS, Lambda, and more.

#### Authentication

**Option A: IAM Role with External ID (Recommended for Cross-Account)**

1. Create an IAM role in the target AWS account with a trust relationship to Aurora's account
2. Add an External ID for security
3. Grant permissions: `ReadOnlyAccess` or a custom policy with `ec2:Describe*`, `eks:Describe*`, `rds:Describe*`, etc.

```bash
AWS_ACCOUNT_ID=123456789012
AWS_EXTERNAL_ID=your-external-id
AWS_ROLE_ARN=arn:aws:iam::${AWS_ACCOUNT_ID}:role/AuroraReadOnly
```

**Option B: IAM User with Access Keys**

1. Create an IAM user in AWS
2. Attach `ReadOnlyAccess` policy
3. Generate access keys
4. Users enter Access Key ID and Secret Access Key via the Aurora UI

#### Multi-Account Support

Aurora can assume roles across multiple AWS accounts using STS AssumeRole. Each account is added separately with its own role ARN and External ID.

#### CloudWatch Integration

Aurora receives CloudWatch alarms via SNS webhooks. Set up an SNS topic and subscription to Aurora's webhook URL.

---

### Azure

Microsoft Azure integration for VMs, AKS, Cosmos DB, and more.

#### Authentication

**Option A: Service Principal (Recommended)**

1. Go to [Azure Portal](https://portal.azure.com/) > **Azure Active Directory** > **App registrations**
2. Click **New registration**
   - Name: `Aurora`
   - Supported account types: Single tenant
3. Go to **Certificates & secrets** > **New client secret**
4. Copy **Tenant ID**, **Application ID**, and **Client Secret**

```bash
AZURE_TENANT_ID=your-tenant-id
AZURE_CLIENT_ID=your-client-id
AZURE_CLIENT_SECRET=your-client-secret
```

Grant the service principal roles: `Reader`, `Kubernetes Service RBAC Viewer` (for AKS), etc.

**Option B: Managed Identity (for Azure VMs)**

If Aurora runs on an Azure VM, enable the system-assigned managed identity and grant it the necessary roles.

#### Multi-Subscription Support

Aurora can query multiple Azure subscriptions. Each subscription is added separately with its own credentials.

---

### OVH Cloud

OVH Cloud integration for compute, storage, and networking.

#### Authentication

OAuth 2.0 with multi-region support (EU, CA, US).

1. Go to [OVH Manager](https://www.ovh.com/manager/) > **API** > **Create an application**
2. Name: `Aurora`
3. Copy **Application Key** and **Application Secret**
4. Users enter credentials via the Aurora UI

#### Regions

OVH accounts are region-specific. Ensure your account has access to the regions you want to query.

---

### Scaleway

Scaleway integration for compute, storage, and networking.

#### Authentication

API Token authentication.

1. Go to [Scaleway Console](https://console.scaleway.com/) > **Credentials**
2. Create an API token
3. Users enter the token via the Aurora UI

---

### Cloudflare

Cloudflare integration for DNS, CDN, WAF, and edge diagnostics.

#### Authentication

API Token authentication.

1. Go to [Cloudflare Dashboard](https://dash.cloudflare.com/) > **My Profile** > **API Tokens**
2. Create a token with permissions: `Zone:Read`, `DNS:Read`, `Firewall Rules:Read`, `Cache Purge:Purge`
3. Users enter the token via the Aurora UI

#### Capabilities

- Query DNS records
- Check firewall rules and WAF status
- View analytics (traffic, threats, status codes)
- Purge cache (if write access is enabled)
- Manage security settings

---

## Communication Tools

### GitHub

GitHub integration for PR review, deployment tracking, and incident correlation.

#### Authentication

**Option A: GitHub App (Recommended)**

1. Go to [GitHub Settings](https://github.com/settings/apps) > **New GitHub App**
   - Name: `Aurora`
   - Homepage URL: `https://your-aurora-domain`
   - Webhook URL: `https://your-aurora-domain/github/webhook`
   - Permissions: `Contents:Read`, `Pull requests:Read`, `Deployments:Read`
   - Events: `pull_request`, `push`, `deployment`
2. Copy **App ID** and **Client ID**
3. Generate a **Client Secret**
4. Download the private key

```bash
GITHUB_APP_ID=your-app-id
GITHUB_CLIENT_ID=your-client-id
GITHUB_CLIENT_SECRET=your-client-secret
GITHUB_PRIVATE_KEY=your-private-key
```

**Option B: OAuth (Fallback)**

1. Go to [GitHub Settings](https://github.com/settings/developers) > **OAuth Apps** > **New OAuth App**
   - Authorization callback URL: `https://your-aurora-domain/github/oauth/callback`
2. Copy **Client ID** and **Client Secret**

#### Capabilities

- Review PRs and suggest fixes
- Correlate deployments with incidents
- Search code and issues
- Post findings back to GitHub

---

### Slack

Slack integration for incident notifications and chat-based investigations.

#### Authentication

OAuth 2.0 with optional Socket Mode for private deployments.

1. Go to [Slack API](https://api.slack.com/apps) > **Create New App** > **From scratch**
   - Name: `Aurora`
   - Workspace: your workspace
2. Go to **OAuth & Permissions**
   - Scopes: `chat:write`, `channels:read`, `users:read`, `groups:read`, `im:read`, `mpim:read`
   - Redirect URL: `https://your-aurora-domain/slack/oauth/callback`
3. Copy **Client ID**, **Client Secret**, and **Bot User OAuth Token**

```bash
SLACK_CLIENT_ID=your-client-id
SLACK_CLIENT_SECRET=your-client-secret
SLACK_BOT_TOKEN=xoxb-your-token
```

#### Socket Mode (Optional, for Private Deployments)

Enable Socket Mode for deployments without public webhooks:

1. Go to **Socket Mode** > **Enable Socket Mode**
2. Generate an app-level token
3. Set `SLACK_APP_TOKEN=xapp-your-token`

---

### Microsoft Teams

Microsoft Teams integration for incident notifications and investigations.

#### Authentication

Hybrid Entra OAuth + Bot Framework.

1. Go to [Azure Portal](https://portal.azure.com/) > **Azure Active Directory** > **App registrations**
2. Create a new app registration for Teams
3. Go to **Certificates & secrets** > generate a client secret
4. Go to **API permissions** > add `User.Read`
5. Copy **Application ID** and **Client Secret**

```bash
TEAMS_CLIENT_ID=your-client-id
TEAMS_CLIENT_SECRET=your-client-secret
TEAMS_TENANT_ID=your-tenant-id
```

#### Installing the App

1. Download the app manifest from Aurora
2. Go to Teams > **Apps** > **Manage your apps** > **Upload a custom app**
3. Upload the manifest

---

### Google Chat

Google Chat integration for incident notifications.

#### Authentication

User OAuth + Service Account for hybrid setup.

1. Go to [Google Cloud Console](https://console.cloud.google.com/) > **APIs & Services** > **Credentials**
2. Create an OAuth 2.0 Client ID (Web application)
3. Create a Service Account and download the JSON key

```bash
GOOGLE_CHAT_CLIENT_ID=your-client-id
GOOGLE_CHAT_CLIENT_SECRET=your-client-secret
GOOGLE_CHAT_SERVICE_ACCOUNT_JSON=your-service-account.json
```

---

## Documentation & Project Management

### Confluence

Atlassian Confluence integration for documentation and knowledge management.

#### Authentication

**Option A: OAuth (Cloud)**

1. Go to [Atlassian](https://developer.atlassian.com/console/myapps/) > **Create app**
   - Name: `Aurora`
   - Scopes: `read:confluence-content.all`, `read:confluence-user`
2. Copy **Client ID** and **Client Secret**

**Option B: Personal Access Token (Data Center)**

1. Go to **Profile** > **Personal Access Tokens** > **Create token**
2. Copy the token

```bash
CONFLUENCE_CLIENT_ID=your-client-id
CONFLUENCE_CLIENT_SECRET=your-client-secret
# OR
CONFLUENCE_PAT=your-personal-access-token
```

---

### Jira

Atlassian Jira integration for issue tracking and incident management.

#### Authentication

**Option A: OAuth (Cloud)**

1. Go to [Atlassian](https://developer.atlassian.com/console/myapps/) > **Create app**
   - Name: `Aurora`
   - Scopes: `read:jira-work`, `write:jira-work`
2. Copy **Client ID** and **Client Secret**

**Option B: Personal Access Token (Data Center)**

1. Go to **Profile** > **Personal Access Tokens** > **Create token**
2. Copy the token

```bash
JIRA_CLIENT_ID=your-client-id
JIRA_CLIENT_SECRET=your-client-secret
# OR
JIRA_PAT=your-personal-access-token
```

#### RCA Integration

Aurora can automatically create and update Jira issues when investigations complete. Configure which projects and issue types trigger automatic RCA.

---

### ServiceNow

ServiceNow integration for incident management and ITSM workflows.

#### Authentication

Basic auth (integration user) or OAuth 2.0 client credentials.

1. Create an integration user in ServiceNow with the `itil` role
2. Copy username and password (or OAuth credentials)

```bash
SERVICENOW_INSTANCE=your-instance.service-now.com
SERVICENOW_USERNAME=aurora-integration
SERVICENOW_PASSWORD=your-password
```

#### Webhook Configuration

Aurora provides a webhook URL. Create a Business Rule on the Incident table to POST new incidents to Aurora.

See the [ServiceNow integration guide](./servicenow.md) for the Business Rule script and RCA settings.

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
   - Scopes: `openid`, `users.read`, `incidents.read`, `incidents.write`, `services.read`
3. Copy **Client ID** and **Client Secret**

```bash
NEXT_PUBLIC_ENABLE_PAGERDUTY_OAUTH=true
PAGERDUTY_CLIENT_ID=your-client-id
PAGERDUTY_CLIENT_SECRET=your-client-secret
```

All five scopes must be registered on the app. PagerDuty silently drops any scope that is not registered, and a token issued without `users.read` fails validation when connecting.

#### Option B: API Token

Use a **user API token** from a PagerDuty user with write access (Responder or higher). A dedicated "Aurora" user is recommended so notes are attributed clearly.

1. In PagerDuty, go to **User Icon** > **My Profile** > **User Settings** > **API Access**
2. Click **Create API User Token**
3. Users enter the token via the Aurora UI

A read-only or account-level key still works for reading incidents and triggering RCA, but cannot post RCA notes back to PagerDuty.

#### Posting RCA notes back to PagerDuty

When enabled, Aurora adds one plain-text note to the originating PagerDuty incident when an investigation completes: the root cause, the impact (when the report has one), a link to the full investigation, and a disclaimer. Recurrences of an earlier incident and re-summaries after a follow-up chat do not post.

- **Where**: the toggle lives on the PagerDuty connector page under **RCA Notes**. It is off by default and org-wide.
- **Credential requirement**: the toggle can only be turned on when the stored credentials were proven able to write incidents (user token from a user with write access, or an OAuth token granted `incidents.write`). Account-level keys and read-only users are rejected with a reason.
- **Notes are permanent**: PagerDuty has no API to edit or delete notes. Aurora posts at most one note per incident.
- **Existing OAuth connections**: connections made before `incidents.write` was requested keep their read-only token. Disconnect and reconnect PagerDuty to grant it.
- **Rotating to a read-only token** turns the toggle off automatically.

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

:::tip Data Security
If you need to ensure PII is never sent to Aurora (for GDPR, SOC 2, or other compliance requirements), see the [Datadog PII Filtering guide](../configuration/data-access/datadog.md) after completing the setup below.
:::

:::info Multiple organizations
Several Datadog organizations can be connected at once, each needing its own API + application key pair. Aurora names each one from the org name Datadog reports and queries whichever matches the alert under investigation; a custom label is optional, to override that name with something your team recognises.

Where the name cannot be read — a key managing several organizations, or one without the `org_management` permission — Aurora falls back to the organization id, then to `default`. Set a label in that case so the organizations stay tellable apart.
:::

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

Users enter API keys and site via the Aurora UI, with an optional label. Site is recorded per organization, so organizations on different Datadog sites can coexist.

#### 4. Webhook Configuration

To receive Datadog alerts in Aurora:

1. In Datadog: **Integrations** > **Webhooks**
2. Click **New Webhook**
   - Name: `Aurora`
   - URL: `https://your-aurora-domain/datadog/webhook/{user_id}`
   - Custom Headers: `X-Datadog-Webhook-Secret: your-secret`
3. Test the webhook

---

### Grafana

API Token authentication.

#### 1. Create API Token

1. Go to [Grafana](https://grafana.com/) > **Configuration** > **API Keys**
2. Click **New API Key**
   - Name: `Aurora`
   - Role: `Viewer`
3. Copy the token

```bash
GRAFANA_API_TOKEN=your-token
GRAFANA_URL=https://your-grafana-instance.com
```

#### 2. Webhook Configuration

To receive Grafana alerts in Aurora:

1. In Grafana: **Alerting** > **Contact points** > **New contact point**
   - Type: `Webhook`
   - URL: `https://your-aurora-domain/grafana/webhook/{user_id}`
2. Test the contact point

---

### New Relic

API Key authentication.

#### 1. Create API Key

1. Go to [New Relic](https://one.newrelic.com/) > **API keys**
2. Click **Create key**
   - Type: `User`
   - Name: `Aurora`
3. Copy the key

```bash
NEW_RELIC_API_KEY=your-api-key
NEW_RELIC_ACCOUNT_ID=your-account-id
```

#### 2. Webhook Configuration

To receive New Relic alerts in Aurora:

1. In New Relic: **Alerts** > **Notification channels** > **New notification channel**
   - Type: `Webhook`
   - URL: `https://your-aurora-domain/newrelic/webhook/{user_id}`
2. Test the channel

---

### Splunk

API Token authentication.

#### 1. Create API Token

1. Go to [Splunk](https://your-splunk-instance.com/) > **Settings** > **Data inputs** > **HTTP Event Collector**
2. Click **New Token**
   - Name: `Aurora`
3. Copy the token and HEC URL

```bash
SPLUNK_HEC_TOKEN=your-token
SPLUNK_HEC_URL=https://your-splunk-instance.com:8088
SPLUNK_API_TOKEN=your-api-token
SPLUNK_URL=https://your-splunk-instance.com:8089
```

#### 2. Webhook Configuration

To send events to Splunk from Aurora:

1. Configure the HEC token in Aurora
2. Aurora will send investigation results and alerts to Splunk

---

### Elastic Cloud

Elasticsearch API key authentication.

#### 1. Create API Key

1. Go to [Elastic Cloud](https://cloud.elastic.co/) > **Elasticsearch** > **API keys**
2. Click **Create API key**
   - Name: `Aurora`
   - Privileges: `read` on indices
3. Copy the API key

```bash
ELASTIC_API_KEY=your-api-key
ELASTIC_CLOUD_ID=your-cloud-id
# OR for self-managed
ELASTIC_URL=https://your-elasticsearch-instance.com:9200
ELASTIC_USERNAME=elastic
ELASTIC_PASSWORD=your-password
```

---

### Sentry

API Token authentication.

#### 1. Create API Token

1. Go to [Sentry](https://sentry.io/) > **Settings** > **Auth Tokens**
2. Click **Create New Token**
   - Name: `Aurora`
   - Scopes: `project:read`, `event:read`
3. Copy the token

```bash
SENTRY_API_TOKEN=your-token
SENTRY_ORG_SLUG=your-org-slug
```

---

### Netdata

API Token authentication.

#### 1. Create API Token

1. Go to [Netdata Cloud](https://app.netdata.cloud/) > **Settings** > **API Tokens**
2. Click **Generate New Token**
   - Name: `Aurora`
3. Copy the token

```bash
NETDATA_API_TOKEN=your-token
NETDATA_SPACE_ID=your-space-id
```

---

## Messaging

### RabbitMQ

Read-only access to a broker through the [management HTTP API](https://www.rabbitmq.com/docs/management#http-api) (usually port 15672).

#### Credentials

Create a user with the `monitoring` tag and read permission on each vhost Aurora should see. The tag can read the management API and cannot publish, purge, or change policy. It does not grant vhost access by itself. Connect lists the default vhost and refuses the account when that call is unauthorized.

| Field | Example |
|---|---|
| Management URL | `https://rabbitmq.internal.example.com:15672` |
| Username | `aurora-monitor` |
| Password | stored in Vault |
| Default vhost | `/` |

The URL must be reachable from Aurora. Loopback and link-local addresses (including cloud metadata) are rejected. Private network addresses are allowed.

TLS verification follows `RABBITMQ_SSL_VERIFY` on the deployment: `false` (default), `true`, or a path to a CA bundle. The connect form does not choose this.

#### What the agent can read

Cluster overview, node memory and disk alarms, queues (depth, consumers, dead-letter arguments, rates), exchanges, bindings, connections, consumers, and the built-in health checks.

Message bodies are not fetched. The management `get` endpoint dequeues messages, so it is not exposed.

---

## CI/CD & Development Tools

### CloudBees

Aurora integrates with [CloudBees](https://www.cloudbees.com/) to provide CI/CD deployment visibility, automated incident correlation, and root cause analysis across your entire Jenkins infrastructure.

#### What You Get

| Capability | Description |
|------------|-------------|
| **Deployment event tracking** | Build completions from any controller are correlated with alerts automatically |
| **RCA with build context** | Auto-generated RCA includes pipeline stages, build logs, test results, and changeset |
| **Cross-controller visibility** | Query deployments across all managed controllers via Operations Center |
| **Incident prevention** | Pre-merge PR review blocks risky changes before they deploy |

#### Setup

**Single Controller:**

1. Go to your Jenkins controller > **Manage Jenkins** > **Configure System** > **CloudBees**
2. Enter controller URL and API token
3. Aurora discovers jobs and builds automatically

**Operations Center (Multi-Controller):**

1. Go to Operations Center > **Manage Jenkins** > **Configure System** > **CloudBees**
2. Enter OC URL and API token
3. Aurora queries all connected controllers

**Personal Access Token:**

1. Go to your Jenkins profile > **Configure** > **API Token**
2. Generate a new token
3. Users enter username and token via the Aurora UI

#### Webhook Configuration

To receive deployment events in Aurora:

1. In Jenkins: **Manage Jenkins** > **Configure System** > **CloudBees** > **Webhooks**
2. Webhook URL: `https://your-aurora-domain/cloudbees/webhook/{user_id}`
3. Events: `build.complete`, `build.start`

---

### Bitbucket

Bitbucket integration for PR review, deployment tracking, and incident prevention.

#### Authentication

API Token or OAuth 2.0.

**Option A: API Token**

1. Go to [Bitbucket](https://bitbucket.org/) > **Personal settings** > **App passwords**
2. Click **Create app password**
   - Name: `Aurora`
   - Permissions: `repo:read`, `pullrequest:read`
3. Copy the token

**Option B: OAuth**

1. Go to [Bitbucket](https://bitbucket.org/) > **Workspace settings** > **OAuth consumers**
2. Click **Create consumer**
   - Name: `Aurora`
   - Callback URL: `https://your-aurora-domain/bitbucket/oauth/callback`
   - Permissions: `repo:read`, `pullrequest:read`
3. Copy **Client ID** and **Client Secret**

```bash
BITBUCKET_CLIENT_ID=your-client-id
BITBUCKET_CLIENT_SECRET=your-client-secret
# OR
BITBUCKET_API_TOKEN=your-token
```

#### Incident Prevention

Aurora can review PRs before merge and block risky changes. Enable on the Bitbucket connector page.

---

### Jenkins

Jenkins integration for CI/CD visibility and deployment tracking.

#### Authentication

API Token authentication.

1. Go to your Jenkins instance > **Manage Jenkins** > **Manage Users** > your user > **Configure**
2. Click **Add new Token**
3. Copy the token

```bash
JENKINS_URL=https://your-jenkins-instance.com
JENKINS_USERNAME=your-username
JENKINS_API_TOKEN=your-token
```

#### Webhook Configuration

To receive build events in Aurora:

1. In Jenkins: **Manage Jenkins** > **Configure System** > **Webhooks**
2. Webhook URL: `https://your-aurora-domain/jenkins/webhook/{user_id}`
3. Events: `job.complete`, `job.start`

---

### Spinnaker

Spinnaker integration for multi-cloud deployment visibility.

See the [Spinnaker integration guide](./spinnaker.md) for detailed setup instructions.

---

## Kubernetes

### kubectl Agent

Deploy Aurora as a Kubernetes agent to query clusters directly.

#### Installation

1. Add the Aurora Helm repository:

```bash
helm repo add aurora https://charts.aurora-ai.net
helm repo update
```

2. Install the agent:

```bash
helm install aurora-agent aurora/aurora-agent \
  --namespace aurora \
  --create-namespace \
  --set apiUrl=https://your-aurora-domain \
  --set apiKey=your-api-key
```

3. Verify installation:

```bash
kubectl get pods -n aurora -l app=aurora-kubectl-agent
```

#### Configuration

| Parameter | Description | Default |
|-----------|-------------|---------|
| `apiUrl` | Aurora API URL | (required) |
| `apiKey` | Aurora API key | (required) |
| `namespace` | Kubernetes namespace | `aurora` |
| `replicas` | Number of agent replicas | `1` |

#### Capabilities

- Query pods, deployments, services, and nodes
- Fetch logs and events
- Check resource usage and health
- Trigger remediation actions (if enabled)

---

## Important Patterns

1. **Credentials stored in Vault** — encrypted at rest, never logged
2. **Webhooks for real-time alerts** — Aurora provides per-user webhook URLs with HMAC-SHA256 signing
3. **Read-only by default** — most connectors use minimal permissions; write access only when remediation is enabled
4. **Multi-account/multi-org support** — most cloud providers support multiple connections simultaneously
5. **PII filtering available** — documented separately for GCP and Datadog

## Setup Workflow

For each connector:

1. Create credentials/tokens in the external system
2. Configure environment variables or use Aurora UI
3. Validate connection (Aurora tests credentials)
4. (Optional) Set up webhooks for alert ingestion
5. Enable features like incident prevention or RCA

All troubleshooting sections include common errors and solutions.

## Troubleshooting

### Connection Failed

- **Credentials incorrect**: Verify username, password, API key, or token
- **Network unreachable**: Check firewall rules and security groups
- **SSL certificate error**: Verify TLS settings and CA bundle path
- **Rate limited**: Wait a few minutes and retry

### Webhook Not Receiving Events

- **Webhook URL incorrect**: Verify the URL matches the one in the external system
- **Firewall blocking**: Check that the external system can reach Aurora
- **Shared secret mismatch**: Verify the secret matches in both systems
- **Event type not subscribed**: Check that the correct event types are enabled

### Insufficient Permissions

- **Read-only token**: Use a token with write access if remediation is needed
- **Missing scopes**: Verify all required scopes are granted
- **Role not assigned**: Assign the correct role to the user or service account

## Need Help?

- Check the [Aurora documentation](https://docs.aurora-ai.net/)
- Open an issue on [GitHub](https://github.com/Arvo-AI/aurora/issues)
- Join the [Aurora Slack community](https://slack.aurora-ai.net/)
