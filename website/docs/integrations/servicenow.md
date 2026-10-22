---
sidebar_position: 6
---

# ServiceNow

Aurora integrates with [ServiceNow](https://www.servicenow.com/) ITSM so that incidents opened there get an automatic root cause analysis. A Business Rule on your instance sends each incident to Aurora; Aurora decides whether it deserves an investigation (your saved filters, or an LLM judge following a policy your team edits), creates an Aurora incident, runs the full RCA pipeline, keeps the two incidents in sync, and, when you opt in, writes the result back as a work note and can create or update incidents on your behalf.

## What You Get

| Capability | Description |
|------------|-------------|
| **Automatic RCA** | New incidents start a background RCA in **Filters** mode (priority, assignment group, category, state, optional encoded query) or **Auto** mode (an LLM judges each incident against the editable "ServiceNow RCA" memory) |
| **Incident sync (ServiceNow → Aurora)** | Updates refresh the Aurora incident's fields and severity. Resolved, Closed or Canceled resolves it; a reopen in ServiceNow reopens it |
| **Resolution sync (Aurora → ServiceNow)** | Optional: resolving the incident in Aurora sets the ServiceNow incident to Resolved with your close code and the root cause as close notes |
| **Work note write-back** | Optional: the root cause and impact are posted once as a work note when the RCA completes |
| **Agent tools** | Read: `list_servicenow_incidents`, `get_servicenow_incident`. Write (opt-in): `servicenow_add_work_note`, `servicenow_create_incident`, `servicenow_update_incident` |
| **Alert correlation** | Incoming incidents are correlated with open Aurora incidents |

## Prerequisites

- An Aurora instance reachable from the internet (or a tunnel such as `cloudflared` for local development)
- Admin access to the ServiceNow instance once, to create two system properties and one Business Rule
- A ServiceNow account for Aurora, set up as described below

## Roles and Permissions

Aurora authenticates to the ServiceNow Table API with a dedicated integration account. Create it under **User Administration > Users** and give it exactly what the features you enable need.

| Feature | ServiceNow requirement |
|---------|------------------------|
| Connect, read incidents, agent read tools, Filters-mode pickers | Role `itil` (read on `incident`, read on `sys_user_group`) |
| Basic authentication on Yokohama or Zurich | Role `snc_basic_auth_api_access`, **or** tick **Web service access only** on the user. See the note below |
| Work notes and customer-visible comments | Role `itil` (write on `incident.work_notes` / `incident.comments`) |
| Create and update incidents, resolution sync | Role `itil` (create and write on `incident`). Resolving needs a valid `close_code` and `close_notes` on most instances |
| Category list in the Filters picker | Read on `sys_choice`. `itil` alone cannot read it; Aurora then lists the categories seen on recent incidents instead |
| Installing the webhook (one time) | `admin`, to create the two system properties and the Business Rule |

What Aurora never does with this account: delete records, change users, roles or groups, read tables other than `incident`, `sys_user_group` and `sys_choice`, or write anything unless the matching toggle is on.

:::note Basic authentication on Yokohama and Zurich
Since Yokohama, ServiceNow blocks basic authentication on the REST API for any account that can also log in interactively (see [Basic authentication restriction](https://www.servicenow.com/docs/r/zurich/platform-security/authentication/basic-auth-restriction.html)). The instance logs `SNCRestrictBasicAuthUserAuthenticationGate: denied basic-auth API call for interactive-login user` and Aurora reports "Authentication failed", even with the right password. Exempt the integration account by adding the `snc_basic_auth_api_access` role or by ticking **Web service access only**. The **Internal Integration User** flag is not an exemption. Alternatively, connect with OAuth client credentials (below), which is not affected.
:::

### Authentication methods

- **Service account (basic auth)**: create the user under **User Administration > Users > New** as a human or machine account (an AI user type does not work), tick **Web service access only**, use **Set Password** and save; add the roles from the table above. Enter its username and password in Aurora.
- **OAuth 2.0 client credentials**: open **System OAuth > Application Registry > New > Create an OAuth API endpoint for external clients**, copy the **Client ID** and **Client Secret**, and set the integration account as the **OAuth Application User**. The client credentials grant must be enabled on the instance (system property `glide.oauth.inbound.client.credential.grant_type.enabled` = true). Aurora requests tokens from `/oauth_token.do` and keeps them in memory only. Not affected by the basic-auth restriction.

Aurora validates the connection by reading one incident from the Table API before storing anything.

---

## Setup

### 1. Connect ServiceNow

In Aurora, go to **Connectors > ServiceNow**, enter the instance URL (`https://<instance>.service-now.com`; `.servicenowservices.com` is also accepted), choose the authentication method and connect.

### 2. Get the Webhook URL and Secret

The connector page shows the webhook URL, a generated shared secret, and the Business Rule script. Opening that page needs Aurora's connectors write permission, because it shows the secret. The URL has the form:

```text
https://<your-aurora-api>/servicenow/webhook/<your-user-id>
```

The secret can be rotated from the page; the old one stops working immediately.

### 3. Create the System Properties

In ServiceNow, open `sys_properties.list` and create:

| Name | Type | Value |
|------|------|-------|
| `x_aurora.webhook_url` | string | The webhook URL from step 2 |
| `x_aurora.webhook_secret` | password | The shared secret from step 2 |

### 4. Create the Business Rule

1. **System Definition > Business Rules > New**
2. Table: **Incident [incident]**, tick **Advanced**
3. **When to run**: When = **async**, tick **Insert** and **Update** (Update is what keeps the Aurora incident in sync). Async means the incident save never waits on Aurora; the script also caps its call at 10 seconds
4. **Advanced**: paste the script from the connector page, then Submit

The script posts the incident's `sys_id`, `number`, `short_description`, `description`, priority, impact, urgency, state, category, subcategory, assignment group, assignee, configuration item, caller, `opened_at`, `resolved_at`, close code, close notes and `sys_updated_on`, with an `event` of `inserted` or `updated`, and the `X-Aurora-Webhook-Secret` header. Filter conditions on the rule are optional; Aurora's own filters below usually make them unnecessary.

### 5. Choose Which Incidents Trigger an RCA

On the connector page, **Automatic RCA** is either:

- **Filters** (default): add one or more filters. Each filter is any-of on priorities, assignment groups, categories and states, plus an optional encoded-query clause that Aurora confirms against the instance (`sys_id=<incident>^<clause>`) before starting. A new incident matching any filter is investigated. With no filters, nothing is investigated.
- **Auto**: Aurora asks an LLM whether each new incident describes a real failure, following the **ServiceNow RCA** memory shown under the toggle. Edit it there, or tell Aurora in chat ("never investigate incidents assigned to Service Desk") and it updates the memory. Verdicts are remembered per incident; a skipped incident is judged again only when its text changes.

A new incident that does not qualify is ignored and never becomes an Aurora incident. Updates to an incident Aurora already tracks only sync its state; they never start a second RCA. The one exception is an incident that arrived already resolved (so no RCA ran) and is later reopened in ServiceNow: that reopen starts its first RCA.

Incident states: Aurora treats state values 6 (Resolved), 7 (Closed) and 8 (Canceled), or any state whose label is Resolved, Closed or Canceled, as resolved. Other custom states keep the Aurora incident open and are not offered in the Filters picker.

### 6. Test

Create an incident in ServiceNow that reads like an outage. Within seconds it appears on Aurora's Incidents page and the RCA starts.

---

## Write-Back

Everything Aurora writes to ServiceNow is off by default and controlled on the connector page.

| Setting | Default | Effect |
|---------|---------|--------|
| **Post work notes on ServiceNow incidents** | Off | When an RCA completes, the root cause and impact plus a link are posted once as a work note. The agent may also post work notes during unattended investigations |
| **Write scope** | Work notes only | **Create & update incidents** additionally lets the agent create incidents and change state, priority, impact, urgency, assignment, category and close fields |
| **Resolve the ServiceNow incident when it's resolved in Aurora** | Off | Requires the full write scope. Resolving in Aurora sets state Resolved with the configured close code and the root cause as close notes |

Work notes are posted at most once per incident (recurrences attach to the original). If ServiceNow answers 403 to any write, Aurora disables writes for that connection until you reconnect with an account that has the required role.

The switch governs every ServiceNow write, in chat and in unattended investigations alike. Turning it off also resets the write scope to work notes only and turns the resolution sync off.

## Agent Tools

Always available when connected:

- `list_servicenow_incidents`: filter by state, priority, assignment group or free text
- `get_servicenow_incident`: one incident by number or `sys_id`

Bound only when write-back is on (or when a user asks in chat):

- `servicenow_add_work_note`: an internal work note, or a customer-visible comment when asked
- `servicenow_create_incident`, `servicenow_update_incident`: full write scope only. Reference fields accept display names (for example an assignment group name)

## How It Works

```text
ServiceNow incident inserted or updated
    |
    v
Business Rule POSTs to Aurora webhook
    |
    v
Verify shared secret → invalid: 401
    |
    v
Known to Aurora already?
    |-- Yes → sync fields, severity and state (resolve / reopen), no new RCA
    |-- No  → Filters: match a saved filter? / Auto: ask the judge
              |-- No  → ignored, nothing created
              |-- Yes → create Aurora incident, correlate, summarize, run RCA
                        severity from priority: 1 → critical, 2 → high, 3 → medium, 4 and 5 → low
                        |
                        v
                  RCA completes → work note (if enabled)
```

## Security

- Credentials are stored in Vault and never leave the backend.
- The webhook accepts only requests carrying the shared secret, compared in constant time; the secret can be rotated from the connector page.
- The instance URL must be `https` on an allowed ServiceNow domain (`SERVICENOW_ALLOWED_HOSTS` adds custom domains).
- Incident text is treated as untrusted: the Auto-mode judge never follows instructions found in an incident.
- The judge is capped per org per hour, and background RCAs are rate limited, so a leaked webhook URL cannot run up unbounded LLM spend.
