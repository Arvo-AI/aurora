---
sidebar_position: 1
---

# Connectors

Aurora connects to cloud providers and observability tools through connectors. This page provides detailed setup instructions for each integration.

:::info Cloud Connectors Are Optional
Aurora works without any cloud provider accounts. You only need an LLM API key to get started. Add cloud connectors when you're ready to query your infrastructure.
:::

## Catalog

Everything below is connected from **Connectors** in the app. Pages linked here have extra setup steps (OAuth apps, webhooks, IAM). The others take a token or kubeconfig in the UI.

| Category | Connectors |
|----------|------------|
| Infrastructure | [GCP](#gcp-google-cloud-platform), [AWS](#aws-amazon-web-services), [Azure](#azure-microsoft-azure), [OVH](#ovh-cloud), [Scaleway](#scaleway), [Cloudflare](#cloudflare), [Fly.io](#flyio), [Tailscale](#tailscale), [SSH](#ssh) |
| Kubernetes | [kubectl agent or kubeconfig upload](#kubernetes) |
| Monitoring | [Datadog](#datadog), [Grafana](#grafana), [New Relic](#new-relic), [Sentry](#sentry), [Netdata](#netdata), [Splunk](#splunk), [Elastic](#elastic-cloud), [Dynatrace](#dynatrace), [Coroot](#coroot), [ThousandEyes](#thousandeyes), [CloudWatch](#cloudwatch) |
| Incident management | [PagerDuty](#pagerduty), [OpsGenie / JSM](./opsgenie-jsm.md), [incident.io](#incidentio), [ServiceNow](./servicenow.md), [BigPanda](#bigpanda) |
| Chat | [Slack](#slack), [Microsoft Teams](#microsoft-teams), [Google Chat](#google-chat) |
| Code and CI | [GitHub](#github), [GitLab](#gitlab), [Bitbucket](#bitbucket), [Jenkins](#jenkins), [CloudBees](#cloudbees), [Spinnaker](./spinnaker.md) |
| Docs and tickets | [Confluence and Jira](#atlassian-confluence-jira), [Jira automatic RCA](./jira.md), [Notion](#notion), [SharePoint](#sharepoint) |
| Other | [Custom MCP servers](./custom-mcp-servers.md) |

A connector that is not in this table can still be reached by registering its [MCP server](./custom-mcp-servers.md).

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

On each project Aurora should investigate:

```bash
SA=aurora-connector@YOUR_PROJECT_ID.iam.gserviceaccount.com

gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:$SA" --role="roles/viewer"
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:$SA" --role="roles/logging.viewer"
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:$SA" --role="roles/monitoring.viewer"
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:$SA" --role="roles/bigquery.dataViewer"
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:$SA" --role="roles/bigquery.jobUser"
```

`roles/viewer` lists BigQuery datasets but cannot run queries. `bigquery.dataViewer` reads table rows. `bigquery.jobUser` creates the query job. Those are the project roles the Service Account tab asks for, together with `logging.viewer` and `monitoring.viewer`. `roles/viewer` already includes GKE and Compute Engine reads, so `container.viewer` and `compute.viewer` are not extra grants on this key.

To list every project under an organization or folder, grant `roles/browser` there. It only lists projects. A folder binding does not cover sibling folders. Without that grant, Aurora lists the projects the key can already get: the project the key was created in, and any other project where the key already has a role such as `roles/viewer`.

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

The recommended split is **`roles/browser` for enumeration** + the step 2 roles on each project you want investigated:

```bash
SA=aurora-connector@YOUR_PROJECT_ID.iam.gserviceaccount.com

# Enumerate every project under the org
gcloud organizations add-iam-policy-binding YOUR_ORG_ID \
  --member="serviceAccount:$SA" --role="roles/browser"
```

Then repeat the step 2 bindings (`roles/viewer`, `logging.viewer`, `monitoring.viewer`, `bigquery.dataViewer`, `bigquery.jobUser`) on each project Aurora should investigate.

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

#### Option B: OAuth 2.0 {#gcp-oauth}

Interactive OAuth flow. The page is **Connect with Google**. After consent, Aurora creates two service accounts. Neither list is shown on the connect page. The roles in Option A are what you grant on a key you upload.

The Agent account receives `roles/owner`, `roles/iam.serviceAccountUser`, and `roles/iam.serviceAccountTokenCreator`. When the consenting user can see an organization, those roles are granted on the organization and on each project. Otherwise they are granted on each project.

The Ask-mode account receives, on each project, `roles/viewer`, `roles/logging.viewer`, `roles/monitoring.viewer`, `roles/browser`, `roles/cloudasset.viewer`, `roles/compute.viewer`, `roles/container.viewer`, `roles/storage.objectViewer`, `roles/bigquery.dataViewer`, `roles/bigquery.jobUser`, `roles/iam.serviceAccountUser`, and `roles/iam.serviceAccountTokenCreator`.

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

Cross-account IAM role. Aurora assumes the role with STS. The onboarding page shows Aurora's account ID, an external ID, and the trust policy.

The Aurora process needs its own AWS credentials so it can call `AssumeRole`. Set `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` (and a region), or an instance role that can assume customer roles. On EKS, IRSA works the same way. Do not put a customer's keys in those variables.

#### Connect an account

**Connectors → AWS.** Pick an access level, then create the role.

| Access level | Managed policy | Role name the page expects |
|--------------|----------------|----------------------------|
| Read-only | `ReadOnlyAccess` | `AuroraReadOnlyRole` |
| Admin | `AdministratorAccess` | `AuroraAdminRole` |

**CloudFormation (the button on the page).** **Deploy Read-Only Role** or **Deploy Admin Role** opens a Quick-Create stack in the AWS account you are logged into. Review it, acknowledge IAM, and create the stack. Copy the role ARN from the stack **Outputs** tab and paste it into Aurora. You can download the template YAML instead.

**Manual.** Create a role with the trust policy shown on the page (Aurora's account as principal, and the external ID from that page). Attach `ReadOnlyAccess` or `AdministratorAccess`. Paste the role ARN.

A trust-policy change can take a few minutes before assume-role succeeds.

More accounts can be added the same way, or registered in bulk as `account-id,region` lines. Disconnecting in Aurora removes the connection only. Delete the CloudFormation stack (or the role) in AWS to revoke access.

Turn on **CloudWatch Alerts** on the same page when alarms should open incidents. The SNS steps are under [CloudWatch](#cloudwatch).

#### Troubleshooting

| Error | Solution |
|-------|----------|
| "Aurora cannot assume this role" | Trust policy account ID or external ID does not match the onboarding page. Wait a few minutes after editing the policy |
| "Unable to determine Aurora's AWS account ID" | Aurora has no credentials to call STS. Set the server keys, or the instance / IRSA role |
| "Access denied" | The role is missing `ReadOnlyAccess` or `AdministratorAccess` |

---

### Azure (Microsoft Azure)

Service Principal authentication for Microsoft Azure, set up by a script you run in Azure Cloud Shell. Aurora never asks for your Azure password and cannot modify its own permissions.

The script creates **two** service principals:

| Principal | Roles | Used for |
|-----------|-------|----------|
| `Aurora-Agent-*` | Contributor, Azure Kubernetes Service RBAC Writer, Cost Management Reader | Agent mode (remediation) |
| `Aurora-ReadOnly-*` | Log Analytics Reader, Cost Management Reader, Azure Kubernetes Service Cluster User Role, Azure Kubernetes Service RBAC Reader | Ask mode (investigation only) |

Built-in roles only — no custom roles, and no `cluster-admin`. Contributor excludes all `Microsoft.Authorization` writes, so Aurora cannot escalate its own access in either mode.

#### What you need before starting

Azure keeps subscription access and directory access separate, and the script needs both:

- **Owner** or **User Access Administrator** on every enabled subscription in the tenant. That is the scope when you do not pass a management group. If you pass one, the same role has to be on the group. A subscription-level grant does not cover it.
- Permission to **register applications** in Entra ID, to create the two service principals. Subscription Owner does *not* include this.

If your tenant sets *Users can register applications* to **No**, ask an Entra ID admin to either change that setting or grant you the **Application Developer** role. The script checks this and stops before creating anything, so a missing permission costs you nothing but a re-run.

#### 1. Get the script

In Aurora, go to **Connectors → Azure**. **Cloud Shell Setup** is the default: **Copy Script**, then **Open Cloud Shell**, paste, and press Enter. The copied command already includes a management group ID if you filled one in. **Manual Setup** is the download: save the script and upload it to Cloud Shell.

#### 2. Run it in Cloud Shell

Open [Azure Cloud Shell](https://shell.azure.com) (Bash). If you copied the script, paste it and press Enter — it writes itself to `setup-aurora-access.sh` and runs. If you downloaded it, upload the file and run:

```bash
bash setup-aurora-access.sh
```

Cloud Shell already has `az`, `jq`, `python3` and `kubectl`, and you are already authenticated — nothing to install.

By default this covers **every enabled subscription in your current tenant**. To scope to a management group instead, set the management group ID in Aurora before copying the script, or pass it as an argument:

```bash
bash setup-aurora-access.sh <management-group-name>
```

Choose the scope **before the first run**. The script only adds role assignments and never removes them, so running it tenant-wide and then re-running it against a management group leaves the tenant-wide grants in place — it does not narrow them. Each run also creates a new pair of service principals; the script tells you when earlier ones exist so you can delete the ones you no longer use.

:::tip Recommended for multiple subscriptions
With a management group, roles are assigned **once** at the group scope and every subscription beneath it inherits them. Subscriptions you add to the group later are picked up automatically, with no need to re-run the script.

This requires permission to read the management group; if you get an authorization error, ask an owner to grant you **Management Group Reader**, or omit the argument to use per-subscription scope.
:::

#### 3. Paste the output into Aurora

The script prints a JSON block containing both sets of credentials and the list of subscriptions. Paste it into Aurora to finish connecting.

#### Multiple subscriptions

All enabled subscriptions in scope are connected. During an investigation the agent queries every connected subscription and then narrows to whichever one holds the affected resource. Scope is controlled in Azure — by the roles the script assigns — not in Aurora, so to exclude a subscription, run the script against a management group that omits it.

#### Private AKS clusters

A private AKS cluster has no public API server endpoint, so it is unreachable from Cloud Shell and from Aurora. RBAC alone is not enough. The script detects these and lists them at the end — connect each one with the [kubectl agent](#deploy-agent), which runs in-cluster and dials out to Aurora.

#### Revoking access

Delete both service principals; the command is printed at the end of the script run.

```bash
az ad sp delete --id <agent-client-id>
az ad sp delete --id <readonly-client-id>
```

#### Troubleshooting

| Error | Solution |
|-------|----------|
| "Insufficient privileges to complete the operation" | Your tenant restricts app registration. Ask a Global Administrator to run the script, or to grant you the **Application Developer** role |
| "AuthorizationFailed" on `managementGroups/read` | You lack RBAC at the management group. Ask an owner for **Management Group Reader**, or omit the argument to use per-subscription scope |
| "Management group not found or not readable" | Check the name (not the display name), or omit the argument |
| "No enabled subscriptions found in tenant" | The script only uses subscriptions in the tenant you are currently logged in to, since a service principal exists in a single tenant. Run `az account set --subscription <id>` for the right tenant first |
| Authentication fails right after setup | Azure role assignments take 1–2 minutes to propagate. Wait, then retry |
| `kubectl` commands fail on an AKS cluster | The cluster is likely not Entra-integrated, so Azure RBAC roles do not apply to it. Either enable Entra + Azure RBAC on the cluster, or connect it with the Kubernetes connector |

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

The callback Aurora sends is `{public backend}/ovh_api/ovh/oauth2/callback`. The public backend is `NEXT_PUBLIC_BACKEND_URL`. When that value is `http://localhost…`, Aurora uses `NGROK_URL` instead. Register that exact URL. `OVH_*_REDIRECT_URI` is not read.

```json
{
  "callbackUrls": [
    "https://abc123.ngrok-free.app/ovh_api/ovh/oauth2/callback"
  ],
  "description": "Aurora Cloud Platform",
  "flow": "AUTHORIZATION_CODE",
  "name": "Aurora"
}
```

5. Copy the **Client ID** and **Client Secret** from the response

#### 3. Configure Environment

```bash
NEXT_PUBLIC_BACKEND_URL=http://localhost:5080
NGROK_URL=https://abc123.ngrok-free.app

# EU Region
OVH_EU_CLIENT_ID=your-eu-client-id
OVH_EU_CLIENT_SECRET=your-eu-client-secret

# CA Region (optional)
OVH_CA_CLIENT_ID=your-ca-client-id
OVH_CA_CLIENT_SECRET=your-ca-client-secret

# US Region (optional)
OVH_US_CLIENT_ID=your-us-client-id
OVH_US_CLIENT_SECRET=your-us-client-secret
```

#### Troubleshooting

| Error | Solution |
|-------|----------|
| "OAuth2 credentials not configured for [region]" | Set the corresponding `OVH_[REGION]_CLIENT_ID` and `OVH_[REGION]_CLIENT_SECRET` |
| "Invalid redirect_uri" | The client `callbackUrls` entry must be `{NGROK_URL or NEXT_PUBLIC_BACKEND_URL}/ovh_api/ovh/oauth2/callback`, and it must be HTTPS |

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

:::note Hosted deployments only — one-click website signup
If you run a hosted Aurora and want the marketing site's "Install" CTA to
provision an account straight from the GitHub install screen
(`HOSTED_SIGNUP_ENABLED=true`, see [Environment](../configuration/environment.md)),
configure the App additionally as follows:

| Setting | Value |
|---|---|
| Callback URLs | Add `<API_URL>/github/app/signup/callback` and make it the **first** entry. Keep `<API_URL>/github/callback` as a later entry. |
| Request user authorization (OAuth) during installation | **Checked** |
| Client secret | Generate one and set `GITHUB_APP_CLIENT_SECRET` on the server |
| Account permissions → Email addresses | Read-only (lets Aurora use the installer's verified primary email; without it the account gets a `@users.noreply.github.com` address) |

With that checkbox on, GitHub **ignores the Setup URL** and sends every
install — including logged-in users clicking Install inside Aurora — to the
first Callback URL, which is why the signup callback must be first. It
recognises in-app installs by their signed state and hands them to the
regular install handler, so both flows keep working. Self-hosted installs
should leave all of this alone.

After the GitHub install screen the visitor lands here for a second or two
while the one-time handoff token is exchanged for a session, then arrives on
the Connectors page signed in:

![One-click signup landing](/img/one-click-signup-landing.jpg)
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

Vault (`SECRETS_BACKEND=vault`, default). These commands need a `vault` CLI
with `VAULT_ADDR` and a write-capable `VAULT_TOKEN` set; with Docker Compose,
run them in the `vault` container. On a Helm install, follow
[Writing the secrets on Kubernetes](#github-app-secrets-kubernetes) instead.

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
A Helm deployment has no `.env`. All the `GITHUB_APP_*` keys (plus
`GITHUB_AUTH_MODE` and `NEXT_PUBLIC_GITHUB_APP_SLUG`) live under
`secrets.backend:` in the chart's `values.yaml`, not under `config:`. They
render into the `<release>-secrets-backend` Secret, which the server,
celery-worker, celery-beat and chatbot pods load as env. Fill in your values
there and apply with `helm upgrade`.

The private key has **no** values or env fallback: Aurora reads it only from
your secrets backend at `aurora/system/github-app/private-key`. The webhook
secret is read from `aurora/system/github-app/webhook-secret` first and falls
back to `secrets.backend.GITHUB_APP_WEBHOOK_SECRET` when it is missing there.
See [Writing the secrets on Kubernetes](#github-app-secrets-kubernetes).
:::

##### Writing the secrets on Kubernetes {#github-app-secrets-kubernetes}

With `SECRETS_BACKEND=aws_secrets_manager`, run the `aws secretsmanager`
commands from Step 3 from any machine with access to the account. Nothing
runs in the cluster.

With the chart's bundled Vault, run `vault` inside the Vault pod. The pod is
`<release>-vault-0` (`aurora-oss-vault-0` for release `aurora-oss`). It
already sets `VAULT_ADDR=http://127.0.0.1:8200` but has no `VAULT_TOKEN`, so
export one. The app token in `secrets.backend.VAULT_TOKEN` cannot write these
paths; use the root token from `vault operator init`, or another token allowed
to write `aurora/data/system/github-app/*`.

```bash
# Copy the key into the pod
kubectl -n aurora-oss cp ./app-private-key.pem aurora-oss-vault-0:/tmp/key.pem -c vault

# Open a shell in the pod
kubectl -n aurora-oss exec -it aurora-oss-vault-0 -c vault -- sh

# Inside the pod
export VAULT_TOKEN=<ROOT_TOKEN>
vault kv put aurora/system/github-app/private-key value=@/tmp/key.pem
vault kv put aurora/system/github-app/webhook-secret value=<the secret>
rm -f /tmp/key.pem
exit
```

Exporting the token inside the pod shell keeps it out of your local shell
history.

The `aurora-app` policy from the [Kubernetes guide](../deployment/kubernetes.md#6-initialize-vault)
only grants `aurora/data/users/*`, so the app token the pods use cannot
**read** these paths either. Add read access to `aurora/data/system/*` by
rewriting the policy from the same root-token shell:

```bash
vault policy write aurora-app - <<EOF
path "aurora/data/users/*" { capabilities = ["create","read","update","delete","list"] }
path "aurora/metadata/users/*" { capabilities = ["list","read","delete"] }
path "aurora/metadata/" { capabilities = ["list"] }
path "aurora/metadata/users" { capabilities = ["list"] }
path "aurora/data/system/*" { capabilities = ["read"] }
EOF
```

Then restart the Aurora pods (`kubectl -n aurora-oss rollout restart deployment`):
Aurora caches both secrets in memory after the first successful read.

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

In **OAuth & Permissions** > **Scopes** > **Bot Token Scopes**, add (these must match `SLACK_SCOPES` in `server/connectors/slack_connector/oauth.py`):

| Scope | Purpose |
|-------|---------|
| `app_mentions:read` | Receive @mentions |
| `chat:write` | Send messages |
| `channels:join` | Join public channels |
| `channels:manage` | Create channels, invite users, set topics |
| `channels:read` | List public channels |
| `channels:history` | Read public channel messages |
| `groups:read` | List private channels |
| `groups:history` | Read private channel messages |
| `groups:write` | Create private channels |
| `im:write` | Send direct messages |
| `im:history` | Read direct messages |
| `mpim:write` | Send group direct messages |
| `mpim:history` | Read group direct messages |
| `users:read` | Get user info |
| `users:read.email` | Get user email |

#### 3. Enable Event Subscriptions

In **Event Subscriptions**, toggle **Enable Events** on.

- **Request URL**: `https://your-ngrok-url.ngrok-free.app/slack/events`
  Slack validates this URL from its own infrastructure, so it must be a publicly
  reachable HTTPS tunnel (not `localhost`). The server must be running so
  Slack's one-time `url_verification` challenge succeeds.
- Under **Subscribe to bot events**, add:

| Event | Purpose |
|-------|---------|
| `app_mention` | Aurora replies when @mentioned (required) |
| `member_joined_channel` | *Instant* activation of channels Aurora is added to, e.g. incident.io channels (optional — the connector page also reconciles membership on load) |

Save changes. If the app is already installed, Slack will prompt you to
**reinstall** so the new events and scopes take effect.

:::tip Private / self-hosted deployments — use Socket Mode instead
Step 3's Request URL requires Aurora's backend to be reachable from the public
internet (with valid TLS), because Slack POSTs events to it. If Aurora runs
somewhere Slack cannot reach — a private VPC/Kubernetes cluster, behind a
firewall, air-gapped from inbound traffic, or on a laptop — enable
[Socket Mode](#socket-mode-private--self-hosted) instead. Aurora then opens an
*outbound* WebSocket to Slack, so no public URL, ingress, or TLS endpoint is
needed. Note that a dev `ngrok`/`cloudflared` tunnel only *fakes* a public URL
and is not appropriate for a real private production deployment — Socket Mode
is.
:::

#### Socket Mode (private / self-hosted) {#socket-mode-private--self-hosted}

Socket Mode replaces the inbound HTTP webhook with an outbound WebSocket that
Aurora opens to Slack, so Slack `@mentions` and interactive buttons work even
when Aurora's backend has no public inbound endpoint. Outbound messages (incident
alerts, replies) already work over plain HTTPS and are unaffected. This is the
recommended path when a public webhook URL with valid TLS is not available.

1. In your Slack app, go to **Socket Mode** and toggle **Enable Socket Mode** on.
2. When prompted, generate an **App-Level Token** with the `connections:write`
   scope. Copy the token — it starts with `xapp-`.
3. Keep **Event Subscriptions** enabled and subscribe to the same bot events as
   above (`app_mention`, optionally `member_joined_channel`). With Socket Mode
   on, Slack ignores the Request URL, so you do **not** need a public one.
4. If you use interactive buttons (Aurora's suggestion / right-sizing cards), go
   to **Interactivity & Shortcuts** and toggle it on. As with events, no Request
   URL is required under Socket Mode.
5. Configure Aurora's environment. Setting the app-level token is all that's
   needed — its presence turns Socket Mode on:

   ```bash
   SLACK_APP_TOKEN=xapp-...        # app-level token from step 2
   # SLACK_CLIENT_ID / SLACK_CLIENT_SECRET / SLACK_SIGNING_SECRET stay as-is
   ```

   - **Docker Compose**: the `slack_socket_mode` service starts automatically and
     stays idle unless `SLACK_APP_TOKEN` is set.
   - **Kubernetes (Helm)**: set the app token under `secrets.backend.SLACK_APP_TOKEN`
     (it's a secret — never put it in `config`). The chart renders the
     `slack-socket-mode` deployment whenever that token is set. If you supply the
     token via an **existing secret** instead of inline, set
     `slackSocketMode.enabled: true` so the listener still renders (the chart
     can't read inside your secret). It is fixed at a single replica by design.
     Slack distributes an app's events across its open sockets (and tolerates
     briefly overlapping connections during restarts), so extra replicas add no
     throughput here — the listener only acks and hands off to Celery. Scale
     `celeryWorker` instead if event *processing* is ever the bottleneck.
6. Restart Aurora. The listener logs `Slack Socket Mode listener connected.` once
   the outbound WebSocket is established. If the token is set but malformed, the
   listener exits non-zero instead of idling, so the misconfiguration shows up in
   pod/container status rather than only in the logs.

#### 4. Get Credentials

In **Basic Information**, copy:
- **Client ID**
- **Client Secret**
- **Signing Secret**

#### 5. Configure Environment

```bash
SLACK_CLIENT_ID=your-slack-client-id
SLACK_CLIENT_SECRET=your-slack-client-secret
SLACK_SIGNING_SECRET=your-signing-secret

# Optional — only for private/self-hosted deployments with no public webhook URL
# (see "Socket Mode" above). Setting this enables Socket Mode; leave empty for
# HTTP webhooks.
SLACK_APP_TOKEN=          # xapp-... app-level token
```

#### Troubleshooting

| Error | Solution |
|-------|----------|
| "bad_redirect_uri" | Redirect URL must match exactly in Slack App settings |
| "Slack OAuth credentials not configured" | Set `SLACK_CLIENT_ID` and `SLACK_CLIENT_SECRET` in `.env` |
| Aurora doesn't reply to @mentions | Enable **Event Subscriptions**, verify the `/slack/events` Request URL, and subscribe to the `app_mention` bot event, then reinstall the app |
| Channels Aurora is added to aren't auto-registered instantly | Subscribe to the `member_joined_channel` bot event for real-time pickup, then reinstall. Otherwise the connector page reconciles membership on load |
| Deactivating a channel doesn't remove Aurora from it / a channel stays Active after Aurora is kicked | Membership is the source of truth: deactivating calls `conversations.leave`, and the connector page reconciles membership every time it loads, so a channel Aurora was removed from drops off the Active list automatically |
| Aurora's backend has no public URL (private VPC/cluster, firewall, air-gapped inbound) | Use [Socket Mode](#socket-mode-private--self-hosted): set `SLACK_APP_TOKEN` (`xapp-...`) and enable Socket Mode in the Slack app |
| "SLACK_APP_TOKEN does not look like an app-level token" in logs | You supplied a bot/user token (`xoxb-`/`xoxp-`). Socket Mode needs the **App-Level Token** (`xapp-`) with `connections:write`. The listener exits non-zero on this, so the pod/container will show as crashing until it's corrected |
| Socket Mode listener starts but no events arrive | Confirm Socket Mode is enabled in the Slack app, the app is reinstalled, and the bot events are subscribed |
| Aurora replies twice to the same `@mention` | Slack retried the event because the listener did not ack within ~3s, so it was delivered (and handled) twice. Check the gap between the arrival and completion log lines — this is an ack-latency problem, not a duplicate-listener one: Slack distributes events across an app's sockets rather than duplicating them |

---

### Microsoft Teams {#microsoft-teams}

Hybrid authentication (similar to [Google Chat](#google-chat)): **Entra OAuth**
connects Aurora for Graph channel discovery and history. **Bot Framework** handles
@mentions and **all outbound messages** as the Teams app ("Aurora"), not as the
user who clicked Connect.

#### 1. Register an Entra application

1. [Azure Portal](https://portal.azure.com) → **Microsoft Entra ID** → **App registrations** → **New registration** (name e.g. `Aurora`).
2. **Authentication** → **Web** redirect URI:
   - Production: `https://your-api.example.com/teams/callback`
   - Local dev: use an HTTPS tunnel (`NGROK_URL` in `.env`, same idea as Slack) — Entra will not accept bare `localhost`.
3. **Overview** → **Application (client) ID** → `TEAMS_CLIENT_ID` in `.env`.
4. **Certificates & secrets** → **New client secret** → copy **Value** when created (shown once) → `TEAMS_CLIENT_SECRET` in `.env` (also the Bot Framework app password).
5. **API permissions** → **Microsoft Graph** → **Delegated** — match `TEAMS_SCOPES` in `server/connectors/teams_connector/oauth.py` (`Team.ReadBasic.All`, `Channel.ReadBasic.All`, `ChannelMessage.Read.All`, `Chat.Read`, `openid`, `profile`, `offline_access`, etc.).
6. **Grant admin consent** (Entra admin only — e.g. Global Administrator):
   - On **API permissions**, click **Grant admin consent for [tenant name]** and confirm.
   - Permissions should show **Granted for [tenant name]**. Without this, every user gets a long consent screen on Connect.

#### 2. Azure Bot + Teams channel (Azure Portal — backend only)

Create a separate **Azure Bot** resource (not the Entra app alone): **Create a resource** → **Azure Bot** → **Creation type: Use existing app registration** → **App ID** = step‑1 **Application (client) ID** → **App tenant ID** = **Microsoft Entra ID** → **Overview** → **Tenant ID** (Directory GUID, not `common`) → create → **Go to resource**.

After create, if the sidebar only shows **Inputs / Outputs / Template**, you are on the **Deployment** page — click **Go to resource** (or search the bot handle and open **Azure Bot**). Then:

1. **Settings → Configuration** — **Messaging endpoint** `https://your-api.example.com/teams/messages` (or tunnel URL + `/teams/messages` locally). Save.
2. **Channels** — enable **Microsoft Teams** → **Apply**.

Enabling the Teams **channel** on the Azure Bot does **not** add an app under **Teams → Apps**. That requires step 3 in the Developer Portal.

If the bot uses the same app registration as OAuth, set only `TEAMS_CLIENT_ID` (leave `TEAMS_APP_ID` unset). See `server/connectors/teams_connector/README.md` §2.

#### 3. Teams client app ([Developer Portal](https://dev.teams.microsoft.com/) — not Azure)

1. **Apps** → **+ New app** → name it (e.g. Aurora).
2. **App features** → **Bot** → paste the **Microsoft App ID** manually if needed (same as `TEAMS_CLIENT_ID` / Azure Bot — not the **Basic** tab app ID).
3. **What can your bot do?** — leave **all unchecked** for Aurora: do **not** enable **Only send notification** (blocks @mentions); file upload and audio/video are unused.
4. Scopes: **Team** (required); **Personal** / **Group chat** optional → **Save**.
5. **Distribute** → **Publish to your org** (admin), or **Preview in Teams** / **Download** zip and **Upload a custom app** in Teams if tenant policy allows.
6. Before sideloading: run **[app package validation](https://dev.teams.microsoft.com/tools/store-validation)** on the zip. If Teams shows *Manifest parsing error message unavailable*, the validator usually reports the real issue (e.g. add `"supportsChannelFeatures": "tier1"` for **Team** scope on manifest 1.25+, set **Full name** in Basic, remove or complete `webApplicationInfo`). Details: `server/connectors/teams_connector/README.md` §3 (Validate the app package).

Users then find the app in **Teams → Apps → Built for your org**. Full steps: `server/connectors/teams_connector/README.md` §3.

#### 4. Install in each team (Teams client)

Until Microsoft finishes validating the app (or an admin publishes it to the org catalog), sideload the package:

[Download the Aurora Teams app](pathname:///downloads/aurora-teams-app.zip)

Teams needs the `.zip` itself. Safari unzips downloads into a folder by default, so download with Chrome or Edge, or turn off **Safari → Settings → General → Open "safe" files after downloading**. If you already have the folder, open it, select `manifest.json`, `color.png` and `outline.png`, and choose **Compress**. Compressing the folder itself puts the manifest one level down, and Teams rejects the package.

1. In Teams: **Apps** → **Manage your apps** → **Upload an app** → **Upload a custom app**.
2. Select the zip. If upload is blocked, a Teams admin has to allow custom apps (Teams admin center → **Teams apps** → **Setup policies** → **Upload custom apps**).
3. Add Aurora to the team: Team **···** → **Manage team** → **Apps** → **Install**.

This package is built for Aurora's hosted bot. A self-hosted install has its own Entra app id, so it must build its own package in the [Developer Portal](https://dev.teams.microsoft.com/) (step 3) instead of using this zip.

Once the app is in the org catalog, install it from **Teams → Apps → Built for your org** the same way. Users **@mention** the bot by its display name (e.g. `@Aurora`). Channel chats require an @mention.

#### 5. Configure environment

```bash
# Entra app registration → Overview → Application (client) ID
TEAMS_CLIENT_ID=

# Entra → Certificates & secrets → client secret Value (copy when created)
TEAMS_CLIENT_SECRET=

# TEAMS_APP_ID=   # optional; only if bot uses a different Entra app than OAuth

TEAMS_TENANT_ID=<your Entra tenant GUID>

# Local OAuth redirect via tunnel (optional)
NGROK_URL=https://your-tunnel.example.com
```

**`TEAMS_TENANT_ID`** — Entra **Tenant ID** GUID for **Connect** (`login.microsoftonline.com/{value}/...`) and for **single-tenant** Bot Framework outbound tokens when Aurora posts replies to Teams. **Single-tenant** setups (typical self-hosted) must use your tenant GUID — not `common`. Same value as **App tenant ID** on the Azure Bot resource. Restart **aurora-server** and **celery_worker** after changes.

Operator setup (Entra, Azure Bot, manifest, troubleshooting): `server/connectors/teams_connector/README.md` in the Aurora repo.

#### 6. Use Aurora

1. **Connectors** → **Microsoft Teams** → Connect (Entra sign-in) — do this before @mention tests.
2. Install the Aurora Teams app in each Microsoft Team where the bot should work.
3. @mention Aurora in a channel (e.g. `@Aurora hello`) — getting-started steps on **Teams → Manage** clear once Aurora receives it.
4. **Teams → Manage** — refresh/activate channels, set the **incident card** channel, configure **Teams memory** and notification toggles.

#### Troubleshooting

| Issue | What to check |
|-------|----------------|
| OAuth redirect error | Entra redirect URI must match `{backend}/teams/callback`; use `NGROK_URL` for local dev |
| `invalid_request` / `/common` not supported | Single-tenant app: set `TEAMS_TENANT_ID` to your Tenant ID GUID, not `common`; restart server |
| No reply to @mention | Messaging endpoint reachable, `TEAMS_APP_ID` + secret match bot, Teams app installed |
| Incident/routing posts fail | Bot installed in target team/channel; not fixable by OAuth alone |
| Unknown tenant on mention | An admin must Connect Teams in Aurora for that tenant first |
| Azure Bot done but no app in Teams → Apps | Azure ≠ Teams catalog — create & publish the app in [Developer Portal](https://dev.teams.microsoft.com/) (step 3), not only Azure **Channels** |

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

### Atlassian (Confluence + Jira) {#atlassian-confluence-jira}

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
     - `read:blogpost:confluence`
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
ATLASSIAN_CLIENT_ID=your-client-id
ATLASSIAN_CLIENT_SECRET=your-client-secret
```

##### 3. Connect via Aurora UI

1. Navigate to **Connectors** > **Atlassian**
2. Select which products to connect (Confluence, Jira, or both)
3. Click **Connect with Atlassian**
4. Authorize Aurora in the Atlassian popup
5. Connection complete - the site URL is detected automatically
6. For Jira, turn on **Comment back on Jira tickets** if you want Aurora to post after an investigation, then choose **Comment only** or **Create & comment**. This only gates automatic posts — asking Aurora in chat to comment on a ticket works either way

#### Option B: Data Center (PAT)

For self-hosted Confluence or Jira instances:

##### 1. Create Personal Access Token

**Confluence:**
1. In Confluence, go to your profile > **Settings** > **Personal Access Tokens**
2. Click **Create token**, name: `Aurora`, set expiry as needed
3. Copy the token

**Jira (Data Center / Server 8.14+ only — not Jira Cloud):**
1. Click your avatar (top right) → **Profile**
2. In the **left sidebar**, select **Personal access tokens**
3. Click **Create token**, name: `Aurora`, set expiry as needed
4. Copy the token immediately (shown once)

:::note Jira Cloud has no PAT
If you use `*.atlassian.net`, you will not see **Personal access tokens** under Profile. Use **Option A (OAuth)** above instead.
:::

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

API token, or OAuth. OAuth needs `PAGERDUTY_CLIENT_ID`, `PAGERDUTY_CLIENT_SECRET`, and `NEXT_PUBLIC_BACKEND_URL` on the server. The Connect with OAuth button stays hidden until all three are set. The token form works without them.

#### Option A: OAuth

1. Go to [PagerDuty](https://app.pagerduty.com/) > **Integrations** > **Developer Mode** > **My Apps**
2. Click **Create New App**
   - Name: `Aurora`
   - Category: Operations
   - Enable **OAuth 2.0**
   - Redirect URL: `http://localhost:5080/pagerduty/oauth/callback`
   - Scopes: `openid`, `users.read`, `incidents.read`, `incidents.write`, `services.read`
3. Copy **Client ID** and **Client Secret**

```bash
PAGERDUTY_CLIENT_ID=your-client-id
PAGERDUTY_CLIENT_SECRET=your-client-secret
```

All five scopes must be registered on the app. PagerDuty silently drops any scope that is not registered, and a token issued without `users.read` fails validation when connecting.

#### Option B: API Token

Use a **user API token** from a PagerDuty user with write access (Responder or higher). A dedicated "Aurora" user is recommended so notes are attributed clearly.

1. In PagerDuty, go to **User Icon** > **My Profile** > **User Settings** > **API Access**
2. Click **Create API User Token**
3. Users enter the token via the Aurora UI

A read-only or account-level key still works for reading incidents and triggering RCA, but cannot post RCA notes back to PagerDuty.

#### Posting RCA notes back to PagerDuty

When enabled, Aurora adds one plain-text note to the originating PagerDuty incident when an investigation completes: the root cause, the impact (when the report has one), a link to the full investigation, and a disclaimer. Recurrences of an earlier incident and re-summaries after a follow-up chat do not post.

- **Where**: the toggle lives on the PagerDuty connector page under **RCA Notes**. It is off by default and org-wide.
- **Credential requirement**: the toggle can only be turned on when the stored credentials were proven able to write incidents (user token from a user with write access, or an OAuth token granted `incidents.write`). Account-level keys and read-only users are rejected with a reason.
- **Notes are permanent**: PagerDuty has no API to edit or delete notes. Aurora posts at most one note per incident.
- **Existing OAuth connections**: connections made before `incidents.write` was requested keep their read-only token. Disconnect and reconnect PagerDuty to grant it.
- **Rotating to a read-only token** turns the toggle off automatically.

#### Webhook Configuration

To receive PagerDuty alerts in Aurora:

1. In PagerDuty: **Integrations** > **Generic Webhooks (v3)** > **New Webhook**
2. Webhook URL: `https://your-aurora-domain/pagerduty/webhook/{user_id}`
3. Subscribe to events:
   - `incident.triggered`
   - `incident.acknowledged`
   - `incident.resolved`

---

### ServiceNow

Connect with the instance URL and either Basic auth (an integration user with the `itil` role) or OAuth 2.0 client credentials. Aurora gives you a webhook URL and shared secret; a Business Rule on the Incident table POSTs new incidents to Aurora, which creates an incident and starts an RCA. Aurora can optionally post the root cause back to the incident as one work note.

See the [ServiceNow integration guide](./servicenow.md) for the Business Rule script, RCA settings, and security details.

---

### Datadog

API Key + Application Key authentication.

:::tip Data Security
If you need to ensure PII is never sent to Aurora (for GDPR, SOC 2, or other compliance requirements), see the [Datadog PII Filtering guide](../configuration/data-access/datadog.md) after completing the setup below.
:::

:::info Multiple organizations
Several Datadog organizations can be connected at once, each needing its own API +
application key pair. Aurora names each one from the org name Datadog reports and queries
whichever matches the alert under investigation; a custom label is optional, to override that
name with something your team recognises.

Where the name cannot be read — a key managing several organizations, or one without the
`org_management` permission — Aurora falls back to the organization id, then to `default`.
Set a label in that case so the organizations stay tellable apart.
:::

#### 1. Create a service account key pair

A service account keeps the integration off a personal login. It needs both an API key and an application key.

1. In Datadog, open the bottom-left **Accounts** menu and switch to the organization you want.
2. **Organization Settings → Service Accounts**: create a service account for Aurora.
3. **Organization Settings → API Keys**: create an API key for that service account.
4. **Organization Settings → Application Keys**: create an application key on the same service account. It needs read access to logs, metrics, monitors, and events: `logs_read`, `metrics_read`, `monitors_read`, `events_read`.
5. Note the service account name. Aurora shows it as the owner of the connection.

#### 2. Identify Your Site

| Site | API URL |
|------|---------|
| US1 | `datadoghq.com` |
| US3 | `us3.datadoghq.com` |
| US5 | `us5.datadoghq.com` |
| EU | `datadoghq.eu` |
| AP1 | `ap1.datadoghq.com` |
| AP2 | `ap2.datadoghq.com` |

#### 3. Connect in Aurora

On the Datadog page, enter the API key, the application key, and the site. A label is optional. Site is recorded per organization, so organizations on different Datadog sites can coexist.

#### 4. Connect Additional Organizations

1. Switch organization from the bottom-left **Accounts** menu in Datadog
2. Repeat the key pair and site steps in that organization
3. In Aurora, use **Add another organization** on the Datadog page and enter the new keys

#### Webhook Configuration

Aurora shows the URL after connect. It belongs to the Aurora account, not to one Datadog organization.

1. In Datadog: **Integrations**, search for the **Webhooks** integration.
2. Create a webhook with that URL. A shared secret for signing is optional.
3. In a monitor's notification, add `@webhook-<name>`, using the name you gave the webhook.
4. To test: **Monitors → New Monitor → Metric**, any easy condition, the same `@webhook-<name>` in **Notify your team**, then **Test notifications**.

:::warning One URL, every organization
With several organizations connected, create this webhook inside **each** of them, from the bottom-left **Accounts** menu. Alerts from an organization without it never arrive. Use the same webhook name in each so `@webhook-<name>` is the same everywhere.
:::

---

### Grafana

Webhook-based connection for Grafana Cloud or self-hosted instances. No API key required.

#### Setup

1. Open the **Grafana** integration page in Aurora
2. Copy the webhook URL shown on screen
3. In Grafana: **Alerts & IRM** > **Notification Configuration** > **Contact points** > **New contact point**
   - Type: **Webhook**
   - URL: paste the Aurora webhook URL (`https://your-aurora-domain/grafana/alerts/webhook/{user_id}`)
4. Click **Test** to send a test notification
5. Aurora auto-connects when it receives the test webhook
6. Save the contact point, then add it to a notification policy

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

API token for Netdata Cloud.

1. Sign in at [app.netdata.cloud](https://app.netdata.cloud).
2. Profile picture → **User Settings** → **API Tokens**.
3. Create a token with `scope:all` and paste it into Aurora.
4. Space name on that page is an optional label.

---

### Splunk

API Token authentication for Splunk Cloud or Enterprise.

#### 1. Create a Minimal Role (Recommended)

Aurora needs the `search` capability, and **Indexes searched by default** set to `*` (or the indexes you want searched). The built-in **power** role already has that. A smaller role works if it has `search` and those default indexes.

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

Repeat those steps to connect another Splunk. Each instance keeps its own URL and token, and is identified by its host and port. Searches and the agent can target one instance, or query every instance the caller is allowed to use. When connector access is limited by role or team, each Splunk is granted on its own, the same way each AWS account is.

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

### Elastic Cloud

API-key authentication for Elasticsearch + Kibana. Works with **Elastic Cloud Hosted** (Cloud ID), **Elastic Cloud Serverless** (endpoint URLs) and **self-managed** clusters. Aurora is read-only against Elastic: it searches logs, reads Kibana alert documents and lists alerting rules, and it never writes.

#### 1. Create a read-only API key

**Option A (recommended): an admin creates a restricted key.**

1. In Kibana, as an admin, go to **Stack Management > API keys > Create API key**.
2. Name it `aurora`, turn on **Restrict privileges**, and paste the role descriptor below.
3. Create the key and copy the **Encoded** value. It is shown once. (An `id:api_key` pair is also accepted.)

**Option B: a dedicated read-only user creates its own key.** The built-in **Viewer** role has no cluster privileges, so a Viewer cannot create API keys on its own. Give the user two roles: **Viewer** plus a small custom role whose only cluster privilege is `manage_own_api_key` (Stack Management > Roles). Sign in as that user, create the key with *Restrict privileges* off, and it inherits the Viewer's read-only access to indices and Kibana.

Role descriptor for Option A:

```json
{
  "aurora_read": {
    "cluster": ["monitor"],
    "indices": [
      {
        "names": ["logs-*", "filebeat-*", "metrics-*", ".alerts-*"],
        "privileges": ["read", "view_index_metadata"]
      }
    ],
    "applications": [
      { "application": "kibana-.kibana", "privileges": ["read"], "resources": ["*"] }
    ]
  }
}
```

- `read` + `view_index_metadata` cover `_search`, `_count`, `_field_caps` and `_resolve/index`.
- `.alerts-*` is where Kibana stores alert documents; without it `elastic_get_alerts` returns a privilege error.
- The `kibana-.kibana` **application** privilege is what lets Aurora list Kibana rules. A bare index-only key gets a Kibana 403.
- Cluster `monitor` is optional but recommended. It enables `GET /` (cluster name and version shown in Aurora) and `_cat/indices` (document counts and index sizes). Without it Aurora validates the key through `_security/_authenticate` and still lists indices, just without stats or version info.

#### 2. Find your endpoint

| Deployment | What to enter |
|------------|---------------|
| Elastic Cloud **Hosted** | The **Cloud ID** (Elastic Cloud > Deployments > your deployment > Manage). Aurora derives the Elasticsearch and Kibana URLs from it. |
| Elastic Cloud **Serverless** | Endpoint URLs: `https://<project>.es.<region>.<csp>.elastic.cloud` and `https://<project>.kb.<region>.<csp>.elastic.cloud`. Serverless projects have no Cloud ID. |
| **Self-managed** | Your Elasticsearch URL (e.g. `https://es.internal:9200`) and, optionally, the Kibana URL. Set `ELASTIC_SSL_VERIFY=false` or point it at a CA bundle if you use private certificates. |

The Kibana URL is optional. Without it Aurora still searches logs and reads alert documents, but it cannot list Kibana rules or deep-link incidents into Kibana.

#### 3. Connect via Aurora UI

1. Navigate to **Connectors > Elastic Cloud**.
2. Choose **Cloud ID** or **Endpoint URLs** and fill in the endpoint.
3. Paste the API key (**Encoded** value).
4. Optionally set a default index pattern (default `logs-*`).
5. Click **Connect**. Aurora validates the key against Elasticsearch; Kibana is checked best-effort and never blocks the connection.

#### 4. Send Kibana alerts to Aurora (optional)

Aurora exposes a per-user webhook URL and a secret on the connector page. In Kibana:

1. **Stack Management > Connectors > Create connector > Webhook**. Name it `Aurora`, method `POST`, URL = the webhook URL from Aurora.
2. Authentication: **Basic**, username `aurora`, password = the webhook secret. (Alternatively add a header `X-Aurora-Webhook-Secret` with the secret as its value.)
3. Open the rule you want Aurora to see (**Observability > Alerts > Manage rules**) and add an action using the `Aurora` connector.
4. Set the action frequency to **On status changes** (otherwise Kibana re-sends the alert on every rule interval; Aurora dedupes these, but the extra traffic is unnecessary).
5. Paste the **action body** template shown in Aurora into the Body field. It maps Kibana's Mustache variables (`rule.name`, `alert.uuid`, `context.reason`, `context.viewInAppUrl`, ...) to the fields Aurora reads.
6. Add a second action row with the same connector and body and **Run when: Recovered**, so Aurora marks the alert recovered.

The Webhook connector is a **Gold+** feature on self-managed clusters (a Basic license returns a license error). Elastic Cloud subscriptions include it.

**Enable Alert RCA.** Webhook alerts are always stored and visible under **View Alerts**. With the **Enable Alert RCA** switch on the connector page on (the default), each new alert also creates an incident and starts an automatic investigation. Turn it off if your incidents already come from another source such as incident.io and you only want the alerts stored.

#### What Aurora Queries

| Purpose | Endpoint |
|---------|----------|
| Validate the connection | `GET /_security/_authenticate` (required), `GET /` (best-effort, needs `monitor`) |
| List indices, aliases, data streams | `GET /_resolve/index/{pattern}` (+ `GET /_cat/indices` when the key has `monitor`) |
| Field names and types | `GET /{index}/_field_caps` |
| Search logs | `POST /{index}/_search` (Query DSL, `size <= 500`) |
| ES\|QL queries | `POST /_query` |
| Kibana alerts | `POST /.alerts-*/_search` |
| Kibana rules | `GET {kibana}/api/alerting/rules/_find` (header `kbn-xsrf: true`) |

All calls send `Authorization: ApiKey <encoded>` to the endpoint you configured (Elastic Cloud is always HTTPS; a self-managed `http://` URL is used as entered). Aurora never calls `_cluster/health`, node or snapshot APIs, so it works unchanged on Serverless.

#### Troubleshooting

| Error | Solution |
|-------|----------|
| "Invalid API key" (401) | The key is wrong, expired or invalidated. Re-create it and paste the **Encoded** value. |
| "API key lacks privileges" (403) on search | Grant `read` and `view_index_metadata` on the index pattern (and on `.alerts-*` for alerts). |
| "API key lacks Kibana privileges" when listing rules | The key has no Kibana application privilege. Create it as a Viewer, or add the `kibana-.kibana` read privilege. |
| Index sizes / doc counts / version missing | Expected without cluster `monitor`. Add it to the role descriptor if you want them. |
| "unauthorized ... manage_own_api_key" when a Viewer creates a key | Viewer cannot create API keys. Use Option A, or add a custom role with `manage_own_api_key` to that user. |
| Kibana "license" error when saving the Webhook connector | Self-managed Basic license. Start a trial or upgrade; Elastic Cloud includes the connector. |
| Kibana shows "not verified" after connecting | Aurora could not reach the Kibana URL. Logs and alerts still work; check the URL or network path if you need rule listing. |
| Alerts arrive but no incident is created | Check that **Enable Alert RCA** on the connector page is on. Recovery events never create incidents. |
| Kibana action fails with `[401] UNAUTHORIZED` | The webhook secret changed (disconnecting deletes it; reconnecting generates a new one). Copy the current secret from Aurora into the Kibana connector. |

---

## Kubernetes

**Connectors → Kubernetes.** **Deploy Agent** is the default. **Upload Kubeconfig** is the other tab.

### Deploy Agent {#deploy-agent}

The agent runs in the cluster and dials out to Aurora. You need Helm 3 and permission to install a release. The page copies a command that installs chart `oci://gcr.io/sublime-flux-414616/helm/aurora-kubectl-agent` version `1.0.3` as release `aurora-kubectl-agent` in namespace `default`.

1. **Generate New Token.** Cluster name is required and should match the name your alerts use. Cluster context is optional. The token is shown once.
2. Point `kubectl` at that cluster (`kubectl config current-context`, then `kubectl config use-context` if needed).
3. Paste the **Install with Helm** command from the page. It sets `aurora.agentToken`, `aurora.backendUrl` (`NEXT_PUBLIC_BACKEND_URL`), and `aurora.wsEndpoint` (`NEXT_PUBLIC_WEBSOCKET_URL`). If either URL is missing, the page shows an error instead of a command.
4. The page switches to **Agent Connected** when the agent checks in.

```bash
kubectl get pods -n default -l app=aurora-kubectl-agent
kubectl logs -n default -l app=aurora-kubectl-agent --tail=50
```

### Upload Kubeconfig {#upload-kubeconfig}

Use this when the agent cannot be installed (restricted RBAC, no outbound from the cluster). Select one or more `.yaml` or `.yml` kubeconfig files. Aurora runs kubectl with those credentials. Remove a cluster later from the Kubernetes manage page.

---

## CI/CD & Development Tools

### CloudBees

Aurora integrates with [CloudBees](https://www.cloudbees.com/) to provide CI/CD deployment visibility, automated incident correlation, and root cause analysis across your entire Jenkins infrastructure.

#### What You Get

| Capability | Description |
|------------|-------------|
| **Deployment event tracking** | Build completions from any controller are correlated with alerts automatically |
| **RCA with build context** | Auto-generated RCA includes pipeline stages, build logs, test results, and changeset |
| **Cross-controller visibility** | Query deployments across all managed controllers via Operations Center |
| **Feature flag correlation** | Identify if a feature flag toggle caused an incident (Feature Management) |
| **Webhook-triggered RCA** | Automatically investigate failed deployments when they happen |

#### Connection Modes

Aurora supports three ways to connect CloudBees:

##### Single Controller

Direct connection to one CloudBees CI (Jenkins) controller. Best for teams with a single CI instance.

**You'll need:**
- Controller URL (e.g., `https://jenkins.company.com`)
- Username with API token permissions
- API Token (generated in your profile → Security → API Token)

##### Operations Center

Connect to your CloudBees Operations Center to automatically discover and manage all controllers from one connection. Best for enterprises with multiple Jenkins instances.

**You'll need:**
- Operations Center URL (e.g., `https://cjoc.company.com`)
- Username with OC-level API token permissions
- API Token (generated at the Operations Center level)

After connecting, Aurora will discover all managed controllers and can query builds across all of them during incident investigation.

##### Personal Access Token (PAT)

Platform-level authentication for organizations using CloudBees Platform tokens. PAT mode supports Operations Center discovery, cross-controller builds, and Feature Management — the same capabilities as OC mode. It does not support single-controller-only workflows.

**You'll need:**
- Platform URL (e.g., `https://your-org.cloudbees.io`)
- Personal Access Token (generated in Profile → Personal access tokens)

#### Setup

1. Navigate to **Connectors** in Aurora
2. Find **CloudBees** under the CI/CD category
3. Select your connection mode
4. Enter your credentials
5. Click **Connect**

For Operations Center mode, Aurora will automatically discover your managed controllers after connecting.

#### Webhook Setup (Deployment Tracking)

To track deployments in real-time, add a webhook to your Jenkinsfile post-build step. After connecting, Aurora provides:

- A unique webhook URL for your account
- Ready-to-use Jenkinsfile snippets (Basic, OpenTelemetry, and cURL variants)
- HMAC-SHA256 signing for webhook security

Copy the webhook URL and Jenkinsfile snippet from the connected view in Aurora.

#### RCA Actions

During incident investigation, Aurora's AI agent can use these CloudBees-specific actions:

##### Standard (all modes)

- `recent_deployments` — Recent build events tracked via webhook
- `build_detail` — Changeset, causes, and build metadata
- `pipeline_stages` — Stage-level breakdown with durations
- `stage_log` — Per-stage console output
- `build_logs` — Full build console output
- `test_results` — JUnit test report data
- `blue_ocean_run` — Blue Ocean pipeline run data
- `blue_ocean_steps` — Step-level pipeline data

##### Enterprise (Operations Center)

- `controller_list` — All managed controllers and their status
- `cross_controller_deployments` — Recent builds across ALL controllers
- `flag_changes` — Recent feature flag toggles (requires Feature Management)

#### Feature Flag Correlation (Optional)

If your organization uses CloudBees Feature Management, you can optionally provide a Feature Management API token during setup. This enables Aurora to check if a feature flag was toggled before an incident occurred — a common root cause that's otherwise hard to spot.

This is configured in the "Feature flag correlation" section during Operations Center setup.

#### Auto-trigger RCA

When enabled (default), Aurora automatically starts an investigation when a deployment webhook reports a failure. Configure this in the CloudBees connector settings under "RCA Settings."

---

### Bitbucket

Bitbucket Cloud supports two authentication methods: **API Token** (recommended — no `.env` setup) or **OAuth** (optional — requires registering a consumer).

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
- `read:webhook:bitbucket`, `write:webhook:bitbucket`, `delete:webhook:bitbucket` — only needed for [Incident Prevention](#incident-prevention-pre-merge-pr-review); lets Aurora create and verify the change-gating webhook for you, and remove it when you disable the feature

##### 2. Connect via Aurora UI

1. Navigate to **Connectors** > **Bitbucket**
2. Enter your Atlassian account **email** and the **API token**
3. Click **Connect**

#### Option B: OAuth (optional)

Use OAuth if you prefer a per-user consent flow. The OAuth tab is always on the Bitbucket page. Connect works only when `BB_OAUTH_CLIENT_ID` and `BB_OAUTH_CLIENT_SECRET` are set. Without them the tab is still there, and Connect returns "Bitbucket OAuth is not available. Use an API token instead."

##### 1. Create OAuth Consumer

1. Go to **Bitbucket workspace settings** > **OAuth consumers** > **Add consumer**
   - Name: `Aurora`
   - Callback URL: `{NEXT_PUBLIC_BACKEND_URL}/bitbucket/callback` (e.g. `https://your-aurora-domain/bitbucket/callback`)
   - Permissions: **Account** (Read), **Projects** (Read), **Repositories** (Read & Write), **Pull requests** (Read & Write), **Issues** (Read & Write), **Pipelines** (Read & Write), **Webhooks** (Read & Write)
2. Copy the **Key** and **Secret**

:::note
**Webhooks** is only required for [Incident Prevention](#incident-prevention-pre-merge-pr-review). Existing consumers keep working without it; add the permission and re-authorize to let Aurora create and verify change-gating webhooks for you.
:::

##### 2. Configure Environment

```bash
BB_OAUTH_CLIENT_ID=your-bitbucket-key
BB_OAUTH_CLIENT_SECRET=your-bitbucket-secret
```

Restart Aurora after setting these, then connect via **Connectors** > **Bitbucket** > **OAuth**.

#### Incident Prevention (pre-merge PR review)

Aurora can review pull requests that target the default branch of an enrolled Bitbucket Cloud repository and post an advisory review comment before merge — the same feature as GitHub Incident Prevention. The Incident Prevention review never approves (or blocks) a PR: approval stays with human reviewers.

Requirements: a connected Bitbucket account (either auth method above), `NEXT_PUBLIC_ENABLE_INCIDENT_PREVENTION=true` (the default) on **both** the server and the Celery worker, and Aurora's API reachable from the public internet (Bitbucket must be able to deliver webhooks to it).

##### Enable per repository

1. Navigate to **Connectors** > **Bitbucket** and connect at least one repository
2. Flip the **Incident Prevention** switch on a connected repository
3. Aurora responds with a **webhook URL** (`{API_URL}/bitbucket/webhook/{org_id}`) and a **secret**. It also tries to create the repo webhook for you via the API; if your credentials lack the webhook scopes, add it manually:
   - In Bitbucket: **Repository settings → Webhooks → Add webhook**
   - Title: `Aurora Incident Prevention`; URL: the webhook URL from Aurora; Secret: the secret from Aurora
   - Triggers: **Pull request → Created** and **Pull request → Updated** only (do not add approval/comment triggers — they would loop on Aurora's own activity)
4. Repeat the webhook setup for each repository you enable, using the **same URL and secret** — the secret is shared across your organization

##### How the webhook gets verified

The repository badge reads **Awaiting first delivery** until Aurora confirms the hook, then **Active**. There are two ways it gets confirmed:

- **Verify webhook** (immediate) — reads the repository's hooks via the API and looks for one pointing at Aurora's URL with both pull request triggers. Requires the `read:webhook:bitbucket` scope on the connected credentials. Without it you get *Verification pending* rather than a failure, because not being able to look is not proof of absence.
- **First delivery** (automatic) — any pull request event that passes signature validation confirms the hook on its own. This is the stronger check: it proves the hook exists, is enabled, points at the right URL **and** carries the right secret, none of which the API listing can tell you.

Bitbucket sends **no test event** when you save a webhook (unlike GitHub's `ping`), so a manually added hook stays *Awaiting first delivery* until a real PR event arrives. Open or update a pull request to confirm it.

:::note
Verification is recorded against Aurora's public URL at the time. If that URL changes (a new dev tunnel, a domain migration), the badge turns red with **Webhook URL changed** — the hook still points at the old address and can no longer reach Aurora. Click the badge for the current URL and secret, then update the existing hook in Bitbucket. Aurora cannot rewrite a hook it did not create.
:::

Notes and limits:

- Reviews run only for PRs targeting the repository's **default branch**; drafts are skipped. A draft marked ready **without new commits** is reviewed on the next push.
- Reviews post as the **connected Bitbucket account** by default. Both SAFE and RISKY verdicts post as a review comment only — Aurora never approves the PR.
- Approvals posted by older Aurora versions (which approved on SAFE verdicts) are **not retracted automatically**: Bitbucket approvals are account-level, so unapproving could strip an approval the connected user made themselves — remove legacy Aurora approvals by hand on any still-open PRs. (On GitHub, Aurora dismisses its own legacy approvals the next time it reviews the PR.)
- Disabling the toggle (or deselecting/disconnecting the repo) deletes the webhook when Aurora created it via API; manually created hooks must be removed manually.

##### Optional: dedicated bot account

To have reviews appear as "Aurora" instead of a team member, create a separate Atlassian account:

1. Create the Atlassian user (e.g. `aurora-bot@yourcompany.com`), invite it to the workspace/repos with **pull request write** access
2. Create a scoped API token for it with at least `read:pullrequest:bitbucket` and `write:pullrequest:bitbucket`
3. Store it in the secrets backend as a system secret (no env vars involved):

```bash
# Vault
vault kv put aurora/system/bitbucket-bot/credentials \
  value='{"email": "aurora-bot@yourcompany.com", "api_token": "YOUR_SCOPED_TOKEN"}'

# AWS Secrets Manager
aws secretsmanager create-secret --name aurora/system/bitbucket-bot/credentials \
  --secret-string '{"email": "aurora-bot@yourcompany.com", "api_token": "YOUR_SCOPED_TOKEN"}' \
  --region "$AWS_SM_REGION"
```

Only **posting** (review comments) uses the bot; investigation reads always use the org's connected account. Bot credentials are never exposed to agent tools.

#### Troubleshooting

| Error | Solution |
|-------|----------|
| "Authentication failed... you are using a classic API token (not supported)" | Create a **scoped** token at [id.atlassian.com](https://id.atlassian.com/manage-profile/security/api-tokens), not a classic one |
| "Missing required scopes: ..." | Recreate the API token with all the read+write scopes listed above |
| "Bitbucket OAuth is not available. Use an API token instead." | `BB_OAUTH_CLIENT_ID`/`BB_OAUTH_CLIENT_SECRET` are not set. Use the API token method, or configure the OAuth consumer |
| Incident Prevention toggle missing | `NEXT_PUBLIC_ENABLE_INCIDENT_PREVENTION` is `false`, or Bitbucket is not connected |
| "Verification pending" when clicking Verify webhook | The token lacks `read:webhook:bitbucket`. Not a failure — open or update a PR and the first delivery confirms the hook automatically |
| Badge stuck on "Awaiting first delivery" | No PR event has arrived yet. Bitbucket sends no test event on save, so push to a PR. If it still does not flip, check **Repository settings → Webhooks → View requests** for the delivery attempts and their response codes |
| Badge reads "Webhook URL changed" (red) | Aurora's public URL changed since the hook was verified, so the hook now points at an unreachable address. Click the badge for the current URL and update the existing hook in Bitbucket |
| PRs never get reviewed | Check the webhook in Bitbucket shows recent 2xx deliveries; confirm the PR targets the default branch and is not a draft; confirm the same org secret was pasted into the repo hook |

---

## More connectors {#more-connectors}

Setup below matches the Connectors page.

### Scaleway {#scaleway}

1. [Scaleway Console](https://console.scaleway.com) → profile → **IAM & API keys** → **API keys**.
2. **Generate API key**. Bearer: **Myself (IAM user)**. Copy the access key and the secret key.
3. In Aurora, paste both. Default project ID is optional.

### Cloudflare {#cloudflare}

An account administrator creates the token.

1. [Cloudflare dashboard](https://dash.cloudflare.com) → **Manage Account** → **Account API Tokens**.
2. Next to **Read all resources**, click **Use template**, then **Create Token**.
3. Confirm **Account — API Tokens — Read** is selected, and pick the zones under **Zone Resources**.
4. Optional writes, for actions during an incident: Zone — DNS — Edit, Zone — Cache Purge — Purge, Zone — Zone WAF — Edit, Account — Load Balancing: Account Load Balancers — Edit.
5. Paste the token into Aurora. Creating the token requires **Super Administrator**. Read-only is enough to start.

### Fly.io {#flyio}

1. Fly.io dashboard → **Account** → [Access Tokens](https://fly.io/tokens). Create a token for the organization.
2. In Aurora, enter the org slug (`personal` for a personal account, otherwise the org name) and the token.

Aurora detects read-only versus full access. A read-only token can monitor. Restart, stop, and start need a full org token.

### Tailscale {#tailscale}

1. Tailscale admin console → **Settings** → **OAuth clients** → **Generate OAuth client**.
2. Scopes: `devices:read`, `devices:write`, `keys:read`, `keys:write`, `dns:read`, `acl:read`.
3. Paste the client ID and client secret into Aurora. The secret is shown once.

SSH to machines on the tailnet is a separate page, **SSH Setup**, after the tailnet is connected. That page gives a public key and a command that appends it to `authorized_keys`.

### SSH {#ssh}

**Connectors → Instances SSH Access.** Aurora shows a public key and a command. SSH to each host and run it so the key lands in `authorized_keys`. Ask mode allows remote reads (`journalctl`, `kubectl get`, `uptime`) when SSH is on the allow list. Remote writes stay blocked there.

### Dynatrace {#dynatrace}

1. Environment URL. SaaS looks like `https://abc12345.live.dynatrace.com`. Managed looks like `https://your-domain.com/e/environment-id`.
2. **Settings → Access tokens** → **Generate new token**.
3. Scopes: `problems.read`, `entities.read`, `metrics.read`, `logs.read`, `events.read`. A scope is missing from the picker when that feature is not in the plan.
4. Paste the URL and token into Aurora. Problem notifications are a webhook configured after connect.

### Coroot {#coroot}

Not an API key. Aurora signs in to the Coroot HTTP API with an email and password (a session cookie).

1. The Coroot URL must be reachable from this Aurora deployment.
2. Use an account that can see the projects you want monitored. A dedicated account is the better choice.
3. Enter the URL, email, and password.

Aurora then reads metrics (PromQL), logs, traces, incidents, the service map, profiling, deployments, costs, and risks.

### ThousandEyes {#thousandeyes}

1. [app.thousandeyes.com](https://app.thousandeyes.com) → **Manage** → **Account Settings** → **Users and Roles**.
2. Under **User API Tokens**, **Create** next to **OAuth Bearer Token**. The token is shown once.
3. Paste it into Aurora. Account Group ID is optional.

That token can read tests and results, alerts, agents, dashboards, Internet Insights, path visualization, BGP, and DNS/VoIP data.

### CloudWatch {#cloudwatch}

Turn on **CloudWatch Alerts** on the AWS connector page. Aurora shows an HTTPS URL.

1. SNS → **Topics** → create a Standard topic.
2. **Create subscription**. Protocol **HTTPS**, endpoint is that URL. Aurora confirms the subscription when AWS sends `SubscriptionConfirmation`.
3. On each alarm, under **Notification**, send **In alarm**, **OK**, and **Insufficient data** to that topic.

An alarm that enters ALARM starts an RCA.

### incident.io {#incidentio}

**Settings → API keys.** The key needs:

- **View all incident data**, including private incidents
- **Create incidents**
- **View data**, so Aurora can read alert priorities. Without it, RCA still runs on every alert
- **Edit incidents**, so Aurora can post the root cause as an incident update
- **Create and manage on-call resources** (incident.io spells the checkbox "ressources"), so Aurora can post the root cause as a note on an alert that only escalated. Without it, incident updates still post

After connect, create the webhook in incident.io and paste the signing secret (`whsec_…`) into Aurora. Without that secret, Aurora accepts any request to the webhook URL.

### BigPanda {#bigpanda}

1. BigPanda → **Settings → API Keys**. Copy the User API Key, or create one.
2. Paste it into Aurora.
3. After connect, copy the webhook URL. In BigPanda: **Integrations → Outbound Integrations → Webhooks → New Integration → Alerts Webhook**.
4. Paste the URL. Sign requests with header `X-Aurora-Signature`, HMAC-SHA256 of the body using the webhook secret. Choose which environments and incident types notify Aurora, then save.

### GitLab {#gitlab}

Group access token, not a personal one.

1. **Group → Settings → Access Tokens**.
2. Full use (investigation plus fix branches, commits, and merge requests): role **Maintainer**, scope `api`.
3. Read-only investigation: role **Reporter**, scope `read_api`. The agent cannot open a fix.
4. In Aurora, set the instance URL (`https://gitlab.com` or your host) and paste the token (`glpat-…`).

Projects in that group and its subgroups are connected. There is no per-project picker.

### Jenkins {#jenkins}

Read-only. Aurora does not need a Jenkins admin token.

1. Sign in as the user Aurora should use.
2. Username (top right) → **Configure → API Token → Add new Token**. Name it and generate it. Copy it immediately.
3. In Aurora, enter the Jenkins URL (no trailing slash), the username, and the token.

### Notion {#notion}

This is an integration token, not Notion OAuth.

1. [notion.so/my-integrations](https://www.notion.so/my-integrations) → **New connection**. Name it Aurora. Authentication method: **Access token**.
2. On **Configuration**, copy the integration token. Capabilities: **Read**, **Update**, and **Insert content**.
3. Share each page or database with the connection (**Connections → Add connections**), or use the connection's **Content access** tab.
4. Paste the token into Aurora.

### SharePoint {#sharepoint}

The page is **Connect with Microsoft**. There is no token to paste. Aurora then reads documents and site pages for the signed-in user.

## Anything Else: Custom MCP Servers

If a system you need has no connector above, you can register your own [MCP](https://modelcontextprotocol.io/) server and Aurora will discover and use its tools. See [Custom MCP Servers](./custom-mcp-servers.md).

---

## Credential Storage
All connector credentials are stored securely in HashiCorp Vault:

- Credentials are encrypted at rest
- Database stores only Vault path references
- Credentials resolved at runtime
- Never logged or exposed in responses

See [Vault Configuration](/docs/configuration/vault) for details.
