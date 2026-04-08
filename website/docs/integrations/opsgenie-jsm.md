---
sidebar_position: 4
---

# OpsGenie / JSM Operations

Aurora integrates with [OpsGenie](https://www.atlassian.com/software/opsgenie) and [Jira Service Management (JSM) Operations](https://www.atlassian.com/software/jira/service-management/features/operations) to ingest alerts, query on-call schedules, and automatically generate root cause analysis when incidents occur.

## What You Get

| Capability | Description |
|------------|-------------|
| **Real-time alert ingestion** | Alerts from OpsGenie or JSM Operations are pushed to Aurora via webhook |
| **Automatic RCA** | When a new alert fires, Aurora creates an incident and starts a background root cause analysis |
| **Alert querying** | Ask the chatbot about open alerts, alert details, logs, and notes |
| **On-call visibility** | Query who is currently on-call, view schedules and rotations |
| **Team and service discovery** | List all Operations teams and registered services |
| **Incident querying (JSM)** | Query JSM service desk incidents (Jira issues of type `[System] Incident`) |
| **Alert correlation** | Incoming alerts are correlated with existing incidents using title, service, and time proximity |

:::tip Which should I choose?
**OpsGenie** if you have a standalone OpsGenie account with a GenieKey. OpsGenie is end-of-sale (June 2025) and end-of-support (April 2027).

**JSM Operations** if you use Jira Service Management. This is Atlassian's replacement for OpsGenie — same alerting capabilities, built into JSM.
:::

## Prerequisites

- An **OpsGenie account** with an API Integration key, OR
- A **Jira Service Management** site with Operations enabled and an **Atlassian API token**
- Aurora with Vault configured and unsealed

---

## Connecting OpsGenie

### 1. Get your OpsGenie API Key

1. Log in to [OpsGenie](https://app.opsgenie.com)
2. Go to **Settings** > **Integrations**
3. Search for **API** and select **API Integration**
4. Click **Add** to create a new integration
5. Copy the **API Key** (GenieKey)

:::info Required Permissions
The API key needs read access to alerts, incidents, services, schedules, and teams. The default API Integration includes all read permissions.
:::

### 2. Connect in Aurora

1. Navigate to **Connectors** > **OpsGenie / JSM**
2. Make sure the **OpsGenie** toggle is selected (default)
3. Paste your **API Key**
4. Select your **Region** (US or EU)
5. Click **Connect OpsGenie**

Aurora validates the key by calling the OpsGenie Account API. On success, credentials are encrypted and stored in Vault.

---

## Connecting JSM Operations

### 1. Create an Atlassian API Token

1. Go to [id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens)
2. Click **Create API token**
3. Give it a label (e.g., "Aurora")
4. Copy the token

:::info No OAuth Required
JSM Operations uses Basic auth (email + API token). You do **not** need to configure an OAuth app or add scopes in the Atlassian Developer Console. The API token inherits your Atlassian account's permissions.
:::

### 2. Connect in Aurora

1. Navigate to **Connectors** > **OpsGenie / JSM**
2. Click the **JSM Operations** toggle
3. Enter your:
   - **Atlassian Site URL**: e.g., `https://yourteam.atlassian.net`
   - **Email**: Your Atlassian account email
   - **API Token**: The token you created above
4. Click **Connect JSM Operations**

Aurora resolves your site's cloud ID automatically and validates the connection by querying the JSM Operations alerts API.

:::caution Account Access
Your Atlassian account must have access to JSM Operations on the target site. If you get a validation error, verify that:
- You can access `https://yoursite.atlassian.net/jira/ops/alerts` in your browser
- Your account has the correct JSM agent license
:::

---

## Webhook Configuration

After connecting, Aurora displays a **webhook URL** on the setup page. Configure your OpsGenie or JSM instance to send alert events to this URL for real-time alert ingestion and automatic RCA.

### OpsGenie Webhook Setup

1. In OpsGenie, go to **Settings** > **Integrations**
2. Search for **Webhook** and select it
3. Click **Add** to create a new webhook
4. Paste the Aurora webhook URL
5. Select alert actions: **Create**, **Acknowledge**, **Close**, and any others you want to track
6. **Save** the integration

### JSM Operations Webhook Setup

:::info JSM Premium Required
Outgoing webhooks require **JSM Premium** or **Enterprise**. On JSM Standard, you can use the API connection to query alerts on-demand from the chatbot, but real-time webhook ingestion is not available.
:::

1. In Jira, click the **Settings gear** (top right)
2. Under **Jira admin settings**, click **Operations**
3. In the left sidebar, click **Integrations**
4. Click **Add integration**, search for **Webhook**, and add it
5. Name it (e.g., "Aurora") and assign a team
6. Click **Edit settings** on the integration
7. Select **Authenticate with a Webhook account** (this reveals the URL field)
8. Paste the Aurora webhook URL
9. Check **Add alert description to payload** and **Add alert details to payload**
10. Click **Save**
11. Under **Alert actions**, select which events to forward:
    - **Alert is created** (required — this triggers RCA)
    - **Alert is acknowledged** (recommended)
    - **Alert is closed** (recommended)
    - Other actions are optional
12. Click **Turn on integration** (top right)

:::tip Recommended Alert Actions
Only **Create** triggers a new incident and RCA in Aurora. Other actions (Acknowledge, Close, Snooze, etc.) are stored as events but do not create new incidents. Selecting too many actions generates noise without additional value for RCA.
:::

### What Happens When Alerts Arrive

1. JSM/OpsGenie sends an alert event to the Aurora webhook
2. Aurora stores the raw event in the `opsgenie_events` table
3. For **Create** actions, Aurora:
   - Creates an incident record
   - Runs alert correlation (matches against existing incidents by title, service, and time proximity)
   - Triggers a background RCA investigation
   - Generates an incident summary
4. For other actions (Acknowledge, Close, etc.), the event is stored but no new incident is created

---

## Using the Chatbot

Once connected, ask Aurora about your alerts and operations data:

### Alert Queries
- *"What alerts do I have?"*
- *"Show me P1 alerts from the last 24 hours"*
- *"Get details for alert [alert-id]"*

### On-Call and Schedules
- *"Who is on-call right now?"*
- *"What on-call schedules are configured?"*

### Teams and Services
- *"List all operations teams"*
- *"Show me registered services"*

### Incidents (JSM only)
- *"What incidents do I have?"*
- *"Show me open incidents"*

:::info JSM Incidents vs OpsGenie Incidents
In **OpsGenie**, incidents are a separate API resource with their own endpoints.

In **JSM**, incidents are Jira issues with the `[System] Incident` work type. Aurora queries them via the Jira search API. If you also have the Jira connector connected to the same site, both connectors can return the same incidents.
:::

The agent uses the `query_opsgenie` tool with these resource types:

| Resource Type | Description |
|---------------|-------------|
| `alerts` | List open/recent alerts with priority, status, and source |
| `alert_details` | Full alert with description, logs, notes, tags, and runbook URLs |
| `incidents` | List incidents (OpsGenie native or JSM Jira issues) |
| `incident_details` | Full incident details with responders and affected services |
| `teams` | All Operations teams |
| `schedules` | On-call schedules and rotations |
| `on_call` | Current on-call assignments per schedule |
| `services` | Registered services in Operations |

---

## JSM Plan Comparison

Not all JSM features are available on every plan:

| Feature | Free | Standard | Premium | Enterprise |
|---------|------|----------|---------|------------|
| Alert API (read alerts, teams, schedules) | Limited | Yes | Yes | Yes |
| On-call schedules | Basic | Yes | Yes | Yes |
| Incoming integrations (API-based) | Limited | Yes | Yes | Yes |
| **Outgoing webhooks** (real-time to Aurora) | No | No | **Yes** | **Yes** |
| Global integrations | No | No | Yes | Yes |

On **Standard**, Aurora can query alerts and schedules on-demand via the chatbot. Real-time webhook ingestion requires **Premium**.

---

## Disconnecting

1. Navigate to **Connectors** > **OpsGenie / JSM**
2. Click **Disconnect OpsGenie** or **Disconnect JSM Operations**

This removes stored credentials from Vault and clears webhook events from the database. It does not remove incidents already created in Aurora.

---

## Troubleshooting

| Error | Solution |
|-------|----------|
| **"Failed to validate OpsGenie credentials"** | Verify the API key is correct and has read permissions. Check that you selected the correct region (US vs EU) |
| **"Failed to validate JSM Operations credentials"** | Verify your email, API token, and site URL. Ensure your account has access to JSM Operations on that site |
| **"Could not resolve cloud ID from site URL"** | The site URL must be a valid Atlassian site (e.g., `https://yourteam.atlassian.net`). Check for typos |
| **"No raw payload available" on incident** | The Celery worker may not have the OpsGenie task registered. Verify `routes.opsgenie.tasks` is in `celery_config.py`'s `include` list and restart the worker |
| **Webhook events not arriving** | Check that the webhook integration is turned on in OpsGenie/JSM. For local development, use ngrok to expose Aurora |
| **Celery: "unregistered task opsgenie.process_event"** | Add `'routes.opsgenie.tasks'` to the `include` list in `celery_config.py` and rebuild the Celery worker |
| **Duplicate incidents on acknowledge/close** | Ensure you're running the latest code. Only **Create** actions should trigger incident creation |
| **"max_workers must be greater than 0"** | This occurs when there are no on-call schedules configured. Update to the latest code which handles this case |
| **Alerts return 0 results (JSM)** | JSM uses different response formats. Ensure you're running code with the `_normalize()` response handler |
| **Can't find JSM Operations Integrations page** | Navigate to your team in Operations (`/jira/ops/teams`), then click the Integrations tab. On Standard, integrations are team-scoped only |
| **"Access denied" on Jira OAuth** | Check that you're logged into the correct Atlassian account in your browser. The OpsGenie/JSM connector uses Basic auth, not OAuth |
