# GitHub App Setup

Operator one-time setup for the Aurora GitHub App — the only auth path
to GitHub since the OAuth flow was removed in `feat/github-app-only`.
After this is done, end users see an "Install GitHub App" button on
the GitHub connector page.

This guide assumes you have shell access to the host running Aurora and to
the Vault container. For a scripted alternative, see
`server/scripts/register_github_app.py` which drives GitHub's App Manifest
flow and writes credentials directly into `.env` + Vault.

---

## What the App gives Aurora

Stable installation-scoped identity per repository, fine-grained read
permissions on Contents/Issues/Pull-requests/Actions/Deployments/Checks,
real-time webhooks for incident-correlation events (`pull_request`,
`deployment`, `workflow_run`, `check_run`, etc.), and a 5,000-req/hr
rate-limit budget that scales with installation count rather than user
count. For the capability matrix see the connector [README](./README.md).

---

## Prerequisites

| Requirement | Where to get it |
|---|---|
| Aurora running locally or in your environment | `make dev` or `make prod-prebuilt` |
| Vault container running and unsealed | Part of `make dev` / `make prod-*` stack |
| `VAULT_TOKEN` in `.env` | `docker logs vault-init 2>&1 \| grep "Root Token:"` on first run |
| Public-facing base URL for callbacks and webhooks | Pick one: `https://aurora.example.com`, an ngrok tunnel for local dev (`https://<subdomain>.ngrok.io`), etc. |
| `openssl` on the host | Pre-installed on macOS / most Linux distros |

> Use a real reachable URL for `<BASE_URL>` in the steps below. GitHub's
> webhook delivery requires a publicly resolvable hostname; `localhost`
> works only for the install callback when you run Aurora and the browser
> on the same machine.

---

## Step 1: Create the GitHub App

1. Open the GitHub App creation page:
   - **Personal account**: <https://github.com/settings/apps/new>
   - **Organization**: `https://github.com/organizations/<org>/settings/apps/new`
2. Fill in the form:

   | Field | Value |
   |---|---|
   | **GitHub App name** | `aurora-<your-org-or-handle>` (must be globally unique) |
   | **Description** | `Aurora — AI-powered incident investigation. Reads repo metadata, PRs, issues, and CI status to correlate code changes with production incidents.` |
   | **Homepage URL** | `<BASE_URL>` |
   | **Callback URL** | `<BASE_URL>/github/app/install/callback` |
   | **Setup URL** | `<BASE_URL>/github/app/install/callback` (same as callback — used for post-install redirect) |
   | **Redirect on update** | Leave checked |
   | **Webhook Active** | Checked |
   | **Webhook URL** | `<BASE_URL>/github/webhook` |
   | **Webhook secret** | Generated in Step 2 below |

3. Do **not** click Create yet — fill in permissions and events first
   (Steps 3 and 4).

> The GitHub UI evolves over time. If a label here does not match exactly,
> the field is still findable by description (e.g. "Setup URL" sometimes
> appears under "Identifying and authorizing users").

---

## Step 2: Generate the webhook secret

Generate a 256-bit hex secret on your operator machine:

```bash
openssl rand -hex 32
```

Copy the output and paste it into the **Webhook secret** field in the
GitHub App form. Keep this value handy — you will write it to Vault in
Step 7.

> Treat this secret like an API key. Anyone with it can forge webhooks
> that Aurora will accept as authentic.

---

## Step 3: Permissions checklist

Scroll to **Repository permissions** and **Account permissions** and set
these exact values. Leave every other permission as **No access**.

### Repository permissions

| Permission | Access | Why Aurora needs it |
|---|---|---|
| **Contents** | Read and write | Read code/metadata for RCA. Write is reserved for future auto-fix PRs and is **not used by current code** (read-only enforced in the auth router). |
| **Metadata** | Read-only | Mandatory baseline for any repo-scoped permission. |
| **Issues** | Read-only | Correlate incidents with open issues. |
| **Pull requests** | Read-only | RCA: which PR shipped the bad change. |
| **Actions** | Read-only | Inspect workflow runs around the incident window. |
| **Deployments** | Read-only | Build the deploy timeline for RCA. |
| **Commit statuses** | Read-only | Pull CI / commit status signals. |
| **Webhooks** | Read and write | Future flexibility for repo-level custom webhooks. |

### Account permissions

| Permission | Access | Why Aurora needs it |
|---|---|---|
| **Email addresses** | Read-only | Optional user attribution when linking installs to Aurora users. |

> **9 permissions total.** If your count differs, recheck before continuing.

---

## Step 4: Subscribe to events

Scroll to **Subscribe to events** and check these boxes:

- [ ] `installation`
- [ ] `installation_repositories`
- [ ] `pull_request`
- [ ] `issues`
- [ ] `deployment`
- [ ] `deployment_status`
- [ ] `workflow_run`
- [ ] `check_run`
- [ ] `check_suite`

> **9 events total.** Do **not** subscribe to `push` or `release` — Aurora
> does not handle them and they would generate substantial noise.

---

## Step 5: Installation scope

Under **Where can this GitHub App be installed?**, select **Any account**.

This makes the App public so any user or org can install it from a shared
install URL. Choose **Only on this account** if you want to lock the App to
a single owner (e.g. your company GitHub org).

Click **Create GitHub App**.

---

## Step 6: Generate and download the private key

After creation, you land on the App's settings page.

1. Note the numeric **App ID** (e.g. `123456`) and the **Client ID**
   (e.g. `Iv1.<placeholder>`) at the top of the page. You will use both in
   Step 8.
2. Note the **App slug** from the URL: the page lives at
   `https://github.com/settings/apps/<slug>`. You will use the slug in
   Step 8 for `NEXT_PUBLIC_GITHUB_APP_SLUG`.
3. Scroll to **Private keys** > click **Generate a private key**.
4. The browser downloads a file like
   `aurora-<your-org>.<date>.private-key.pem`. Move it somewhere safe:

   ```bash
   mv ~/Downloads/aurora-<your-org>.*.private-key.pem /tmp/github-app.pem
   chmod 600 /tmp/github-app.pem
   ```

> The PEM file is the App's private signing key. Anyone with it can mint
> JWTs that GitHub will accept as the App. Never check it in, never log
> it, never paste it into chat.

---

## Step 7: Write secrets to Vault

From the host running Aurora, write the private key and webhook secret to
Vault. The Vault container name in the default Compose stack is
`aurora-vault-1`; adjust if you renamed services.

The two canonical commands are:

```bash
vault kv put aurora/system/github-app/private-key pem=@/path/to/key.pem
vault kv put aurora/system/github-app/webhook-secret secret=<the-secret>
```

Run them through `docker exec` so they hit the Vault container:

```bash
docker exec -i aurora-vault-1 vault kv put aurora/system/github-app/private-key \
  pem=@/tmp/github-app.pem
```

```bash
docker exec aurora-vault-1 vault kv put aurora/system/github-app/webhook-secret \
  secret='<paste-the-openssl-rand-output-from-step-2>'
```

Verify both writes:

```bash
docker exec aurora-vault-1 vault kv get -field=pem aurora/system/github-app/private-key | head -1
# Expect: a PEM private-key header line (BEGIN ... PRIVATE KEY)

docker exec aurora-vault-1 vault kv get -field=secret aurora/system/github-app/webhook-secret | wc -c
# Expect: 64 (hex chars) + trailing newline = 65
```

After writing the PEM, delete the on-disk copy:

```bash
shred -u /tmp/github-app.pem  # GNU coreutils
# or, on macOS:
rm -P /tmp/github-app.pem
```

> If your Vault path layout differs (custom `VAULT_KV_MOUNT` or
> `VAULT_KV_BASE_PATH`), adjust the path. The defaults in `.env.example`
> use mount `aurora` so the full KV v2 path resolves to
> `kv/data/aurora/system/github-app/...`.

---

## Step 8: Configure Aurora env vars

Edit your `.env` file at the project root and add (or fill in if Task 3
already added empty defaults):

```bash
# GitHub App (alternative to OAuth, see server/connectors/github_connector/SETUP_GITHUB_APP.md)
GITHUB_APP_ID=<numeric-app-id-from-step-6>
GITHUB_APP_CLIENT_ID=<client-id-from-step-6>
GITHUB_APP_WEBHOOK_URL=<BASE_URL>/github/webhook
GITHUB_APP_SETUP_URL=<BASE_URL>/github/app/install/callback
GITHUB_APP_WEBHOOK_SECRET=  # leave empty if you stored it in Vault (recommended)
```

Edit `client/.env` (or your client-level env file) and add:

```bash
NEXT_PUBLIC_GITHUB_APP_SLUG=<slug-from-step-6>
```

The frontend uses this slug to build the install URL
(`https://github.com/apps/<slug>/installations/new`).

---

## Step 9: Restart Aurora

The server reads env vars at boot and caches the Vault PEM in memory for
process lifetime, so a restart is required.

```bash
make rebuild-server
```

Or, for production-style images:

```bash
make down
make prod-prebuilt
```

Tail logs and confirm App mode is enabled:

```bash
docker logs aurora-server-1 2>&1 | grep -i 'github.*app'
# Expect a line like: "GitHub App configured: app_id=<id>, slug=<slug>"
```

If you see a warning like
`GitHub App config incomplete: missing ['GITHUB_APP_ID', ...]`, recheck
Step 8 — Aurora falls back to OAuth-only mode (degraded mode) when env
vars or Vault paths are missing.

---

## Step 10: Verify the install URL endpoint

Hit the install URL endpoint as an authenticated Aurora user with
`connectors:write` permission (substitute your session bearer token):

```bash
curl -s -H 'Authorization: Bearer <your-aurora-session-token>' \
     http://localhost:5080/github/app/install \
     | jq .install_url
```

Expected output (a real, clickable URL):

```text
"https://github.com/apps/<slug>/installations/new?state=<your-user-id>"
```

If you get `404` or `503` instead, App mode is not enabled — go back to
Step 9 logs.

---

## Step 11: Install on a sandbox repo (end-to-end)

1. As a normal user in Aurora, navigate to **Connectors > GitHub**.
2. Click **Install GitHub App**. You are redirected to GitHub's install
   page.
3. Pick the account (your personal user, or an org), choose **Only select
   repositories**, pick one sandbox repo, and click **Install**.
4. GitHub redirects back to `<BASE_URL>/github/app/install/callback`.
   Aurora verifies the installation with the GitHub API, persists it,
   links it to your user, and renders the success template.
5. Back on the **Connectors > GitHub** page, the new installation appears
   with the selected repos and a **Connected** status.

To verify the webhook half of the flow, open or close a pull request on
the sandbox repo. Within a few seconds you should see a delivery in
GitHub's **Advanced > Recent Deliveries** tab on the App settings page,
and a corresponding row in Aurora's webhook delivery log:

```bash
docker exec aurora-postgres-1 psql -U aurora -d aurora_db \
  -c "SELECT delivery_id, event, received_at FROM webhook_deliveries ORDER BY received_at DESC LIMIT 5;"
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `curl /github/app/install` returns `503` or `404` | App mode disabled | Recheck Step 8 env vars and Step 9 startup logs for `GitHub App config incomplete: missing [...]` |
| Install callback shows `error` template with "installation not found" | Wrong `installation_id` or App not yet propagated | Wait ~10s after install and retry; Aurora calls `GET /app/installations/{id}` and GitHub may need a moment |
| Webhook deliveries land in GitHub's UI but Aurora returns `401` | Webhook secret mismatch | Re-run Step 2, rewrite the secret to Vault (Step 7), restart server (Step 9) |
| Webhook delivery returns `403` | Installation suspended on the account | The account owner re-enables it from the GitHub App settings page |
| `GitHubAppConfigError: aurora/system/github-app/private-key not found` in logs | Vault path missing or empty | Re-run Step 7's `vault kv put` for the PEM |
| `GET /github/app/install` returns valid URL but install page 404s | `NEXT_PUBLIC_GITHUB_APP_SLUG` does not match the App slug | Recheck the slug in the App settings URL (Step 6) and update `client/.env` |
| Installation succeeds but shows zero repos | User picked **All repositories** but App needs reinstall to refresh repo list | Re-trigger the install flow from Aurora; the `installation_repositories` webhook will sync the list |
| `pending org admin approval` status shown on the connectors page | Non-admin user installed the App on an org that requires admin approval | An org owner approves the install at `https://github.com/organizations/<org>/settings/installations` |
| `pending permissions update` status | Aurora App permissions changed since this install | Org owner reviews and accepts the new permissions on the same settings page |

---

## On-Premises Deployment

When Aurora runs on customer infrastructure (private cloud, on-prem
datacenter, customer-managed VPC) the operator owns both the App and the
ingress. There is no shared "Aurora SaaS" GitHub App in this case — each
customer creates their own App in their own GitHub org and points it at
their own Aurora hostname.

### Requirements

| Requirement | Why |
|---|---|
| Public hostname Aurora can be reached at | GitHub.com posts webhooks from public IPs; tunnel-only setups break under load |
| Valid TLS cert (Let's Encrypt, internal CA chained to a public root, or paid CA) | GitHub refuses webhook delivery to invalid certs |
| Outbound HTTPS to `api.github.com` | Aurora calls GitHub for installation token mint, repo metadata, etc. |
| GitHub App created in **the customer's** GitHub org | App secrets live with the customer, not Arvo |

### Reverse-proxy sketch (nginx)

Terminate TLS at the edge, forward `/github/webhook`, `/github/app/*`,
and the rest of Aurora to `aurora-server:5080`.

```nginx
server {
    listen 443 ssl http2;
    server_name aurora.example.com;

    ssl_certificate     /etc/letsencrypt/live/aurora.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/aurora.example.com/privkey.pem;

    # GitHub webhooks can deliver bursts; raise body size to absorb large
    # workflow_run payloads without truncation.
    client_max_body_size 25m;

    location / {
        proxy_pass http://aurora-server:5080;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 300s;
    }
}
```

A traefik-style label config achieves the same; the only requirements
are TLS termination and Host-header preservation.

### Env vars to point at the customer's hostname

In the customer's `.env` (kept on their host, never committed):

```bash
GITHUB_APP_SETUP_URL=https://aurora.example.com/github/app/install/callback
GITHUB_APP_WEBHOOK_URL=https://aurora.example.com/github/webhook
NEXT_PUBLIC_BACKEND_URL=https://aurora.example.com
```

These three must match the URLs configured inside the customer's GitHub
App settings (Step 1). Mismatch causes silent webhook drops or "GitHub
App install URL is missing the required state parameter" toasts on the
client.

### Secret hygiene for prod

| Secret | Where in dev | Where in prod |
|---|---|---|
| `GITHUB_APP_WEBHOOK_SECRET` | `.env` | Vault: `aurora/system/github-app/webhook-secret` (field `value`); env var becomes a fallback only |
| `GITHUB_APP_PRIVATE_KEY` (PEM) | Vault: `aurora/system/github-app/private-key` | Same — Vault is canonical in both envs; never bake into images |
| `INTERNAL_API_SECRET` | Empty in dev | Required, rotated; the server refuses to start without it when `AURORA_ENV != dev` |

Set `AURORA_ENV=production` so the runtime startup check enforces the
above. Empty `INTERNAL_API_SECRET` is permitted only in dev.

### Per-environment Apps

Create separate `aurora-<customer>-prod`, `aurora-<customer>-staging`,
`aurora-<customer>-dev` Apps. Aurora reads `GITHUB_APP_*` env vars per
deployment, so each env gets its own App keys. This stops a stray
callback URL change in dev from breaking prod webhook delivery.

### When public ingress is not possible

Some customers cannot expose any port to the internet. Today the only
supported workaround is a self-hosted webhook relay (smee.io-style
tunnel that the customer operates), which is **not** part of the
upstream Aurora deployment. If you need this, the OAuth-mode flag
(`GITHUB_AUTH_MODE=oauth`, see below) gives you a path that does **not**
need GitHub.com-to-Aurora reachability — it relies on user-level OAuth
tokens and polling instead of webhook push.

### GitHub Enterprise Server (GHES)

GHES customers run their own GitHub instance inside their network, so
both the App and Aurora can stay internal — no public hostname needed.
This is the cleanest on-prem story when the customer already runs GHES.

**Status:** not yet supported. Aurora's GitHub-API call sites still
hardcode `https://api.github.com`. To enable GHES, every hardcoded
`api.github.com` and `github.com` path needs to be routed through a
configurable base URL (`GITHUB_API_URL`, `GITHUB_BASE_URL`). See
[GHES_AUDIT.md](./GHES_AUDIT.md) for the file-by-file punch list. Until
that work lands, GHES customers must use [Path A above](#requirements)
with a public Aurora hostname that talks to the public GHES URL.

---

## See also

- [README.md](./README.md) — Connector overview
- GitHub docs: [Creating a GitHub App](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app)
- GitHub docs: [Authenticating as a GitHub App installation](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation)
