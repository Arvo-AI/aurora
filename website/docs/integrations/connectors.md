---
sidebar_position: 1
---

# Connectors

Aurora connects to cloud providers and observability tools through connectors. This page provides detailed setup instructions for each integration.

:::info Cloud Connectors Are Optional
Aurora works without any cloud provider accounts. You only need an LLM API key to get started. Add cloud connectors when you're ready to query your infrastructure.
:::

## Cloud Providers

### GCP (Google Cloud Platform)

Two authentication methods are available: **OAuth 2.0** (interactive, per-user consent) or **Service Account Key** (non-interactive, ideal for automation and cross-project setups).

:::tip PII-Safe Configuration
For environments with strict data privacy requirements, see [Configuration > Data Access > GCP](/docs/configuration/data-access/gcp) for PII redaction options and recommended minimal-permission roles.
:::

#### Option A: Service Account Key

Upload a GCP service account JSON key directly — no OAuth consent screen, no redirect URIs, no browser flow. The uploaded key becomes the working identity (Aurora skips its per-user SA impersonation chain).

##### 1. Create a Service Account

```bash
gcloud iam service-accounts create aurora-connector \
  --project=YOUR_PROJECT_ID \
  --display-name="Aurora Connector"
```

##### 2. Grant Roles

At minimum, grant read-only roles for investigation:

```bash
SA=aurora-connector@YOUR_PROJECT_ID.iam.gserviceaccount.com

gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:$SA" --role="roles/viewer"
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:$SA" --role="roles/logging.viewer"
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:$SA" --role="roles/monitoring.viewer"
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:$SA" --role="roles/container.viewer"
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:$SA" --role="roles/compute.viewer"
```

For full investigation access (running commands in sandboxed pods, checking deployments, etc.), add `roles/editor` or the specific roles your team needs.

##### 3. Download Key

```bash
gcloud iam service-accounts keys create aurora-sa-key.json \
  --iam-account=aurora-connector@YOUR_PROJECT_ID.iam.gserviceaccount.com
```

##### 4. Connect via Aurora UI

1. Navigate to **Connectors** > **GCP**
2. Select **Service Account** authentication
3. Upload or paste the JSON key file contents
4. Aurora validates the key, lists accessible projects, and connects

##### 5. Auto-discover all your projects

By default Aurora only sees the project the key was created in. Project enumeration uses Cloud Resource Manager v1 `projects.list`, which returns every project the SA has IAM access to anywhere in the hierarchy. Two roles matter and they do different things:

| Role granted at org/folder | Project shows up in Aurora | SA can investigate it |
|---|---|---|
| `roles/browser` | ✅ yes | ❌ no — directory-listing only |
| `roles/viewer` | ✅ yes | ✅ yes, but grants org-wide read of every resource (logs, IAM, billing, …) |

The recommended split is **`roles/browser` for enumeration** + **`roles/viewer`-family roles per project for inspection**:

```bash
SA=aurora-connector@YOUR_PROJECT_ID.iam.gserviceaccount.com

# Enumerate every project under the org
gcloud organizations add-iam-policy-binding YOUR_ORG_ID \
  --member="serviceAccount:$SA" --role="roles/browser"

# Then grant viewer-tier roles on the projects you actually want Aurora to use
gcloud projects add-iam-policy-binding TARGET_PROJECT \
  --member="serviceAccount:$SA" --role="roles/viewer"
```

Bind at the **organization** level to reach everything; a **folder**-level binding only enumerates projects under that folder (sibling folders stay invisible — bind each individually or move up to the org).

If your security model allows org-wide read in one shot, you can grant `roles/viewer` at the org level instead of `roles/browser`. It's much broader; only use it if you've cleared the blast radius.

##### 6. Manage projects in the connector UI (optional)

Once connected, the **GCP Project Management** dialog lets you scope Aurora to a subset of what the SA can reach:

- **Enable / disable** individual projects. Disabled projects are excluded from Aurora's discovery scans, and the chat agent refuses `cloud_exec` commands that target them (returns `GCP_PROJECT_DISABLED`).
- **Set as Root** pins which enabled project Aurora uses as the default context for agent commands. Lookup order: per-call override → root preference → the `project_id` baked into your SA key. If you disable your SA's default project, pin a root explicitly so commands have somewhere to land — otherwise the auto-injected `--project` would target a disabled project and every command would be blocked.

##### Troubleshooting

| Error | Solution |
|-------|----------|
| "Service account key is malformed" | Verify the JSON file is complete and `private_key` is a valid PEM |
| "Credential refresh failed" | The SA may be disabled or the key revoked — create a new key |
| "No accessible projects" | Grant at least `roles/viewer` on the target project |

---

#### Option B: OAuth 2.0

Interactive OAuth flow — best for development or when users connect their own GCP accounts.

##### 1. Create OAuth Credentials

1. Go to [GCP Console > Credentials](https://console.cloud.google.com/apis/credentials)
2. If this is your first OAuth app, configure the **OAuth consent screen**:
   - User Type: **External** (or Internal for Workspace)
   - App name: `Aurora`
   - User support email: Your email
   - Developer contact: Your email
   - Add your email as a test user (required for External apps)
3. Create OAuth credentials:
   - Click **+ CREATE CREDENTIALS** > **OAuth client ID**
   - Application type: **Web application**
   - Name: `Aurora`
   - Authorized redirect URIs: `http://localhost:5080/callback`
4. Copy the **Client ID** and **Client Secret**

##### 2. Configure Environment

Add to your `.env`:

```bash
CLIENT_ID=123456789-xxxxxxxxxxxxxxxxxxxxxxxxxxxxx.apps.googleusercontent.com
CLIENT_SECRET=GOCSPX-xxxxxxxxxxxxxxxxxxxxxxxxx
```

##### 3. Enable Required APIs

In GCP Console, enable these APIs for your project:
- Cloud Resource Manager API
- Compute Engine API
- Cloud Logging API
- Cloud Monitoring API

##### Troubleshooting

| Error | Solution |
|-------|----------|
| "Redirect URI mismatch" | Ensure redirect URI in GCP Console exactly matches `http://localhost:5080/callback` |
| "Access blocked: App has not been verified" | Add your email as a test user in OAuth consent screen |
| "API not enabled" | Enable required APIs in GCP Console |

---

### AWS (Amazon Web Services)

IAM Role with External ID for secure cross-account access.

#### How It Works

Aurora uses AWS STS AssumeRole to access customer AWS accounts. This requires:
1. Aurora's AWS credentials (for making STS calls)
2. An IAM Role in the customer's account with a trust policy

#### 1. Configure Aurora's AWS Credentials

Aurora needs its own AWS credentials to make STS AssumeRole calls. Add to `.env`:

```bash
AWS_ACCESS_KEY_ID=AKIAXXXXXXXXXXXXXXXX
AWS_SECRET_ACCESS_KEY=your-secret-access-key
AWS_DEFAULT_REGION=us-east-1
```

#### 2. Create IAM Role in Customer Account

Users create this role in their own AWS account:

1. Go to [IAM > Roles](https://console.aws.amazon.com/iam/home#/roles) > **Create role**
2. Select trusted entity:
   - **AWS account**
   - **Another AWS account**
   - Enter Aurora's AWS Account ID (displayed in Aurora onboarding UI)
   - Check **Require external ID**
   - Enter the External ID (displayed in Aurora onboarding UI)
3. Attach permissions:
   - `ReadOnlyAccess` for read-only access
   - `PowerUserAccess` for full access (excluding IAM)
4. Name the role: `AuroraRole`
5. Copy the **Role ARN** after creation

#### Trust Policy Example

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::AURORA_ACCOUNT_ID:root"
      },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {
          "sts:ExternalId": "EXTERNAL_ID_FROM_AURORA"
        }
      }
    }
  ]
}
```

#### Troubleshooting

| Error | Solution |
|-------|----------|
| "Aurora cannot assume this role" | Verify trust policy has correct Aurora Account ID and External ID |
| "Unable to determine Aurora's AWS account ID" | Set `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` in `.env` |
| "Access denied" | Check the IAM role has sufficient permissions |

---

### Azure (Microsoft Azure)

Service Principal authentication for Microsoft Azure.

#### 1. Create App Registration

1. Go to [Azure Portal > App registrations](https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade)
2. Click **+ New registration**
   - Name: `Aurora`
   - Supported account types: Single tenant (or multi-tenant if needed)
   - Redirect URI: **Web** > `http://localhost:5080/azure/callback`
3. After creation, note down:
   - **Application (client) ID**
   - **Directory (tenant) ID**

#### 2. Create Client Secret

1. In the app registration, go to **Certificates & secrets**
2. Click **+ New client secret**
   - Description: `Aurora`
   - Expires: Choose appropriate duration
3. **Copy the secret Value immediately** (it won't be shown again)

#### 3. Grant API Permissions

1. Go to **API permissions** > **+ Add a permission**
2. Select **Azure Service Management**
3. Check **user_impersonation**
4. Click **Grant admin consent for [your tenant]**

#### 4. Assign Role to Subscription

1. Go to [Subscriptions](https://portal.azure.com/#view/Microsoft_Azure_Billing/SubscriptionsBlade)
2. Select your subscription
3. Go to **Access control (IAM)** > **+ Add role assignment**
4. Role: **Reader** (or **Contributor** for write access)
5. Members: Select your `Aurora` app
6. Review + assign

#### Troubleshooting

| Error | Solution |
|-------|----------|
| "No enabled subscription found" | Assign Reader/Contributor role to the app in subscription IAM |
| "AADSTS50011: Reply URL mismatch" | Verify redirect URI exactly matches in App Registration |
| "Insufficient privileges" | Grant admin consent for API permissions |

---

### OVH Cloud

OAuth 2.0 authentication for OVH Cloud with multi-region support.

:::warning HTTPS Required
OVH OAuth2 only accepts **HTTPS** callback URLs. For local development, use ngrok or cloudflared to create an HTTPS tunnel.
:::

#### 1. Set Up HTTPS Tunnel (Local Development)

```bash
# Using ngrok
ngrok http 5080

# Note the HTTPS URL, e.g., https://abc123.ngrok-free.app
```

#### 2. Create OAuth App

1. Go to the API console for your region:
   - EU: https://eu.api.ovh.com/console/
   - CA: https://ca.api.ovh.com/console/
   - US: https://us.api.ovh.com/console/
2. Authenticate with your OVH account
3. Navigate to `/me` > `/me/api/oauth2/client`
4. Use **POST** to create a new client:

```json
{
  "callbackUrls": [
    "https://abc123.ngrok-free.app/ovh/oauth2/callback"
  ],
  "description": "Aurora Cloud Platform",
  "flow": "AUTHORIZATION_CODE",
  "name": "Aurora"
}
```

5. Copy the **Client ID** and **Client Secret** from the response

#### 3. Configure Environment

```bash
NEXT_PUBLIC_ENABLE_OVH=true

# EU Region
OVH_EU_CLIENT_ID=your-eu-client-id
OVH_EU_CLIENT_SECRET=your-eu-client-secret
OVH_EU_REDIRECT_URI=https://abc123.ngrok-free.app/ovh_api/ovh/oauth2/callback

# CA Region (optional)
OVH_CA_CLIENT_ID=your-ca-client-id
OVH_CA_CLIENT_SECRET=your-ca-client-secret
OVH_CA_REDIRECT_URI=https://abc123.ngrok-free.app/ovh_api/ovh/oauth2/callback

# US Region (optional)
OVH_US_CLIENT_ID=your-us-client-id
OVH_US_CLIENT_SECRET=your-us-client-secret
OVH_US_REDIRECT_URI=https://abc123.ngrok-free.app/ovh_api/ovh/oauth2/callback
```

#### Troubleshooting

| Error | Solution |
|-------|----------|
| "OAuth2 credentials not configured for [region]" | Set the corresponding `OVH_[REGION]_CLIENT_ID` and `OVH_[REGION]_CLIENT_SECRET` |
| "OVH connector not enabled" | Set `NEXT_PUBLIC_ENABLE_OVH=true` and restart Aurora |
| "Invalid redirect_uri" | OVH requires HTTPS. Use ngrok or cloudflared |

---

## Communication Tools

### GitHub

Aurora ships with **GitHub App** as the default and recommended auth path.
On-prem deployments that cannot host their own App can fall back to a
classic OAuth App via the `GITHUB_AUTH_MODE` flag (or run both side-by-side
in `hybrid` mode).

#### Auth modes

| `GITHUB_AUTH_MODE` | When to use | What the user sees |
|---|---|---|
| `app` (default) | Most deployments. Per-installation tokens, fine-grained perms, real-time webhooks. | "Install GitHub App" CTA only. |
| `oauth` | On-prem boxes that cannot expose a public webhook URL. | "Connect via OAuth" CTA only. |
| `hybrid` | Migration windows or operators who want to offer both. | Both CTAs; App is recommended. |

#### Path A — GitHub App (recommended)

##### On-prem deployment

When Aurora runs on customer infrastructure (private cloud, on-prem
datacenter, customer-managed VPC) the operator owns both the App and
the ingress. There is **no shared "Aurora SaaS" GitHub App** — each
customer creates their own App in their own GitHub org and points it at
their own Aurora hostname.

**Prerequisites:**

| Requirement | Why |
|---|---|
| Public hostname Aurora can be reached at | GitHub.com posts webhooks from public IPs; tunnel-only setups break under load. |
| Valid TLS cert (Let's Encrypt or chained to a public root) | GitHub refuses webhook delivery to invalid certs. |
| Outbound HTTPS to `api.github.com` | Aurora calls GitHub for installation token mint, repo metadata, etc. |
| GitHub org admin role | Creating an App on an org requires owner. |
| Aurora deployment shell + secrets backend access (Vault or AWS Secrets Manager) | You need to write App private key + webhook secret into the configured backend. |

**Step 1 — Create the App** on the org (or a personal account):

```
# Organization-owned app
https://github.com/organizations/<customer-org>/settings/apps/new

# Personal account
https://github.com/settings/apps/new
```

| Field | Value |
|---|---|
| GitHub App name | `aurora-<customer-slug>` (globally unique). Examples: `aurora-acme-prod`, `aurora-acme-staging`. |
| Homepage URL | `<FRONTEND_URL>` (the user-facing Aurora hostname, e.g. `https://aurora.example.com`) |
| Callback URL | `<API_URL>/github/callback` (OAuth user-authorization redirect) |
| Setup URL | `<API_URL>/github/app/install/callback` (post-install redirect — a **different** route from the Callback URL) |
| Webhook URL | `<API_URL>/github/webhook` |
| Webhook secret | Output of `openssl rand -hex 32` — keep a copy, you'll write it to your secrets backend |
| Where can be installed? | **Only on this account** (locks the App to the customer org) |

:::important Webhook, Setup, and Callback URLs use the **API** host — not the frontend
`/github/webhook`, `/github/app/install/callback`, and `/github/callback` are
**backend** routes (the Flask server on port `5080`). GitHub posts to them
directly and the Next.js frontend does **not** proxy `/github/*`, so they must
point at wherever the backend is publicly reachable — `<API_URL>`. In the
default Helm ingress that's the `api.<domain>` host (`ingress.hosts.api`, e.g.
`api.aurora.example.com`); in local dev it's a tunnel straight to port `5080`.
Only the **Homepage URL** uses the frontend hostname (`<FRONTEND_URL>`).
:::

**Repository permissions** (set in Permissions & events tab):

| Permission | Access level | Why |
|---|---|---|
| Actions | **Read and write** | Read workflow run status for CI/CD correlation; trigger workflow re-runs during remediation |
| Checks | Read-only | CI check-result correlation |
| Commit statuses | Read-only | Correlate commit/CI status with deployments |
| Contents | **Read and write** | Read file contents and repo trees (MCP, metadata generation); create branches and commits when applying fixes |
| Deployments | Read-only | Deploy timeline correlation |
| Discussions | Read-only | Correlate GitHub Discussions with incidents |
| Issues | **Read and write** | Issue-to-incident correlation; comment on and open issues |
| Metadata | Read-only | Required by GitHub for all App installations (auto-selected) |
| Pull requests | **Read and write** | Read PR diffs; post change-gating review comments; open remediation PRs |

:::note Why some permissions need write
Aurora performs its write actions (commit a fix, open a PR, comment on an
issue, re-run a workflow) through the **GitHub MCP** server. An MCP write
call fails if the App installation lacks the matching permission — so every
`Read and write` row above is required by the GitHub MCP tools that use it.
Resources Aurora only reads stay `Read-only`.
:::

**Organization permissions:** Members → Read-only (org membership for owner resolution).

**Subscribe to events** (same Permissions & events tab):

| Event | Purpose |
|---|---|
| Check run | CI check correlation |
| Check suite | CI suite lifecycle |
| Deployment | Deploy timeline |
| Deployment status | Deploy success/failure tracking |
| Issues | Issue-incident correlation |
| Pull request | Change-gating trigger (`opened`, `synchronize`, `reopened`, `ready_for_review`) |
| Workflow run | CI/CD pipeline correlation |

There is no checkbox for the `installation` and `installation_repositories`
events (install/uninstall/suspend, repos added/removed) — GitHub delivers
those to every App automatically, and Aurora relies on them for installation
lifecycle tracking.

**Step 2 — Download the private key**: on the App's settings page after
creation, **Generate a private key** downloads a `.pem` file once. Back
it up before closing the tab.

**Step 3 — Write secrets to the customer's secrets backend**:

These are read from whichever backend `SECRETS_BACKEND` selects, at the path
`aurora/system/github-app/*`. Use the commands for your backend.

Vault (`SECRETS_BACKEND=vault`, default):

```bash
vault kv put aurora/system/github-app/webhook-secret value=<the secret>
vault kv put aurora/system/github-app/private-key value=@<path-to-pem>
```

AWS Secrets Manager (`SECRETS_BACKEND=aws_secrets_manager`) — create the
secrets at the same logical path, in your `AWS_SM_REGION`. For the private
key, `file://` + an **absolute** path makes the CLI read the multi-line PEM
verbatim (an absolute path yields three slashes — `file:///…`); no manual
newline handling needed.

```bash
aws secretsmanager create-secret --name aurora/system/github-app/webhook-secret \
  --secret-string '<the secret>' --region "$AWS_SM_REGION"
aws secretsmanager create-secret --name aurora/system/github-app/private-key \
  --secret-string file:///absolute/path/to/app-private-key.pem --region "$AWS_SM_REGION"
```

:::warning PEM Key Format
The `.pem` file is multi-line with `-----BEGIN RSA PRIVATE KEY-----` headers.
You **must** use `file://` to preserve newlines. Passing the PEM content
directly as a shell string strips newlines and causes "Could not deserialize
key data" errors when Aurora tries to sign installation tokens.
:::

To update a secret that already exists, swap `create-secret` for
`put-secret-value --secret-id <name> --secret-string … --region "$AWS_SM_REGION"`.

**Step 4 — Set Aurora env vars** in the customer's `.env`, with the webhook
and setup URLs pointing at the customer's `<API_URL>` (the backend host — see
the note above). Set `AURORA_ENV=production` and a rotated
`INTERNAL_API_SECRET` so the runtime startup check enforces both.

```bash
GITHUB_AUTH_MODE=app

GITHUB_APP_ID=<numeric, from App settings>
GITHUB_APP_CLIENT_ID=<starts with Iv23l...>
NEXT_PUBLIC_GITHUB_APP_SLUG=<URL slug, e.g. aurora-acme>
GITHUB_APP_WEBHOOK_URL=<API_URL>/github/webhook
GITHUB_APP_SETUP_URL=<API_URL>/github/app/install/callback
GITHUB_APP_WEBHOOK_SECRET=<openssl rand -hex 32>
```

The private key (PEM) is **not** an env var — it lives in your secrets
backend at `aurora/system/github-app/private-key` (Step 3).

:::note Kubernetes (Helm)
A Helm deployment has no `.env`. All the `GITHUB_APP_*` keys (including
`GITHUB_APP_WEBHOOK_SECRET`) already live under `config:` in the chart's
`values.yaml` — fill in your values there and apply with `helm upgrade`.
The private key is **not** a values field — it is read from your secrets
backend at `aurora/system/github-app/private-key`, so store it there
(`vault kv put` or `aws secretsmanager create-secret`) exactly as in Step 3.
:::

**Per-environment Apps**: create separate `aurora-<customer>-prod`,
`aurora-<customer>-staging`, `aurora-<customer>-dev` Apps. Aurora reads
`GITHUB_APP_*` env per deployment, so each env gets its own App keys
and a stray callback-URL change in dev cannot break prod webhook
delivery.

**Verification**: open `<FRONTEND_URL>` in a browser, click **Connectors** in
the left sidebar, find the **GitHub** card and click **Manage**, then use
**Install GitHub App**. The popup goes to GitHub.com, you approve
repository access, and the dialog flips
from "Not connected" to "Available" or "Connected". `aurora-server` logs
should show `200 GET /github/app/install/callback` followed by the new
`installation_id`.

##### Upgrading permissions later

When you change the App's permissions (e.g. adding a repository permission
for a new feature), GitHub does **not** apply them to existing installations
automatically — each installer must approve the new scope first, and Aurora
flags the installation as pending until they do.

To approve:

1. In Aurora, click **Connectors** in the left sidebar, open the **GitHub**
   card's **Manage** dialog, then click **Manage** on the installation under
   **Connected GitHub Installations**. This opens its GitHub settings page
   (`https://github.com/settings/installations/<id>`, or the
   `…/organizations/<org>/settings/installations/<id>` variant for orgs).
2. GitHub shows a banner — *"&lt;App name&gt; is requesting an update to its
   permissions"* — click **Review request**.
3. Review the diff (e.g. *Read and write access to Issues — was read-only*)
   and click **Accept new permissions**.

GitHub then delivers an `installation` event with action
`new_permissions_accepted`, which Aurora processes to refresh the stored
scopes and clear the pending state.

#### Path B — OAuth fallback (on-prem only, when public ingress isn't possible)

1. Go to [GitHub > Settings > Developer settings > OAuth Apps](https://github.com/settings/developers)
2. Click **New OAuth App**
   - Application name: `Aurora`
   - Homepage URL: `http://localhost:3000` (or your real `<FRONTEND_URL>`)
   - Authorization callback URL: `http://localhost:5080/github/callback`
3. Click **Register application** and copy the **Client ID** + a freshly-generated **Client secret**

```bash
GITHUB_AUTH_MODE=oauth   # or hybrid if you also have an App
GH_OAUTH_CLIENT_ID=your-github-client-id
GH_OAUTH_CLIENT_SECRET=your-github-client-secret
```

OAuth gives Aurora a user token and uses polling for repo state. You
lose real-time webhook delivery (no push for `pull_request`,
`workflow_run`, etc.) — incident correlation features that depend on
webhooks degrade to lag-based polling.

#### GitHub Enterprise Server (GHES)

**Not currently supported.** Aurora's GitHub-API call sites still
hardcode `https://api.github.com`. Enabling GHES requires routing every
hardcoded `api.github.com` and `github.com` reference through
configurable base URLs (`GH_API_BASE_URL`, `GH_BASE_URL`). Until that
work lands, GHES customers must run Aurora at a public hostname and
talk to a public GHES URL (which usually defeats the point of GHES) or
use the OAuth fallback above.

#### Troubleshooting

| Error | Solution |
|-------|----------|
| "No authorization code provided" | OAuth callback URL must match what's registered exactly. Default: `http://localhost:5080/github/callback`. |
| "Bad credentials" | Regenerate the OAuth Client secret and update `.env`. |
| "GitHub App install URL is missing the required state parameter" | `GITHUB_APP_SETUP_URL` doesn't match what's registered on the App settings page. Update `.env` to match. |
| "Failed to initiate GitHub OAuth" | `GH_OAUTH_CLIENT_ID`/`SECRET` empty when `GITHUB_AUTH_MODE=oauth` or `hybrid`. Set them and restart. |
| Webhook deliveries fail with 4xx | Webhook secret in your secrets backend doesn't match what's registered on the App. Rewrite it at `aurora/system/github-app/webhook-secret` (`vault kv put` or `aws secretsmanager put-secret-value`, per `SECRETS_BACKEND`) and re-save the App secret. |
| "Could not deserialize key data" in logs | Private-key PEM was stored without its newlines. Re-store with `file://` (AWS SM) or `@` (Vault) so the multi-line PEM is preserved verbatim. |
| Change-gating / incident-prevention reviews not posting | Pull Requests permission is Read-only. Upgrade it to **Read and write** in Permissions & events, then accept the prompt in GitHub. |
| No webhook for `pull_request` events | Event not subscribed. Add it under Permissions & events → Subscribe to events (existing installs receive new events automatically). |
| `installation_id` not stored after install | Setup URL misconfigured or server unreachable from GitHub. Verify `GITHUB_APP_SETUP_URL` matches the App's Setup URL exactly and is reachable from the public internet. |
| "Suspended installation" in logs | An org owner suspended the App. Unsuspend it via GitHub org settings → GitHub Apps. |

---

### Slack

OAuth 2.0 authentication for Slack workspaces.

#### 1. Create Slack App

1. Go to [Slack API Apps](https://api.slack.com/apps) > **Create New App** > **From scratch**
   - App Name: `Aurora`
   - Select your workspace
2. Go to **OAuth & Permissions**
3. Add Redirect URLs:
   - Local: `http://localhost:5080/slack/callback`
   - With tunnel: `https://your-ngrok-url.ngrok-free.app/slack/callback`

#### 2. Add Bot Token Scopes

In **OAuth & Permissions** > **Scopes** > **Bot Token Scopes**, add:

| Scope | Purpose |
|-------|---------|
| `chat:write` | Send messages |
| `channels:read` | List channels |
| `channels:history` | Read channel messages |
| `channels:join` | Join channels |
| `app_mentions:read` | Receive @mentions |
| `users:read` | Get user info |

#### 3. Get Credentials

In **Basic Information**, copy:
- **Client ID**
- **Client Secret**
- **Signing Secret**

#### 4. Configure Environment

```bash
SLACK_CLIENT_ID=your-slack-client-id
SLACK_CLIENT_SECRET=your-slack-client-secret
SLACK_SIGNING_SECRET=your-signing-secret
```

#### Troubleshooting

| Error | Solution |
|-------|----------|
| "bad_redirect_uri" | Redirect URL must match exactly in Slack App settings |
| "Slack OAuth credentials not configured" | Set `SLACK_CLIENT_ID` and `SLACK_CLIENT_SECRET` in `.env` |

---

### Google Chat

Hybrid authentication for Google Chat spaces. User OAuth is used during setup
to create the incidents space in the customer's Google Workspace. A service
account handles all ongoing messaging so notifications and @Aurora replies
appear as the Chat app ("Aurora"), not as a human user.

#### 1. Create a Google Cloud Project

Go to [Google Cloud Console](https://console.cloud.google.com/projectcreate) and create a new project (or select an existing one).

#### 2. Enable the Google Chat API

In your project, go to **APIs & Services → Library**, search for "Google Chat API", and click **Enable**.

[Enable Google Chat API →](https://console.cloud.google.com/apis/library/chat.googleapis.com)

#### 3. Create OAuth Credentials

1. Go to **APIs & Services → Credentials → Create Credentials → OAuth client ID**
2. Select **Web application** as the type
3. Add an **Authorized redirect URI**:
   - Local dev: `http://localhost:5080/google-chat/callback`
   - Production: `https://your-domain.com/google-chat/callback`
   - The exact URL for your deployment is shown on the Google Chat setup page in Aurora — navigate to **Connectors → Google Chat** to copy it.
4. Copy the **Client ID** and **Client Secret** — these are your `GOOGLE_CHAT_CLIENT_ID` and `GOOGLE_CHAT_CLIENT_SECRET` environment variables

[Create OAuth Client →](https://console.cloud.google.com/apis/credentials/oauthclient)

#### 4. Create a Service Account

1. Go to **IAM & Admin → Service Accounts → Create Service Account**
2. Name it something like `aurora-chat-bot`
3. Click **Create and Continue** — no IAM roles are needed. The service account authenticates as the Chat app via the `chat.bot` scope, which is granted automatically when you link it in step 5.
4. On the service account page, go to **Keys → Add Key → Create new key → JSON**
5. The downloaded JSON content is your `GOOGLE_CHAT_SERVICE_ACCOUNT_KEY`

[Create Service Account →](https://console.cloud.google.com/iam-admin/serviceaccounts/create)

#### 5. Configure the Chat App

Go to the [Google Chat API Configuration page](https://console.cloud.google.com/apis/api/chat.googleapis.com/hangouts-chat) and set the following. Leave everything else as default.

> **Important:** Uncheck **Build this Chat app as a Workspace add-on** at the top of the page first.

**Application info:**
- **App name:** `Aurora`
- **Avatar URL:** `https://raw.githubusercontent.com/arvo-ai/aurora/main/client/public/arvologo.png`
- **Description:** `AI incident response assistant`

**Interactive features:**
- Enable **Interactive features**
- Under Functionality, check **Join spaces and group conversations**

**Connection settings:**
- Select the **HTTP endpoint URL** radio button
- Paste your publicly accessible HTTPS endpoint in the field:
  - Local (with tunnel): `https://your-ngrok-url.ngrok-free.app/google-chat/events`
  - Production: `https://your-domain.com/google-chat/events`
  - The exact URL for your deployment is also shown on the Google Chat setup page in Aurora.
- Set **Authentication Audience** to **HTTP endpoint URL**

> **Local development with ngrok:** Run `ngrok http 3000` (pointing to the frontend, not the backend). Aurora's Next.js server rewrites `/google-chat/events` to the backend automatically. Set `FRONTEND_URL` in `.env` to your ngrok HTTPS URL so that Google Chat card buttons (e.g. "View Investigation") link to a reachable address. The OAuth redirect URI (`http://localhost:5080/google-chat/callback`) does not need ngrok because it's a browser redirect that your local machine can reach directly.

**Visibility:**
- Check **Make this Chat app available to specific people and groups** and add your email address (or a Google Group to let multiple people find and add the bot)
- This controls who can *find and add* the bot — once added to a space, all members of that space can interact with it. You don't need to add every user here.

#### 6. Configure Environment

```bash
GOOGLE_CHAT_CLIENT_ID=your-client-id
GOOGLE_CHAT_CLIENT_SECRET=your-client-secret
GOOGLE_CHAT_SERVICE_ACCOUNT_KEY='{"type":"service_account",...}'
```

> **Important:** The service account JSON must be on a **single line** in your `.env` file. Convert the downloaded key file with:
> ```bash
> cat your-key-file.json | jq -c .
> ```
> Then paste the output after `GOOGLE_CHAT_SERVICE_ACCOUNT_KEY=`.

Then rebuild and restart Aurora:

```bash
make down && make dev          # development
make down && make prod-local   # production (build from source)
make down && make prod-prebuilt # production (prebuilt images)
```

#### Troubleshooting

| Error | Solution |
|-------|----------|
| `invalid_scope` | Ensure the service account has the `chat.bot` scope |
| "Google Chat OAuth credentials not configured" | Set `GOOGLE_CHAT_CLIENT_ID` and `GOOGLE_CHAT_CLIENT_SECRET` in `.env` |
| "bad_redirect_uri" | Redirect URI must match exactly in Google Cloud Console OAuth settings |
| Event verification failing | Ensure the Chat app's Authentication Audience is set to **HTTP endpoint URL** and the URL matches your events endpoint |
| Messages appear as your name | Set `GOOGLE_CHAT_SERVICE_ACCOUNT_KEY` to enable the Chat app identity |
| Card buttons do nothing on click | Use **Chrome** — Safari does not reliably handle `openLink` button clicks in Google Chat cards |

---

## Documentation & Project Management

### Atlassian (Confluence + Jira)

OAuth 2.0 authentication for Atlassian Cloud (Confluence and/or Jira), or Personal Access Tokens for Data Center.

One OAuth app covers both products. You choose which to connect in the Aurora UI.

#### Option A: Atlassian Cloud (OAuth)

For Atlassian Cloud (`*.atlassian.net`):

##### 1. Create OAuth App

1. Go to [Atlassian Developer Console](https://developer.atlassian.com/console/myapps/)
2. Click **Create** > **OAuth 2.0 integration**
3. Name: `Aurora`
4. Click **Create**
5. Go to **Distribution**, set Distribution Status to **Sharing**, fill in the required vendor fields (name, privacy policy URL), set Personal Data Declaration to **Yes**, and save. Without this, non-owner users will see "You don't have access to this app."
6. Go to **Permissions** and add scopes for the products you want:
   - **Confluence API** > **Add** > **Configure** > click **Edit Scopes** then **Add granular scopes**:
     - `read:page:confluence`
     - `read:space:confluence`
     - `read:user:confluence`
     - `search:confluence`

     :::warning Use Granular Scopes
     You must add these as **granular scopes**, not classic scopes. Click "Add granular scopes" under Confluence API in the Permissions tab. If only classic scopes are added, the OAuth flow will fail with "scopes not added to the app."
     :::

   - **Jira platform REST API** > **Add** > **Configure**:
     - `read:jira-work`
     - `write:jira-work`
     - `read:jira-user`
7. Go to **Authorization** > **Add** callback URL:
   - `http://localhost:3000/atlassian/callback` (development)
   - `https://your-domain.com/atlassian/callback` (production)
8. Go to **Settings** and copy **Client ID** and **Secret**

##### 2. Configure Environment

```bash
NEXT_PUBLIC_ENABLE_CONFLUENCE=true
NEXT_PUBLIC_ENABLE_JIRA=true
ATLASSIAN_CLIENT_ID=your-client-id
ATLASSIAN_CLIENT_SECRET=your-client-secret
```

##### 3. Connect via Aurora UI

1. Navigate to **Connectors** > **Atlassian**
2. Select which products to connect (Confluence, Jira, or both)
3. Click **Connect with Atlassian**
4. Authorize Aurora in the Atlassian popup
5. Connection complete - the site URL is detected automatically
6. For Jira, choose the agent permission tier (Read Only or Full Access)

#### Option B: Data Center (PAT)

For self-hosted Confluence or Jira instances:

##### 1. Create Personal Access Token

**Confluence:**
1. In Confluence, go to your profile > **Settings** > **Personal Access Tokens**
2. Click **Create token**, name: `Aurora`, set expiry as needed
3. Copy the token

**Jira:**
1. In Jira, go to your profile > **Personal Access Tokens**
2. Click **Create token**, name: `Aurora`, set expiry as needed
3. Copy the token

##### 2. Connect via Aurora UI

1. Navigate to **Connectors** > **Atlassian**
2. Select the products you want and enter per-product:
   - **Base URL**: e.g. `https://confluence.yourcompany.com` or `https://jira.yourcompany.com`
   - **Personal Access Token**: The respective PAT
3. Click **Connect with PAT**

#### URL Limitations

:::warning Short Links Not Supported on Cloud
Confluence Cloud short links (e.g., `https://company.atlassian.net/wiki/x/ABC123`) cannot be resolved via API. Use full page URLs instead:
- `https://company.atlassian.net/wiki/spaces/SPACE/pages/123456/Page+Title`
- `https://company.atlassian.net/wiki/pages/viewpage.action?pageId=123456`

Data Center short links work correctly.
:::

#### Troubleshooting

| Error | Solution |
|-------|----------|
| "Unable to parse Confluence page ID from URL" | Use full page URL instead of short link (Cloud only) |
| "Confluence page URL does not match configured base URL" | Verify the page is from your connected Confluence instance |
| "Confluence credentials expired" | Reconnect via the Connectors page |
| "Failed to validate Confluence PAT" | Verify PAT is valid and not expired |
| "Jira credentials expired" | Reconnect via the Connectors page |
| "Failed to validate Jira PAT" | Verify PAT is valid and not expired |
| "Insufficient Jira scopes" | Ensure OAuth app has `read:jira-work`, `write:jira-work`, and `read:jira-user` scopes |
| "Atlassian OAuth configuration missing" | Set `ATLASSIAN_CLIENT_ID` and `ATLASSIAN_CLIENT_SECRET` in `.env` |
| "You don't have access to this app" | Enable **Sharing** in the Distribution tab of your Atlassian OAuth app |
| "Scopes not added to the app" | Add **granular** Confluence scopes (not classic) in the Permissions tab |

---

## Observability Tools

### PagerDuty

OAuth 2.0 or API Token authentication.

#### Option A: OAuth (Recommended)

1. Go to [PagerDuty](https://app.pagerduty.com/) > **Integrations** > **Developer Mode** > **My Apps**
2. Click **Create New App**
   - Name: `Aurora`
   - Category: Operations
   - Enable **OAuth 2.0**
   - Redirect URL: `http://localhost:5080/pagerduty/oauth/callback`
3. Copy **Client ID** and **Client Secret**

```bash
NEXT_PUBLIC_ENABLE_PAGERDUTY_OAUTH=true
PAGERDUTY_CLIENT_ID=your-client-id
PAGERDUTY_CLIENT_SECRET=your-client-secret
```

#### Option B: API Token

1. Go to [PagerDuty](https://app.pagerduty.com/) > **Integrations** > **API Access Keys**
2. Click **Create New API Key**
3. Users enter the token via the Aurora UI

#### Webhook Configuration

To receive PagerDuty alerts in Aurora:

1. In PagerDuty: **Integrations** > **Generic Webhooks (v3)** > **New Webhook**
2. Webhook URL: `https://your-aurora-domain/pagerduty/webhook/{user_id}`
3. Subscribe to events:
   - `incident.triggered`
   - `incident.acknowledged`
   - `incident.resolved`

---

### Datadog

API Key + Application Key authentication.

:::tip Data Security
If you need to ensure PII is never sent to Aurora (for GDPR, SOC 2, or other compliance requirements), see the [Datadog PII Filtering guide](../configuration/data-access/datadog.md) after completing the setup below.
:::

#### 1. Create API Key

1. Go to [Datadog](https://app.datadoghq.com/) > avatar > **Organization Settings** > **API Keys**
2. Click **+ New Key**
3. Name: `Aurora`
4. Copy the key

#### 2. Create Application Key

1. Go to **Organization Settings** > **Application Keys**
2. Click **+ New Key**
3. Name: `Aurora`
4. Copy the key

#### 3. Identify Your Site

| Site | API URL |
|------|---------|
| US1 | `datadoghq.com` |
| US3 | `us3.datadoghq.com` |
| US5 | `us5.datadoghq.com` |
| EU | `datadoghq.eu` |

Users enter API keys and site via the Aurora UI.

#### Webhook Configuration

1. In Datadog: **Integrations** > **Webhooks** > **+ New**
2. Name: `aurora`
3. URL: `https://your-aurora-domain/datadog/webhook/{user_id}`
4. In monitors, add `@webhook-aurora` to notifications

---

### Grafana

Webhook-based connection for Grafana Cloud or self-hosted instances. No API key required.

#### Setup

1. Open the **Grafana** integration page in Aurora
2. Copy the webhook URL shown on screen
3. In Grafana: **Alerts & IRM** > **Alerting** > **Notification Configuration** > **Contact points** > **New contact point**
   - Type: **Webhook**
   - URL: paste the Aurora webhook URL (`https://your-aurora-domain/grafana/alerts/webhook/{user_id}`)
4. Click **Test** to send a test notification
5. Aurora auto-connects when it receives the test webhook
6. Save the contact point, then add it to a notification policy under **Alerting** > **Notification Configuration** > **Notification policies**

#### Disconnect / Reconnect

Disconnecting in Aurora deactivates the connection — incoming webhooks are rejected until the user clicks **Reconnect**. The Grafana contact point does not need to be reconfigured.

#### How Aurora Processes Grafana Webhooks

Grafana sends grouped webhook payloads containing an `alerts[]` array. Each alert has a
**fingerprint** (hash of rule + labels) that uniquely identifies an alert instance.

Aurora processes each alert in the array individually:

- **Firing** (`status: "firing"`): Processed independently per fingerprint. AlertCorrelator
  checks whether the alert matches an existing open incident (by fingerprint, service
  similarity, and time proximity, selecting the newest by `started_at DESC`). If a match
  is found the alert is attached to that incident; otherwise a new incident is created
  and RCA is triggered.
- **Resolved** (`status: "resolved"`): Matches the original incident by fingerprint and
  attaches the resolution as a correlated alert. No new incident or RCA is created.

**Key behaviors:**

| Scenario | Behavior |
|----------|----------|
| Single alert fires then resolves | Matched by fingerprint, resolution grouped with original incident |
| Multiple alerts in one webhook | Each fingerprint is processed independently; correlated alerts attach to an existing incident, uncorrelated ones create a new incident and RCA |
| Partial resolution (some firing, some resolved) | Each alert handled independently by its status |
| Same alert re-fires weeks later | New incident created; resolution matches newest by `started_at DESC` |
| No matching incident for resolution | Logged and skipped; alert still persisted in `grafana_alerts` |
| Labels change mid-incident | Fingerprint changes, so resolution won't match (labels shouldn't change mid-incident) |

---

### New Relic

User API Key authentication for querying New Relic via NerdGraph (GraphQL).

#### 1. Create a User API Key

1. Log in to [one.newrelic.com](https://one.newrelic.com) and go to **Administration > API keys** (or visit [one.newrelic.com/admin-portal/api-keys](https://one.newrelic.com/admin-portal/api-keys/))
2. Click **Create a key** and select **User** as the key type
3. Name the key (e.g., `Aurora Integration`) and save it
4. Copy the key — it starts with `NRAK-`

#### 2. Find Your Account ID

Your Account ID is shown in the account dropdown or on the API keys page. It is a numeric value (e.g., `1234567`).

#### 3. Identify Your Region

| Region | NerdGraph Endpoint |
|--------|-------------------|
| US | `https://api.newrelic.com/graphql` |
| EU | `https://api.eu.newrelic.com/graphql` |

#### 4. (Optional) License Key

If you want Aurora to write annotations back to New Relic in the future, you can also provide a 40-character License (ingest) key. This is optional and not required for read-only RCA.

#### 5. Connect via Aurora UI

1. Navigate to **Connectors** > **New Relic**
2. Enter your **User API Key**, **Account ID**, and **Region** (US/EU)
3. Optionally provide a **License Key** for write-back capabilities
4. Click **Connect**

#### What Aurora Queries

Aurora uses NerdGraph to:
- Execute arbitrary **NRQL queries** against any telemetry type (metrics, logs, traces, events)
- Fetch **alert issues and incidents** with filtering by state, priority, and time window
- Search **entities** (services, hosts, applications)
- List **accessible accounts** for multi-account setups

All queries go through a single endpoint: `POST https://api.newrelic.com/graphql` with the `API-Key` header.

#### Webhook Configuration

To receive New Relic alerts in Aurora:

1. In New Relic: **Alerts > Destinations** > create a new **Webhook** destination
2. Webhook URL: `https://your-aurora-domain/newrelic/webhook/{user_id}`
3. Under **Workflows**, create or edit a workflow
4. Add a notification channel using the webhook destination
5. Configure the workflow filter for the issues you want Aurora to investigate

#### Polling (Alternative to Webhooks)

Aurora can also poll NerdGraph for active issues. Trigger manually via `POST /newrelic/poll-issues` or schedule via Celery Beat.

#### Troubleshooting

| Error | Solution |
|-------|----------|
| "Invalid API key" | Ensure the key starts with `NRAK-` and belongs to a user with read access to APM, Infrastructure, Logs, and Alerts |
| "Account not found" | Verify the Account ID is correct and the API key has access to that account |
| "EU region issues" | Make sure you selected "EU" in the region selector if your account is on the EU data center |

---

### Sentry

Internal Integration auth token authentication for ingesting issue/error webhooks and querying full stacktraces during RCA.

#### 1. Open the Sentry Integration in Aurora

Navigate to **Connectors** > **Sentry**. The page shows the **Webhook URL** Aurora expects (`https://your-aurora-domain/sentry/webhook/{user_id}`) — copy it now; you'll paste it into Sentry on the next step.

#### 2. Create an Internal Integration in Sentry

1. In Sentry, go to **Settings > Custom Integrations** (under *Developer Settings*)
2. Click **Create New Integration** and choose **Internal Integration**
3. Name it `Aurora` and paste the webhook URL from step 1 into the **Webhook URL** field
4. Under **Permissions**, grant **read** access to: **Issue & Event**, **Project**, **Organization**
5. Under **Webhooks**, subscribe to `issue` and `error` (the `error` resource requires a Business/Enterprise plan)
6. Click **Save Changes**. Under **Credentials**, copy the **Client Secret** (long hex string).
7. Scroll to the **Tokens** section and click **Create New Token**. Sentry does **not** generate an auth token automatically — you must create one. Copy the resulting `sntrys_…` token immediately; it's shown once.

> **Read-only is sufficient.** Aurora never writes to Sentry during RCA. Granting read scopes only means revoking the integration immediately revokes Aurora's access.

#### 3. Identify Your Region

| Region | Host |
|--------|------|
| US | `sentry.io` |
| EU | `de.sentry.io` |

#### 4. Connect via Aurora UI

1. Return to the Aurora Sentry page
2. Fill in:
   - **Organization Slug** — the slug in your Sentry URL (e.g. `acme-co`, not the display name)
   - **Region** — US or EU
   - **Auth Token** — the `sntrys_…` token from step 2
   - **Client Secret** — the secret from step 2
3. Click **Connect**

Aurora validates the token against the org, lists accessible projects, and stores both secrets in Vault.

#### What Aurora Queries

Aurora uses the [Sentry web API](https://docs.sentry.io/api/) to:
- Validate the organization and list accessible projects
- Search **issues** by query (e.g. `is:unresolved`), time window, project, and environment
- Fetch **issue metadata** plus the **latest event** for an issue (includes full stacktrace, breadcrumbs, tags)
- Run **Discover-style event searches** across the org

All requests use `Authorization: Bearer <auth_token>` against `https://sentry.io` (or `https://de.sentry.io` for EU). The integration is strictly read-only.

#### How Aurora Processes Sentry Webhooks

Sentry signs every webhook with HMAC-SHA256 of the raw JSON body using the integration's client secret. The signature lives in the `Sentry-Hook-Signature` header (hex digest, no prefix). Aurora rejects any request whose signature does not constant-time-match the secret stored at connect.

For each accepted webhook Aurora:
1. Persists the raw payload to `sentry_events` (deduplicated by `org_id + issue_id + action`)
2. Runs alert correlation against existing open incidents (services, fingerprints, time window)
3. Either attaches to a correlated incident or creates a new one with `source_type='sentry'`
4. Generates an incident summary from the payload
5. Kicks off a background RCA chat session pre-loaded with the Sentry skill context

Subscribed resources: `issue` and `error`. The route also accepts `installation` and `comment` payloads from Sentry but only `issue` / `error` drive incident creation.

#### Disconnecting

Disconnecting deletes the user's Vault-stored credentials. Webhook deliveries for that user are rejected with `404` until the integration is reconnected. The Internal Integration object in Sentry is untouched — revoke it in Sentry separately if you want to invalidate the token immediately.

#### Troubleshooting

| Error | Solution |
|-------|----------|
| "Invalid Sentry auth token or insufficient permissions" | Verify the token starts with `sntrys_` and the Internal Integration grants `Issue & Event: Read`, `Project: Read`, `Organization: Read` |
| "Sentry organization '\<slug\>' not found" | Use the slug in your Sentry URL (lowercase, hyphenated), not the display name |
| "Webhook signing secret not configured" on incoming webhooks | Reconnect Sentry with the client secret — Aurora cannot verify signatures without it |
| "Invalid webhook signature" | Confirm the **Client Secret** in Aurora matches what Sentry shows under the integration's *Credentials* section. Save the integration in Sentry to rotate if needed |
| No webhooks arriving despite events firing | In Sentry, open the Aurora integration and confirm `issue` and `error` are checked under **Webhooks**, and that the Webhook URL field matches the URL Aurora shows |
| "EU region issues" | Select **EU** in the region selector when connecting if your org is hosted on `de.sentry.io` |

---

### Netdata

API Token authentication.

#### 1. Get API Token

1. Go to your Netdata Cloud dashboard
2. Navigate to **Space settings** > **API tokens**
3. Create a new token for Aurora

Users enter the token via the Aurora UI.

---

### Splunk

API Token authentication for Splunk Cloud or Enterprise.

#### 1. Create a Minimal Role (Recommended)

Aurora only needs the `search` capability. You can use the built-in **power** role, or create a minimal custom role:

1. In Splunk: **Settings** > **Roles** > **New Role**
   - Name: `aurora_readonly`
   - Capabilities: check **search** only
   - Under **Indexes**, set **Indexes searched by default** to `All non-internal indexes` (or specific indexes you want Aurora to access)
2. Create a user with this role, or assign it to an existing user

#### 2. Create an API Token

1. Go to **Settings** > **Tokens** > **New Token**
2. Select the user with the role above
3. Set an expiration and create the token
4. Copy the token

#### 3. Connect via Aurora UI

1. Navigate to **Connectors** > **Splunk**
2. Enter your Splunk instance URL (e.g., `https://your-splunk:8089`)
3. Paste the API token
4. Click **Connect**

#### What Aurora Queries

Aurora uses the Splunk REST API to:
- **Search logs** via `/services/search/jobs/export` (SPL queries)
- **List indexes** via `/services/data/indexes`
- **List sourcetypes** for targeted searches

All calls use Bearer token auth over HTTPS on port 8089.

#### Troubleshooting

| Error | Solution |
|-------|----------|
| "Authentication failed" | Token may be expired or the user lacks the `search` capability |
| "Connection refused" | Verify the URL includes port 8089 and is reachable from Aurora |
| "No results" | Check that the role has the correct indexes in "Indexes searched by default" |

---

## Kubernetes

Aurora can connect to Kubernetes clusters via the kubectl agent.

### Installing the kubectl Agent

The kubectl agent runs in your cluster and connects outbound to Aurora via WebSocket.

#### Prerequisites

- Kubernetes 1.19+
- Helm 3.x
- Cluster-admin access
- Aurora instance running

#### 1. Get Agent Token

1. Log into Aurora UI
2. Navigate to **Connectors** > **Kubernetes**
3. Click **Add Cluster**
4. Copy the generated agent token

#### 2. Build Agent Image

```bash
cd kubectl-agent/src/
docker build -t your-registry/aurora-kubectl-agent:1.0.3 .
docker push your-registry/aurora-kubectl-agent:1.0.3
```

#### 3. Create values.yaml

```yaml
aurora:
  backendUrl: "https://your-aurora-instance.com"
  wsEndpoint: "wss://your-aurora-instance.com/kubectl-agent"
  agentToken: "your-generated-token-here"

agent:
  image:
    repository: your-registry/aurora-kubectl-agent
    tag: "1.0.3"
```

#### 4. Install via Helm

```bash
helm install aurora-kubectl-agent ./kubectl-agent/chart \
  --namespace aurora --create-namespace \
  -f values.yaml
```

#### 5. Verify Installation

```bash
# Check pod status
kubectl get pods -n aurora -l app=aurora-kubectl-agent

# Check logs
kubectl logs -n aurora -l app=aurora-kubectl-agent --tail=50
```

The cluster should appear in Aurora UI with "Connected" status.

See [kubectl-agent README](https://github.com/arvo-ai/aurora/blob/main/kubectl-agent/README.md) for advanced configuration.

---

## Development Tools

### Bitbucket

Bitbucket Cloud supports two authentication methods: **API Token** (recommended — no `.env` setup) or **OAuth** (optional — requires registering a consumer and enabling a feature flag).

:::tip Scopes need read **and** write
Aurora's remediation features open pull requests, push branches/commits, comment on issues, and trigger pipelines. Both auth methods therefore need **write** access to repositories, pull requests, issues, and pipelines — read-only credentials will connect but block those actions.
:::

#### Option A: API Token (recommended)

No environment variables are required — credentials are entered in the Aurora UI and stored in Vault.

##### 1. Create a Scoped API Token

Create a **scoped** API token at [id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens). Classic (unscoped) API tokens are **not** supported.

Grant these scopes:

- `read:user:bitbucket`
- `read:workspace:bitbucket`
- `read:project:bitbucket`
- `read:repository:bitbucket`, `write:repository:bitbucket`
- `read:pullrequest:bitbucket`, `write:pullrequest:bitbucket`
- `read:issue:bitbucket`, `write:issue:bitbucket`
- `read:pipeline:bitbucket`, `write:pipeline:bitbucket`

##### 2. Connect via Aurora UI

1. Navigate to **Connectors** > **Bitbucket**
2. Enter your Atlassian account **email** and the **API token**
3. Click **Connect**

#### Option B: OAuth (optional)

Use OAuth if you prefer a per-user consent flow. This path requires both a feature flag (to show the OAuth option in the UI) and a registered OAuth consumer.

##### 1. Create OAuth Consumer

1. Go to **Bitbucket workspace settings** > **OAuth consumers** > **Add consumer**
   - Name: `Aurora`
   - Callback URL: `{NEXT_PUBLIC_BACKEND_URL}/bitbucket/callback` (e.g. `https://your-aurora-domain/bitbucket/callback`)
   - Permissions: **Account** (Read), **Projects** (Read), **Repositories** (Read & Write), **Pull requests** (Read & Write), **Issues** (Read & Write), **Pipelines** (Read & Write)
2. Copy the **Key** and **Secret**

##### 2. Configure Environment

```bash
# Show the OAuth option in the Connectors UI (frontend flag, default false).
# Without this, only the API Token form is shown.
NEXT_PUBLIC_ENABLE_BITBUCKET_OAUTH=true

BB_OAUTH_CLIENT_ID=your-bitbucket-key
BB_OAUTH_CLIENT_SECRET=your-bitbucket-secret
```

Restart Aurora after setting these, then connect via **Connectors** > **Bitbucket** > **OAuth**.

#### Troubleshooting

| Error | Solution |
|-------|----------|
| "Authentication failed... you are using a classic API token (not supported)" | Create a **scoped** token at [id.atlassian.com](https://id.atlassian.com/manage-profile/security/api-tokens), not a classic one |
| "Missing required scopes: ..." | Recreate the API token with all the read+write scopes listed above |
| "Bitbucket OAuth is not available. Use an API token instead." | `BB_OAUTH_CLIENT_ID`/`BB_OAUTH_CLIENT_SECRET` are not set. Use the API token method, or configure the OAuth consumer |
| OAuth tab not visible in the UI | `NEXT_PUBLIC_ENABLE_BITBUCKET_OAUTH` is not `true`. Set it and restart |

---

## Credential Storage

All connector credentials are stored securely in HashiCorp Vault:

- Credentials are encrypted at rest
- Database stores only Vault path references
- Credentials resolved at runtime
- Never logged or exposed in responses

See [Vault Configuration](/docs/configuration/vault) for details.
