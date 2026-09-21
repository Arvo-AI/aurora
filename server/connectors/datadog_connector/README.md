# Datadog Connector

API Key + Application Key authentication for Datadog.

Several organizations can be connected at once, each needing its own key pair. Aurora names
each from the org name Datadog reports and queries whichever matches the alert; a custom
label is optional, to override that name. Where the name cannot be read (a key managing
several orgs, or one lacking `org_management`), the org id is used, falling back to
`default`.

## Setup

### 1. Create API Key

1. Go to [Datadog](https://app.datadoghq.com/) > avatar > **Organization Settings** > **API Keys**
2. Click **+ New Key**, name it `Aurora`, copy the key

### 2. Create Application Key

1. Go to **Organization Settings** > **Application Keys**
2. Click **+ New Key**, name it `Aurora`, copy the key

### 3. Identify Your Site

| Site | URL |
|------|-----|
| US1 | `datadoghq.com` |
| US3 | `us3.datadoghq.com` |
| US5 | `us5.datadoghq.com` |
| EU | `datadoghq.eu` |

Site is recorded per organization, so organizations on different sites can coexist.

> API and Application keys are entered by users via the UI.

### 4. Repeat For Each Organization

Switch organization from Datadog's bottom-left **Accounts** menu, repeat steps 1-3, then use
**Add another organization** in Aurora.

## Webhook Configuration

Webhook URL format: `https://your-aurora-domain/datadog/webhook/{user_id}`

The URL is per Aurora **user**, not per organization. With several connected, create this
same webhook in **each** of them or their alerts never arrive.

In Datadog: **Integrations** > **Webhooks** > **+ New**
- Name: `aurora`, URL: Aurora webhook URL

In monitors, add `@webhook-aurora` to notifications. Keeping the name `aurora` in every
organization keeps `@webhook-aurora` uniform.

## Troubleshooting

**Datadog connector not working** — Check that the API and Application keys are correctly configured in the UI

**Investigation used the wrong environment, or one organization's alerts never arrive** —
Check the Datadog page lists every organization with a distinct label, and that the webhook
was created inside each one. A missing webhook sends no alerts while still looking connected.
