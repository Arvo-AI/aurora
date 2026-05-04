# GitHub Connector

Aurora supports **two independent authentication paths** for GitHub. Both
remain available indefinitely; pick whichever fits your workflow.

---

## Two ways to connect: OAuth (existing) vs GitHub App (new, recommended)

| Dimension | OAuth App | GitHub App |
|---|---|---|
| **Auth model** | Per-user OAuth token (acts as the user) | Installation token (acts as the App) |
| **Rate limit** | 5,000 req/hr per user, shared across all OAuth tokens for that user | 5,000 req/hr per installation, isolated from user limits; scales with installed repo count |
| **Permissions granularity** | Coarse (`repo`, `user`, `read:org`) — all-or-nothing per scope | Fine-grained per-resource (Contents R, Issues R, Pull requests R, Actions R, Deployments R, Commit statuses R, Metadata R, Webhooks R+W, Email addresses R) |
| **Webhooks** | Not supported | Real-time delivery of `installation`, `installation_repositories`, `pull_request`, `issues`, `deployment`, `deployment_status`, `workflow_run`, `check_run`, `check_suite` |
| **Who installs** | End-user clicks "Connect" — Aurora gets read access on every repo the user can see | Org admin (or user, on personal account) installs once and selects exactly which repos to grant |
| **Org UX** | Each org member must reconnect individually | Single install per org; new members inherit access |
| **Survives user departure** | Breaks when the OAuth-connecting user leaves the org | Survives — install is owned by the account, not by an individual user |
| **Attribution** | Commits / comments by Aurora appear as the connecting user | Commits / comments appear as `aurora-<slug>[bot]` |
| **Webhook secret rotation** | N/A | Manual (out of scope for this release) |

**Recommendation**: Use the **GitHub App** path for new installs. The OAuth
path remains supported for existing users and for environments where the
operator has not yet configured an Aurora GitHub App.

When a user has connected via **both** paths for the same repo, Aurora
automatically prefers the GitHub App installation token (better rate limits,
installation-scoped audit trail).

---

## Which guide do I read?

| If you want to... | Read |
|---|---|
| Set up the new GitHub App path (operator one-time setup) | [SETUP_GITHUB_APP.md](./SETUP_GITHUB_APP.md) |
| Migrate an existing OAuth-connected user / org to the App | [docs/oss/GITHUB_APP_MIGRATION.md](../../../docs/oss/GITHUB_APP_MIGRATION.md) |
| Use the legacy OAuth App flow only | Continue below |

---

# OAuth App Setup (legacy)

OAuth App authentication for GitHub.

## Setup

### 1. Create OAuth App

1. Go to [GitHub > Settings > Developer settings > OAuth Apps](https://github.com/settings/developers)
2. Click **New OAuth App**
   - Application name: `Aurora`
   - Homepage URL: `http://localhost:3000`
   - Authorization callback URL: `http://localhost:5000/github/callback`
3. Copy the **Client ID**
4. Click **Generate a new client secret** and copy it

### 2. Configure `.env`

```bash
GH_OAUTH_CLIENT_ID=your-github-client-id
GH_OAUTH_CLIENT_SECRET=your-github-client-secret
```

## Troubleshooting

**"No authorization code provided"** — Verify callback URL in GitHub OAuth App matches exactly: `http://localhost:5000/github/callback`
