---
sidebar_position: 7
---

# GitHub App Setup

Step-by-step guide to creating and configuring a GitHub App for Aurora, matching
the exact layout of the GitHub App settings UI.

---

## Step 1 — Create the App

Navigate to:

```
https://github.com/organizations/<your-org>/settings/apps/new
```

(For a personal account: `https://github.com/settings/apps/new`)

---

## Step 2 — General Settings

### Basic information

| Field | Value |
|---|---|
| **GitHub App name** | A globally unique name, e.g. `AuroraArvoLocal`, `aurora-acme-prod` |
| **Description** | Optional — e.g. "Aurora incident prevention and SRE assistant" |
| **Homepage URL** | Your Aurora frontend URL (e.g. `http://localhost:3000` for dev, `https://aurora.example.com` for prod) |

### Identifying and authorizing users

| Field | Value |
|---|---|
| **Callback URL** | `<BASE_URL>/github/callback` (e.g. `https://your-tunnel.trycloudflare.com/github/callback` for local dev, or `https://aurora.example.com/github/callback` for prod) |
| **Request user authorization (OAuth) during installation** | Unchecked |
| **Enable Device Flow** | Unchecked |

### Post installation

| Field | Value |
|---|---|
| **Setup URL (optional)** | `<BASE_URL>/github` (e.g. `https://your-tunnel.trycloudflare.com/github`) |
| **Redirect on update** | Checked ✅ — redirects users here when repositories are added/removed from an existing installation |

### Webhook

| Field | Value |
|---|---|
| **Active** | Checked ✅ |
| **Webhook URL** | `<BASE_URL>/github/webhook` (e.g. `https://your-tunnel.trycloudflare.com/github/webhook`) |
| **Webhook secret** | Output of `openssl rand -hex 32` — **save this value**, you'll store it in your secrets backend |
| **SSL verification** | **Enable SSL verification** (always; disable only for self-signed dev certs) |

### Display information

Optional — upload the Aurora logo and set badge background color to `#ffffff`.

### Private keys

After creating the App (Step 5), return here to generate a private key.

---

## Step 3 — Permissions & Events

Navigate to the **Permissions & events** tab in the left sidebar.

### Repository permissions

| Permission | Access level | Why Aurora needs it |
|---|---|---|
| **Actions** | Read-only | Read workflow run status for CI/CD incident correlation |
| **Checks** | Read-only | Correlate CI check results with deployments |
| **Contents** | Read-only | Read file contents, directory trees (MCP tools, repo metadata generation) |
| **Deployments** | Read-only | Track deployment timelines for incident correlation |
| **Issues** | Read-only | Correlate GitHub issues with Aurora incidents |
| **Metadata** | Read-only | Required by GitHub for all App installations (auto-selected) |
| **Pull requests** | **Read and write** | Read PR diffs for change-gating analysis; post review comments with risk findings |

### Organization permissions

| Permission | Access level | Why Aurora needs it |
|---|---|---|
| **Members** | Read-only | Resolve org membership for installation owner resolution |

### Account permissions

None required — leave all as "No access."

### Subscribe to events

Check each of these event types:

| Event | Purpose |
|---|---|
| **Check run** | CI check result correlation |
| **Check suite** | CI suite lifecycle correlation |
| **Deployment** | Deploy timeline correlation |
| **Deployment status** | Deploy success/failure tracking |
| **Installation** | App lifecycle: install, uninstall, suspend, permissions accepted |
| **Installation repositories** | Repos added to or removed from the installation |
| **Issues** | Issue-to-incident correlation |
| **Pull request** | Change-gating risk review trigger (`opened`, `synchronize`, `reopened`, `ready_for_review`) |
| **Workflow run** | CI/CD pipeline correlation |

:::tip
You can add events later from the App settings → Permissions & events without
re-installing. Existing installations receive the new events automatically.
:::

---

## Step 4 — Where can this GitHub App be installed?

At the bottom of the creation form:

| Option | When to use |
|---|---|
| **Only on this account** | Single-org / on-prem deployments (recommended) |
| **Any account** | Multi-tenant SaaS where multiple orgs install Aurora |

Click **Create GitHub App**.

---

## Step 5 — Note the App Credentials

After creation, GitHub shows the **About** section at the top of the App settings page:

| Value | Where to find it | Maps to env var |
|---|---|---|
| **App ID** | Shown as "App ID: <your-app-id>" (numeric) | `GITHUB_APP_ID` |
| **Client ID** | Shown as "Client ID: <your-client-id>" | `GITHUB_APP_CLIENT_ID` |
| **Public link** | Shown as `https://github.com/apps/<slug>` | `NEXT_PUBLIC_GITHUB_APP_SLUG` (just the slug, e.g. `<your-app-slug>`) |

You can also generate a **Client secret** here if needed (click **Generate a new client secret** and save the value).

---

## Step 6 — Generate a Private Key

Scroll down to the **Private keys** section on the General tab:

1. Click **Generate a private key**
2. GitHub downloads a `.pem` file (e.g. `<your-app-slug>.2026-06-09.private-key.pem`)
3. **Back it up immediately** — the key content is shown only once (you can see the SHA-256 fingerprint but cannot re-download the PEM)

This PEM is what Aurora uses to sign JWTs for the GitHub API. It **must** be stored in your secrets backend (next step).

---

## Step 7 — Store Secrets in Your Backend

Aurora reads the App's private key and webhook secret from whichever backend
is configured via `SECRETS_BACKEND`, at path `aurora/system/github-app/*`.

### Vault (`SECRETS_BACKEND=vault`, default)

```bash
# Store the webhook secret
vault kv put aurora/system/github-app/webhook-secret value=<the-webhook-secret>

# Store the PEM private key (@ reads the file content verbatim)
vault kv put aurora/system/github-app/private-key value=@/path/to/your-app.private-key.pem
```

### AWS Secrets Manager (`SECRETS_BACKEND=aws_secrets_manager`)

```bash
# Store the webhook secret
aws secretsmanager create-secret \
  --name aurora/system/github-app/webhook-secret \
  --secret-string '<the-webhook-secret>' \
  --region "$AWS_SM_REGION"

# Store the PEM private key
# IMPORTANT: Use file:// with the ABSOLUTE path to the .pem file.
# This preserves the multi-line PEM format exactly (newlines, headers, footers).
# Three slashes is correct: file:// + /absolute/path = file:///absolute/path
aws secretsmanager create-secret \
  --name aurora/system/github-app/private-key \
  --secret-string file:///absolute/path/to/your-app.private-key.pem \
  --region "$AWS_SM_REGION"
```

:::warning PEM Key Format
The `.pem` file is multi-line with `-----BEGIN RSA PRIVATE KEY-----` headers.
You **must** use `file://` to preserve newlines. Passing the PEM content
directly as a shell string strips newlines and causes **"Could not deserialize
key data"** errors when Aurora tries to sign installation tokens.
:::

**To update** an existing secret (e.g. key rotation):

```bash
aws secretsmanager put-secret-value \
  --secret-id aurora/system/github-app/private-key \
  --secret-string file:///absolute/path/to/new-private-key.pem \
  --region "$AWS_SM_REGION"
```

---

## Step 8 — Configure Aurora Environment

Add to your `.env`:

```bash
GITHUB_AUTH_MODE=app

# From the App's About section (Step 5)
GITHUB_APP_ID=<your-app-id>
GITHUB_APP_CLIENT_ID=<your-client-id>
NEXT_PUBLIC_GITHUB_APP_SLUG=<your-app-slug>

# URLs — must match what's registered in App settings (Step 2)
GITHUB_APP_WEBHOOK_URL=https://your-host.com/github/webhook
GITHUB_APP_SETUP_URL=https://your-host.com/github

# The same webhook secret you stored in Step 7
GITHUB_APP_WEBHOOK_SECRET=<the-webhook-secret>
```

Then restart Aurora:

```bash
make down && make dev          # development
make down && make prod-local   # production (build from source)
make down && make prod-prebuilt # production (prebuilt images)
```

---

## Step 9 — Install the App on Your Org/Account

1. Navigate to `https://github.com/apps/<your-app-slug>/installations/new`
   (e.g. `https://github.com/apps/<your-app-slug>/installations/new`)
2. Select the organization or personal account
3. Choose **All repositories** or select specific ones
4. Click **Install**

GitHub redirects to the Setup URL with an `installation_id` query parameter.
Aurora stores the installation and auto-imports the repos you granted.

---

## Step 10 — Verify

1. Open Aurora UI → **Settings → Connectors → GitHub**
2. The installation should appear with status **Connected**
3. Check `aurora-server` logs:
   ```
   gh_webhook_event=received ... event_type=installation
   ```
4. Open a test PR on a connected repo. If **Incident Prevention** is toggled on
   for that repo, Aurora should post a risk review within ~60–90 seconds.

---

## Upgrading Permissions Later

If you need to add or elevate permissions (e.g. enabling change-gating requires
Pull Requests: Read and write):

1. Go to the App settings → **Permissions & events** (left sidebar)
2. Change the permission level
3. Click **Save changes**
4. GitHub sends an `installation.new_permissions_accepted` webhook once an
   org owner approves the prompt

Users see a banner in GitHub: *"AuroraArvoLocal is requesting updated
permissions"* with an **Accept** button. Aurora processes the acceptance
webhook automatically.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Webhook deliveries fail with 4xx | Webhook secret mismatch between App settings and secrets backend | Re-store the secret in Vault/AWS SM and verify it matches the App's webhook secret |
| "Could not deserialize key data" in logs | PEM key not stored correctly (newlines stripped) | Re-store using `file://` prefix (AWS SM) or `@` prefix (Vault) to preserve formatting |
| "App install URL is missing the required state parameter" | `GITHUB_APP_SETUP_URL` env var doesn't match the Setup URL registered on the App | Update `.env` to match exactly |
| Change-gating reviews not posting | Pull Requests permission is Read-only | Upgrade to Read and write in Permissions & events, then accept the prompt in GitHub |
| No webhook for `pull_request` events | Event not subscribed | Add it in Permissions & events → Subscribe to events |
| `installation_id` not stored after install | Setup URL misconfigured or server not reachable from GitHub | Verify the Setup URL is accessible from the public internet |
| "Suspended installation" in server logs | Org owner suspended the App | Ask the org owner to unsuspend via GitHub org settings → GitHub Apps |
| Private key fingerprint doesn't match | Wrong `.pem` file stored, or key was rotated | Generate a new key in App settings → Private keys, re-store in secrets backend |

---

## Per-Environment Apps

For production, staging, and development environments, create **separate** Apps
(e.g. `aurora-acme-prod`, `aurora-acme-staging`, `aurora-acme-dev`). Each
Aurora deployment reads its own `GITHUB_APP_*` env vars, so a Setup URL change
in dev cannot break prod webhook delivery, and key rotation is isolated per
environment.
