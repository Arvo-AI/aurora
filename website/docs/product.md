---
sidebar_position: 2
---

# Product

Aurora is the screen you open when an alert fires, and the one that keeps working after the incident is closed. These are the surfaces in the app.

## Chat

Ask in natural language. The agent picks tools, runs them, and answers with the evidence it actually collected.

Two modes:

- **Agent** can change things (open a pull request, post a note) after the safety checks.
- **Ask** is read-only. It can inspect logs, metrics, and cluster state, and it refuses writes.

Commands run in a sandbox, not on the Aurora control plane. What is allowed is covered in [Command Safety](/docs/configuration/command-safety).

## Incidents

Alert webhooks become incidents. Aurora starts a background investigation, correlates related alerts, and keeps a status of investigating, analyzed, or merged.

Sources include PagerDuty, Datadog, Grafana, New Relic, OpsGenie / JSM, incident.io, ServiceNow, BigPanda, Splunk, Elastic, Dynatrace, Coroot, ThousandEyes, Netdata, Sentry, and CloudWatch alarms. The full list is on [Connectors](/docs/integrations/connectors).

## Actions

Actions are background agent sessions with instructions you write (or the built-in ones) and a trigger:

| Trigger | When it runs |
|---------|----------------|
| On incident | On creation, after the RCA finishes, or when the incident is resolved |
| On schedule | Every N minutes, hours, or days (minimum 5 minutes) |
| Manual | When someone clicks Run |

Built-in actions:

- **Generate Postmortem** when an incident is resolved
- **Alert Gap Audit** (weekly, off until you enable it) opens pull requests for missing alerts
- **Right-Sizing Audit** (weekly, off until you enable it) compares CPU and memory usage with requests, limits, and autoscaler bounds
- **Memory Consolidation** (nightly) merges duplicate memories and drops stale ones

Each action runs as **Agent** (read-write) or **Ask** (read-only).

## Monitor

Monitor is the ongoing view, not the incident view.

| Tab | What it shows |
|-----|----------------|
| Fleet | Recent investigation runs |
| SRE Metrics | MTTD, MTTS (time to an RCA), and MTTR once a person marks an incident resolved |
| Execution | Which tools the investigations called |
| Artifacts | Living documents the agent keeps up to date. You can edit them; every version is restorable |
| Audit Log | User actions, permission changes, and system events |

## Memory

Org memory is the knowledge the agent reads and writes during an investigation: context, runbooks, infrastructure notes, learned patterns, and postmortems. Admins edit it from **Settings**, or at `/manage-org/knowledge-base`.

Admins can stop viewers from chatting, and can stop viewers or editors from editing memory. A team can override the org's connector rules. See the access section below.

## Connectors

Connectors are optional. Aurora runs with only an LLM key. Add a cloud, a cluster, a chat tool, or an alert source when you want the agent to use it.

Chat surfaces: Slack, Microsoft Teams, and Google Chat. Mention Aurora in a channel or a direct message and it investigates there. One routing pass after an investigation posts to every connected chat tool, following that tool's policy.

Identity: email and password, GitHub, or [SAML SSO](/docs/configuration/sso) (Entra ID, Okta, Google Workspace, Keycloak, and any SAML IdP).

## Access

Three roles: Admin, Editor, Viewer.

Admins can also limit which role may use each connector, and for AWS and GCP which account that role gets. Teams are created under Members. Each person belongs to one team, and a team can override the org rules. A connector the team has not saved still follows the org.

## Where to go next

- [Quickstart](/docs/getting-started/quickstart)
- [Connectors](/docs/integrations/connectors)
- [LLM providers](/docs/integrations/llm-providers)
- [Command safety](/docs/configuration/command-safety)
- [Single sign-on](/docs/configuration/sso)
