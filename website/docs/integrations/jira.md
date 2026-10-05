---
sidebar_position: 5
---

# Jira

Aurora integrates with [Jira](https://www.atlassian.com/software/jira) in two directions:

1. **As a context source for RCA** (always on). Aurora searches Jira for recent changes, open bugs and past incidents before touching infrastructure tools.
2. **As a webhook trigger**. When a supported issue type (Bug, Incident, Problem, Defect, Production Issue) is created, Aurora creates an incident and runs its full RCA pipeline — the same investigation flow triggered by Datadog, Grafana, or PagerDuty alerts.

Aurora does **not** write to Jira unless you turn it on. See [RCA permissions](#rca-permissions).

## RCA Permissions

Atlassian OAuth apps have no bot identity. A token issued through the Atlassian authorization flow acts *as the user who authorized it*, so a comment Aurora posts shows **that person** as its author — there is no "Aurora" account to post as.

Because of that, Jira write access is opt-in per org, configured under **Connectors → Jira → RCA Permissions**:

| Mode | What Aurora may do |
|------|--------------------|
| **Read only** (default) | Search and read issues for investigation context. Never posts. |
| **Comment on existing issues** | Also posts RCA findings as a comment on a matching issue. No new issues or links. |
| **Create & comment** | Also creates new issues, updates fields, and links related issues. |

Whichever mode is on, every comment and issue description Aurora writes is wrapped in an attribution banner stating that it was generated automatically by Aurora through the connected account and that the named author did not write it. This is the only attribution mechanism available, since the author field is not Aurora's to set.

Changing the mode takes effect on the next investigation; it is not retroactive.

## What You Get

| Capability | Description |
|------------|-------------|
| **Jira as RCA context** | The agent searches Jira for recent deploys, open bugs and prior incidents before investigating infrastructure |
| **Automatic RCA from bugs** | Bug issues created in Jira trigger Aurora's background RCA pipeline |
| **Incident creation** | Each qualifying Jira issue becomes an Aurora incident with full tracking |
| **Alert correlation** | Incoming issues are correlated with existing incidents by title, service, and time |
| **Optional write-back** | With writes enabled, findings are filed as a comment (or a new issue) after the investigation completes |

## Supported Issue Types

The webhook triggers RCA for these issue types only (others are ignored):

- Bug
- Incident
- Problem
- Defect
- Production Issue

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
5. Under **Issue related events**, check **Issue: created**
6. Optionally set the JQL filter to `issuetype = Bug` to reduce noise
7. Leave **Exclude body** unchecked (Aurora needs the JSON payload)
8. Click **Create**

### 3. Test

Create a Bug issue in Jira. Within a few seconds, you should see:
- A new incident appear in Aurora's incident list
- The RCA status change to "running"
- Investigation results populate within 1-3 minutes

---

## How It Works

```text
Jira Bug created
    |
    v
Webhook POST to Aurora
    |
    v
Filter: is issue type Bug/Incident/Problem?
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

This gives you per-project control and more flexible filtering (e.g., only P1/P2 bugs, only certain components).
