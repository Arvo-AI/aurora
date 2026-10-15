---
sidebar_position: 5
---

# Jira

Aurora integrates with [Jira](https://www.atlassian.com/software/jira) to run root cause analysis on incoming tickets. In **Auto** mode Aurora judges each ticket against an editable memory; in **Filters** mode only tickets that match a saved filter are investigated. You can still ask Aurora to investigate any specific ticket, and posting the result back as a Jira comment is a separate switch that stays off until you enable it.

## What You Get

| Capability | Description |
|------------|-------------|
| **Automatic RCA** | A webhook starts an investigation when Aurora's judge (Auto) or a saved filter (Filters) says so |
| **Incident creation** | Each qualifying Jira issue becomes an Aurora incident with full tracking |
| **Alert correlation** | Incoming issues are correlated with existing incidents by title, service, and time |
| **RCA with full context** | The RCA agent uses the Jira issue details (summary, description, priority, components) plus all connected observability tools |

## Which tickets trigger an RCA

On the Jira connector page, under **Incoming Webhook**, choose a mode. A comment on a ticket never starts an RCA in either mode.

### Auto

Aurora reads each new or changed ticket and decides whether it is worth an investigation, following the **Jira RCA memory** shown on the connector page (also under Settings → Memory as **Context → Jira RCA**). By default:

- Customer bug reports, production incidents, and regressions get an RCA.
- Test or QA investigations usually don't.
- Administrative tasks, like sending an email, don't.

Refine it the same way as the Slack memory: edit it on the connector page, or just tell Aurora in any chat ("never investigate tickets in the QA project") and it updates the memory. Rules are only taken from people talking to Aurora, never from ticket text. Saved filters are ignored in Auto.

A ticket is judged again only when its content changes (summary, description, type, priority, labels, components), not when it moves status or changes assignee. Judging is capped per organization per hour.

### Filters

You set each filter: project, ticket types, and any of priority, labels, components, status, or an extra JQL clause. Leave an optional field empty to allow any value.

Until at least one filter is saved, webhooks are accepted and ignored. That includes ticket types that used to start an RCA automatically (Bug, Incident, Problem, Defect, Production Issue).

## Prerequisites

- Aurora instance accessible from the internet (or ngrok for local dev)
- Jira Cloud or Jira Data Center with admin access to configure webhooks

---

## Setup

### 1. Get the Webhook URL

In Aurora, go to **Connectors** > **Jira** > connect your account. Once connected, the **Incoming Webhook** card shows your webhook URL. Copy it.

The URL format is:

```text
https://<your-aurora-domain>/jira/webhook/<your-user-id>
```

### 2. Configure Jira Webhook

1. In Jira, go to **Settings** (gear icon) > **System** > **Advanced** > **WebHooks**
2. Click **Create a WebHook**
3. Give it a name (e.g., "Aurora RCA")
4. Paste the webhook URL from step 1
5. Under **Issue related events**, check **Issue: created** and **Issue: updated**. Issue updated is what lets a status filter match
6. Optionally set a JQL filter to limit which issues Jira sends. Aurora still applies its own mode (Auto or Filters) to whatever arrives
7. Leave **Exclude body** unchecked (Aurora needs the JSON payload)
8. Click **Create**

### 3. Test

Create a ticket that should be investigated (in Auto, a customer-facing bug; in Filters, one that matches a saved filter). Within a few seconds, you should see:
- A new incident appear in Aurora's incident list
- The RCA status change to "running"
- Investigation results populate within 1-3 minutes

---

## How It Works

```text
Jira ticket created or updated
    |
    v
Webhook POST to Aurora
    |-- Comment → ignored (200)
    |-- Filters mode, no matching filter → ignored (200)
    |-- Filters mode, matching filter → accepted (202)
    |-- Auto mode → accepted (202), then the judge reads the
    |               ticket and the Jira RCA memory
    |                 |-- Skip → nothing happens
    |                 |-- Investigate ↓
           |
           v
    Create Aurora incident
    Correlate with existing incidents
    Generate summary
    Trigger background RCA
           |
           v
    RCA agent investigates using:
    - Jira issue details (summary, description, components, labels)
    - Connected observability (Datadog, Grafana, etc.)
    - Connected infrastructure (AWS, GCP, K8s)
    - Code repos (GitHub, GitLab)
    - Knowledge base and runbooks
```

## Alternative: Jira Automation

Instead of system webhooks, you can use Jira Automation for more control:

1. Go to **Project Settings** > **Automation** > **Create rule**
2. Trigger: **Issue created**
3. Condition: **Issue type is Bug**
4. Action: **Send web request**
   - URL: your Aurora webhook URL
   - Method: POST
   - Headers: `Content-Type: application/json`
   - Body: `{{issue}}`
5. Enable the rule

Jira-side conditions can still narrow what is sent (for example only P1 bugs, or only certain components). Aurora still applies its own mode to every ticket it receives.
