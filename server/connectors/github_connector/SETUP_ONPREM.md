# On-Prem GitHub App Setup

A focused walkthrough for operators standing up Aurora on customer
infrastructure (private cloud, on-prem datacenter, customer-managed VPC).
It assumes you already have Aurora running at a stable public hostname
behind your own TLS terminator — see [SETUP_GITHUB_APP.md
§ On-Premises Deployment](./SETUP_GITHUB_APP.md#on-premises-deployment)
for the ingress sketch.

If you are doing **local development** with ngrok, follow the generic
[SETUP_GITHUB_APP.md](./SETUP_GITHUB_APP.md) instead — it's the same
flow with a tunnel substituted for a real hostname.

If your environment **cannot expose a public ingress** (air-gapped,
strict egress policies that prevent GitHub from reaching you), skip to
[the OAuth fallback path](#fallback-no-public-ingress).

---

## Who creates the App

The App lives in **the customer's** GitHub org, not Arvo's. There is no
shared "Aurora SaaS" App. Each customer deployment gets its own App, its
own credentials, and its own webhook deliveries — nothing crosses the
customer boundary.

Required GitHub roles:

| Role | Why |
|---|---|
| **Org owner** (or App-management permission delegated to you) | Creating an App on an org requires owner. A user account works too if the customer prefers a personal-account App, but org-scoped is the supported pattern. |
| **Aurora deployment shell + Vault access** | You need to write the App private key + webhook secret into the customer's Vault and set env vars on Aurora. |

---

## Prerequisites checklist

- [ ] Aurora reachable at a stable public HTTPS URL — call it `BASE_URL`
      (e.g. `https://aurora.example.com`). Not localhost, not ngrok.
- [ ] Valid TLS cert on `BASE_URL` (Let's Encrypt or chained to a public
      root). GitHub refuses webhook delivery on bad certs.
- [ ] The Aurora host can make outbound HTTPS to `api.github.com`.
- [ ] GitHub.com can make inbound HTTPS to `BASE_URL/github/webhook`.
- [ ] You can `kubectl exec` / `docker exec` into the Vault container or
      have admin credentials to write KV entries.
- [ ] Org admin on the GitHub org that will host the App.

---

## Step-by-step: create the App

### 1. Open the App creation page in the customer's org

```
https://github.com/organizations/<customer-org>/settings/apps/new
```

Replace `<customer-org>` with the org slug. Org-level (not personal) is
strongly recommended — installations survive owner departure.

### 2. Fill in the form

| Field | Value (on-prem specific) |
|---|---|
| **GitHub App name** | `aurora-<customer-slug>` (must be globally unique on GitHub.com). Examples: `aurora-acme-prod`, `aurora-acme-staging`. |
| **Description** | Free text. Customer-facing — they see this on their org's installations page. |
| **Homepage URL** | `BASE_URL` |
| **Callback URL** | `BASE_URL/github/app/install/callback` |
| **Setup URL (optional)** | Same as Callback URL. Used for post-install redirect. |
| **Redirect on update** | Checked |
| **Webhook Active** | Checked |
| **Webhook URL** | `BASE_URL/github/webhook` |
| **Webhook secret** | Paste the secret you generate in [Step 3](#3-generate-the-webhook-secret) |

> The hostname **must** match what end users will see in their browser.
> If `BASE_URL` is fronted by Cloudflare (origin behind), use the
> Cloudflare-facing hostname here, not the origin.

### 3. Generate the webhook secret

On your operator workstation:

```bash
openssl rand -hex 32
```

Paste the output into the **Webhook secret** field. Keep a copy — you
will write it to the customer's Vault in [Step 7](#7-write-secrets-to-vault).

### 4. Permissions and events

Use the same permissions and events as the generic guide — these are
identical for on-prem and dev. See [SETUP_GITHUB_APP.md § Step 3](./SETUP_GITHUB_APP.md#step-3-permissions-checklist)
and [§ Step 4](./SETUP_GITHUB_APP.md#step-4-subscribe-to-events).

Quick summary so you don't have to flip:

- **Repository permissions**: Contents (read/write), Metadata (read),
  Issues (read), Pull requests (read), Actions (read), Deployments
  (read), Commit statuses (read), Webhooks (read/write)
- **Account permissions**: Email addresses (read)
- **Events**: `installation`, `installation_repositories`, `pull_request`,
  `issues`, `deployment`, `deployment_status`, `workflow_run`,
  `check_run`, `check_suite`

### 5. Lock the App to this customer's account

Under **Where can this GitHub App be installed?**, select **Only on this
account**. This makes the App private — only the customer org can
install it. Aurora SaaS-style "any account" installation is not
appropriate for an on-prem deployment because the App and its webhook
secret are scoped to one customer.

### 6. Create the App + download the private key

Click **Create GitHub App**. On the resulting settings page:

1. Note the **App ID** (numeric) and **Client ID** (`Iv23l...`)
2. Note the **public slug** in the URL (`https://github.com/apps/<slug>`)
3. Scroll to **Private keys** → **Generate a private key**. A `.pem`
   file downloads. **This file appears once — back it up before closing
   the tab.**

### 7. Write secrets to Vault

The customer's Vault is canonical for the private key and webhook secret
in production. Setting them as env vars works in a pinch but loses
rotation, audit, and KV-versioning.

```bash
# Webhook secret
vault kv put aurora/system/github-app/webhook-secret \
    value="<the secret you generated in step 3>"

# Private key (paste full PEM including BEGIN/END lines)
vault kv put aurora/system/github-app/private-key \
    value=@/path/to/aurora-<customer>.<id>.private-key.pem
```

Confirm:

```bash
vault kv get aurora/system/github-app/webhook-secret
vault kv get aurora/system/github-app/private-key
```

### 8. Set Aurora env vars

In the customer's `.env` (or equivalent secret-injection mechanism for
your orchestrator):

```bash
GITHUB_AUTH_MODE=app

GITHUB_APP_ID=<App ID from step 6>
GITHUB_APP_CLIENT_ID=<Client ID from step 6>
NEXT_PUBLIC_GITHUB_APP_SLUG=<slug from step 6>

# Must match exactly what you registered in step 2
GITHUB_APP_WEBHOOK_URL=<BASE_URL>/github/webhook
GITHUB_APP_SETUP_URL=<BASE_URL>/github/app/install/callback

# Vault path takes precedence; this env var is fallback only
GITHUB_APP_WEBHOOK_SECRET=
```

Restart Aurora's server + worker so the new env is picked up. The
private key is read from Vault at boot — no env var needed for it.

### 9. End-to-end verification

1. Open `BASE_URL` in a browser logged in as a regular Aurora user.
2. Navigate to **Settings → Connectors → GitHub** → **Connect**.
3. Click **Install GitHub App**. The popup goes to GitHub.com, you
   approve repository access, and the popup closes.
4. The dialog flips from "Not connected" to "Available" (or "Connected"
   if a repo is selected).
5. In `aurora-server` logs you should see `200 GET /github/app/install/callback`
   followed by `installation_id=<n>` log lines.
6. In the customer's GitHub org → Settings → Installed GitHub Apps,
   the App appears with the correct repo selection.

If any step fails see [SETUP_GITHUB_APP.md § Troubleshooting](./SETUP_GITHUB_APP.md#troubleshooting).

---

## Per-environment Apps

Each customer deployment env (prod, staging, dev) needs its own App.
Sharing an App across envs means a stray callback-URL change in dev can
break prod webhook delivery, and a leaked dev private key compromises
prod.

Suggested naming:

| Env | App name | Where created |
|---|---|---|
| Production | `aurora-<customer>-prod` | Customer's prod GitHub org |
| Staging | `aurora-<customer>-staging` | Customer's prod GitHub org or a sandbox org |
| Dev | `aurora-<customer>-dev` | Sandbox org or a developer's personal account |

Each env's Aurora reads its own `GITHUB_APP_*` env block, so swapping
deployments is just a `.env` change.

---

## Fallback: no public ingress

When the customer cannot expose any port to the internet (air-gapped,
strict egress, etc.), the GitHub App path is unavailable — GitHub.com
cannot reach a webhook URL behind a firewall.

Two options:

1. **OAuth mode** — set `GITHUB_AUTH_MODE=oauth` (or `hybrid`) and
   register a [GitHub OAuth App](https://github.com/settings/developers)
   instead of a GitHub App. OAuth uses user-controlled tokens; GitHub
   never pushes events into the customer network. Aurora pulls on
   demand. Real-time webhook features (incident correlation on PR
   events, deploy timelines) degrade to polling. Walkthrough: see
   [SETUP_GITHUB_APP.md § On-Premises Deployment](./SETUP_GITHUB_APP.md#on-premises-deployment).
2. **Customer-operated webhook relay** — a tunnel that the customer
   runs themselves (smee.io-style) where Aurora opens a long-lived
   outbound connection and the relay forwards inbound webhooks down the
   wire. Not part of upstream Aurora today; build-it-yourself.

---

## What changes vs. SaaS

If you are coming from the dev/SaaS-shaped guide and wondering what's
specifically different for on-prem:

| Concern | SaaS / dev | On-prem |
|---|---|---|
| Who owns the App | Arvo / shared dev account | Customer's GitHub org |
| Where the private key lives | Dev workstation, ngrok-only | Customer's Vault, never on disk |
| Webhook secret rotation | Manual when you remember | Vault-versioned, rotation = `vault kv put` |
| Hostname | ngrok / localhost / Aurora SaaS | Customer-controlled `BASE_URL` |
| Installations allowed | "Any account" | "Only on this account" (lock to customer) |
| Number of Apps | One shared | One per customer × env |

---

## See also

- [README.md](./README.md) — Connector overview and capability matrix
- [SETUP_GITHUB_APP.md](./SETUP_GITHUB_APP.md) — Generic/dev walkthrough with
  field-level detail, permission tables, and troubleshooting
- [GHES_AUDIT.md](./GHES_AUDIT.md) — File-by-file punch list before
  Aurora can talk to GitHub Enterprise Server (currently unsupported)
