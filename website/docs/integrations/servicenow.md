---
sidebar_position: 6
---

# ServiceNow

Aurora integrates with [ServiceNow](https://www.servicenow.com/) to automatically run root cause analysis when incidents are created. A Business Rule on your instance sends each new incident to Aurora, which creates an Aurora incident and runs its full RCA pipeline, the same investigation flow triggered by Datadog, Grafana, PagerDuty, or Jira. Optionally, Aurora posts the result back to the originating ServiceNow incident as a work note.

## What You Get

| Capability | Description |
|------------|-------------|
| **Automatic RCA from incidents** | Incidents created in ServiceNow trigger Aurora's background RCA pipeline |
| **Incident creation** | Each ServiceNow incident becomes an Aurora incident with full tracking |
| **Alert correlation** | Incoming incidents are correlated with open Aurora incidents |
| **Status sync** | Update events refresh the Aurora incident and mark it resolved when ServiceNow resolves, closes, or cancels it |
| **Work note writeback** | Optionally post the root cause and impact to the ServiceNow incident as a work note |
| **Chat and RCA tools** | `list_servicenow_incidents` and `get_servicenow_incident` let the agent read incidents (read-only) |

## Prerequisites

- Aurora instance accessible from the internet (or ngrok for local dev)
- A ServiceNow instance with admin access to create system properties and Business Rules
- A ServiceNow account for Aurora to read incidents with (see Authentication below)

---

## Setup

### 1. Connect ServiceNow

In Aurora, go to **Connectors** > **ServiceNow** and enter your instance URL:

```text
https://<instance>.service-now.com
```

`.servicenowservices.com` instances are also accepted. Then choose one authentication method:

- **Basic auth**: a dedicated integration user with the `itil` role, or at minimum read access on the `incident` table and write access on work notes.
- **OAuth 2.0 client credentials**: an Application Registry entry with the client credentials grant enabled. Aurora requests tokens from `/oauth_token.do`.

Aurora validates the connection by reading one incident from the Table API.

### 2. Get the Webhook URL and Secret

Once connected, the connector page shows your webhook URL and a generated shared secret. The URL format is:

```text
https://<your-aurora-api>/servicenow/webhook/<your-user-id>
```

The connector page also shows the exact Business Rule script (reproduced below).

### 3. Create the System Properties

In ServiceNow, open **System Properties** and create two properties:

| Name | Type | Value |
|------|------|-------|
| `x_aurora.webhook_url` | string | The webhook URL from step 2 |
| `x_aurora.webhook_secret` | string (mark as password) | The shared secret from step 2 |

### 4. Create the Business Rule

1. Go to **System Definition** > **Business Rules** > **New**
2. Table: **Incident [incident]**
3. Check **Advanced**
4. When: **After** (or **Async**)
5. Check **Insert**. Check **Update** as well if you want Aurora to track status changes.
6. Optionally add filter conditions, for example **Priority is 1 or 2**
7. In the **Script** tab, paste the following:

```javascript
(function executeRule(current, previous /*null when async*/) {
  var endpoint = gs.getProperty('x_aurora.webhook_url');
  var secret = gs.getProperty('x_aurora.webhook_secret');
  if (!endpoint || !secret) {
    gs.warn('[Aurora] webhook URL or secret property not set');
    return;
  }
  var body = {
    event: current.operation() == 'insert' ? 'inserted' : 'updated',
    sys_id: current.getUniqueValue(),
    number: current.getValue('number'),
    short_description: current.getValue('short_description'),
    description: current.getValue('description'),
    priority: current.getValue('priority'),
    priority_label: current.getDisplayValue('priority'),
    impact: current.getValue('impact'),
    urgency: current.getValue('urgency'),
    state: current.getValue('state'),
    state_label: current.getDisplayValue('state'),
    category: current.getDisplayValue('category'),
    assignment_group: current.getDisplayValue('assignment_group'),
    cmdb_ci: current.getDisplayValue('cmdb_ci'),
    caller: current.getDisplayValue('caller_id'),
    opened_at: current.getValue('opened_at'),
    sys_updated_on: current.getValue('sys_updated_on')
  };
  try {
    var r = new sn_ws.RESTMessageV2();
    r.setEndpoint(endpoint);
    r.setHttpMethod('POST');
    r.setRequestHeader('Content-Type', 'application/json');
    r.setRequestHeader('X-Aurora-Webhook-Secret', secret);
    r.setRequestBody(JSON.stringify(body));
    var resp = r.execute();
    var code = resp.getStatusCode();
    if (code < 200 || code > 299) {
      gs.error('[Aurora] webhook returned HTTP ' + code + ': ' + resp.getBody());
    }
  } catch (e) {
    gs.error('[Aurora] webhook call failed: ' + e.message);
  }
})(current, previous);
```

The script sends the incident `sys_id`, `number`, `short_description`, `description`, priority (value and display label), `impact`, `urgency`, state (value and display label), `category`, `assignment_group`, `cmdb_ci`, caller, `opened_at`, `sys_updated_on`, and an `event` of `inserted` or `updated`. Requests carry the `X-Aurora-Webhook-Secret` header.

### 5. Test

Create an incident in ServiceNow. Within a few seconds, you should see:
- A new incident appear in Aurora's incident list
- The RCA status change to "running" (when the incident meets your RCA settings)
- Investigation results populate within 1-3 minutes

---

## RCA Settings

These settings are on the ServiceNow connector page:

| Setting | Default | Description |
|---------|---------|-------------|
| **Investigate new incidents** | On | Start a background RCA for new incidents |
| **Minimum priority** | 5 (all) | Only investigate incidents at or above this priority |
| **Post the RCA as a work note** | Off | Write the result back to the ServiceNow incident |

## How It Works

```text
ServiceNow incident created
    |
    v
Business Rule POSTs to Aurora webhook
    |
    v
Verify shared secret → invalid: rejected
    |
    v
Store event, create Aurora incident
Severity from priority:
  1 → critical, 2 → high, 3 → medium, 4 and 5 → low
Correlate with open incidents
Generate summary
    |
    v
"Investigate new incidents" on AND priority at or above minimum?
    |-- No  → incident created, no RCA
    |-- Yes → trigger background RCA
           |
           v
    RCA agent investigates using:
    - ServiceNow incident details
    - Connected observability (Datadog, Grafana, etc.)
    - Connected infrastructure (AWS, GCP, K8s)
    - Code repos (GitHub, GitLab)
    - Knowledge base and runbooks
```

Update events refresh the Aurora incident and mark it resolved when the ServiceNow state becomes Resolved, Closed, or Canceled. Update events never start a second RCA.

## Posting RCA Notes Back to ServiceNow

When **Post the RCA as a work note** is on and an RCA completes, Aurora posts the root cause and impact, plus a link to the full investigation, as one work note on the originating ServiceNow incident (`PATCH /api/now/table/incident/{sys_id}` with `work_notes`).

- Aurora posts at most one work note per incident.
- Recurrences attach to the original incident and do not post again.
- If ServiceNow returns a 403, the writeback capability is turned off until you reconnect with credentials that can write work notes.

## Chat and RCA Tools

Whenever ServiceNow is connected, the agent can use two read-only tools in chat and during RCA:

- `list_servicenow_incidents`: filter by state, priority, assignment group, or free-text query, with a limit
- `get_servicenow_incident`: fetch one incident by number or `sys_id`

## Security

- Credentials are stored in Vault.
- The webhook is accepted only with the correct shared secret, compared in constant time.
- The instance URL must be `https` on an allowed ServiceNow domain.
- Aurora never writes anything to ServiceNow except the work note, and only when the toggle is on.
