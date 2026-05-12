# Sentry Connector

Sentry Internal Integration token authentication for querying the Sentry web API and ingesting issue/error/alert webhooks.

## Setup

### 1. Create an Internal Integration in Sentry

1. Log in to your Sentry organization at `https://sentry.io` (or `https://de.sentry.io` for EU).
2. Go to **Settings → Custom Integrations** (under the *Developer Settings* section).
3. Click **Create New Integration** and choose **Internal Integration**.
4. Configure:
   - **Name**: `Aurora` (or similar)
   - **Webhook URL**: paste the Aurora webhook URL shown on the Sentry connector page in Aurora before connecting (e.g. `https://your-aurora-domain/sentry/webhook/{user_id}`)
   - **Permissions** (minimum, all read-only):
     - Issue & Event: `Read`
     - Project: `Read`
     - Organization: `Read`
     - Member: `Read` (optional, for richer context)
   - **Webhooks** subscriptions:
     - `issue` (created / resolved / assigned / archived / unresolved)
     - `error` (created) — *Business/Enterprise plans only*
5. Click **Save Changes**.
6. Under **Credentials**, copy the **Client Secret** — Aurora uses it to verify webhook signatures.
7. Scroll to **Tokens** and click **Create New Token**. Sentry does **not** auto-create one on save — you have to generate it yourself. Copy the resulting token (`sntrys_…`) immediately; it's only shown once.

### 2. Connect in Aurora

In Aurora, open **Connectors → Sentry** and provide:

| Field | Source |
|-------|--------|
| **Auth Token** | The auth token from the Internal Integration page |
| **Organization Slug** | Your Sentry org slug (the part of the URL like `acme-co`) |
| **Region** | `US` for `sentry.io`, `EU` for `de.sentry.io` |
| **Client Secret** | Used to verify incoming webhook signatures; rejection is enforced |

Aurora validates the token by calling `GET /api/0/organizations/{slug}/` and stores the credentials encrypted in HashiCorp Vault. Only an encrypted reference is saved in the database.

## What Aurora Queries

All operations are **read-only**:

- `GET /api/0/organizations/{slug}/projects/` — list projects
- `GET /api/0/organizations/{slug}/issues/?query=...&statsPeriod=...` — search issues
- `GET /api/0/organizations/{slug}/issues/{id}/` — issue metadata
- `GET /api/0/organizations/{slug}/issues/{id}/events/latest/` — full event with stacktrace, breadcrumbs, tags
- `GET /api/0/organizations/{slug}/events/?query=...` — Discover-style event search

## Webhook Configuration

Aurora subscribes to the **Sentry Integration Platform** webhooks (not the legacy project service hooks).

Webhook URL format: `https://your-aurora-domain/sentry/webhook/{user_id}`

Signature verification: every webhook arrives with a `Sentry-Hook-Signature` header containing an HMAC-SHA256 of the raw JSON body using your integration's **Client Secret**. Aurora verifies this signature with `hmac.compare_digest` before processing. Requests with missing or invalid signatures are rejected with `401 Unauthorized`.

Supported events:
- `issue` (`created`, `resolved`, `assigned`, `archived`, `unresolved`)
- `error` (`created`) — Business/Enterprise plans only

## Troubleshooting

- **`401 Invalid Sentry auth token`** — Token is wrong, was rotated, or the integration was deleted.
- **`403 lacks required permissions`** — The integration needs at least `issue:read`, `project:read`, and `org:read`.
- **`404 not found` when validating** — The org slug is wrong. It's the slug, not the display name (e.g. `acme-co`, not `Acme Co`).
- **Webhooks not arriving** — Check the integration's *Webhook URL* matches Aurora's URL exactly, and that at least one resource subscription is checked.
- **Invalid webhook signature** — Re-copy the *Client Secret* from Sentry's *Credentials* section; the value is regenerated whenever you reset it.
- **EU region** — Make sure you selected `EU` in Aurora when the integration lives on `de.sentry.io`.
