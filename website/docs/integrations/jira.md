---
sidebar_position: 5
---

# Jira

Aurora integrates with [Jira](https://www.atlassian.com/software/jira) to run root cause analysis on tickets that match a saved filter. A filter is a project and ticket type, plus any priority, label, component, status, or extra JQL you add. You can still ask Aurora to investigate any specific ticket, and posting the result back as a Jira comment is a separate switch that stays off until you enable it.

## What You Get

| Capability | Description |
|------------|-------------|
| **Automatic RCA** | A webhook starts an investigation only for a saved filter |
| **Incident creation** | Each qualifying Jira issue becomes an Aurora incident with full tracking |
| **Alert correlation** | Incoming issues are correlated with existing incidents by title, service, and time |
| **RCA with full context** | The RCA agent uses the Jira issue details (summary, description, priority, components) plus all connected observability tools |

## Which tickets trigger an RCA

On the Jira connector page, under **Incoming Webhook**, filters decide which new tickets start an RCA.

You set each filter: project, ticket types, and any of priority, labels, components, status, or an extra JQL clause. Leave an optional field empty to allow any value. A comment on a ticket does not change these filters and does not start an RCA.

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
6. Optionally set a JQL filter to limit which issues Jira sends. Aurora still only starts an RCA when the issue matches a saved filter
7. Leave **Exclude body** unchecked (Aurora needs the JSON payload)
8. Click **Create**

### 3. Test

Create a ticket that matches a saved filter. Within a few seconds, you should see:
- A new incident appear in Aurora's incident list
- The RCA status change to "running"
- Investigation results populate within 1-3 minutes

---

## How It Works

```text
Matching Jira ticket created
    |
    v
Webhook POST to Aurora
    |
    v
Filter: does this ticket match a saved filter?
    |-- No  → ignored (200)
    |-- Yes → accepted (202)
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

Jira-side conditions can still narrow what is sent (for example only P1 bugs, or only certain components). Aurora starts an RCA only when the ticket also matches a saved filter.
