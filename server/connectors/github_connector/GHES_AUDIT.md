# GitHub Enterprise Server (GHES) Audit

This is the file-by-file punch list of GitHub.com hardcodes that need to be
routed through configurable base URLs before Aurora supports GHES.

## Proposed config surface

Two env vars cover every case below:

| Var | Default | Purpose |
|---|---|---|
| `GH_API_BASE_URL` | `https://api.github.com` | REST API host. On GHES this becomes `https://<ghes-host>/api/v3`. |
| `GH_BASE_URL` | `https://github.com` | UI host (App install pages, repo browser links, avatars). On GHES this becomes `https://<ghes-host>`. |

Aurora already passes a `GITHUB_API_URL` value through to MCP tool credentials
(see `chat/backend/agent/tools/mcp_tools.py:495,773`) but it is not consumed
by the rest of the backend. The migration is to introduce both env vars at a
single helper module (e.g. `utils/github/endpoints.py`) and rewrite every
call site to read from there.

## Backend hard blockers (must change)

| File | Line | Current | Fix |
|---|---|---|---|
| `utils/auth/github_app_token.py` | 204 | `https://api.github.com/app/installations/{id}/access_tokens` | `f"{GH_API_BASE_URL}/app/installations/{id}/access_tokens"` |
| `routes/github/github_user_repos.py` | 154 | `https://api.github.com/installation/repositories` | `f"{GH_API_BASE_URL}/installation/repositories"` |
| `routes/github/github_user_repos.py` | 311 | `https://api.github.com/repos/{repo}/branches` | `f"{GH_API_BASE_URL}/repos/{repo}/branches"` |
| `routes/github/github_repo_metadata.py` | 30 | `https://api.github.com/repos/{owner}/{repo}/readme` | route through `GH_API_BASE_URL` |
| `routes/github/github_repo_metadata.py` | 47 | `https://api.github.com/repos/{owner}/{repo}/contents` | route through `GH_API_BASE_URL` |
| `routes/github/github_app.py` | 209 | `https://api.github.com/app/installations/{id}` | route through `GH_API_BASE_URL` |
| `scripts/register_github_app.py` | 245 | `https://api.github.com/app-manifests/{code}/conversions` | route through `GH_API_BASE_URL` |

## Backend UI URLs (need GH_BASE_URL)

| File | Line | Current | Fix |
|---|---|---|---|
| `routes/github/github_app.py` | 153 | `https://github.com/apps/{slug}/installations/new?...` | `f"{GH_BASE_URL}/apps/{slug}/installations/new?..."` |
| `scripts/register_github_app.py` | 372 | `https://github.com/organizations/{org}/settings/apps/new` | route through `GH_BASE_URL` |
| `scripts/register_github_app.py` | 374 | `https://github.com/settings/apps/new` | route through `GH_BASE_URL` |
| `scripts/register_github_app.py` | 480 | `https://github.com/apps/{slug}` (display only) | route through `GH_BASE_URL` |

## Frontend UI URLs (need a server-fed `GH_BASE_URL`)

| File | Line | Current | Notes |
|---|---|---|---|
| `client/src/components/github-provider-integration.tsx` | 375-376 | `https://github.com/.../settings/installations/{id}` | Install management URL. Read base URL from `/api/proxy/github/auth-config` (server-fed; client must not trust env). |
| `client/src/components/github-provider-integration.tsx` | 508 | `https://github.com/{login}.png?size=40` | Account avatar URL. Same pattern. |

## Display-only string formatters (cosmetic, optional)

These build human-readable URLs for toasts and surface display. GHES users
will see github.com links until fixed; nothing breaks.

| File | Line | Current |
|---|---|---|
| `chat/backend/agent/tools/github_apply_fix_tool.py` | 207 | `https://github.com/{owner}/{repo}/pull/{n}` |
| `chat/backend/agent/tools/github_commit_tool.py` | 178 | `https://github.com/{repo}/commit/{sha}` |
| `routes/incidents_routes.py` | 1541 | startswith check for `https://github.com/` |

## Not in scope

| File | Why |
|---|---|
| All `docs.github.com/...` references | Documentation links, identical for GitHub.com and GHES users |
| `chat/backend/agent/tools/web_search/web_search_service.py` | Web search domain filters, unrelated to API calls |
| `connectors/azure_connector/metrics_server_azure.py` | Kubernetes metrics-server release URL, unrelated |
| `utils/security/_generated_patterns.py` | gitleaks pattern source attribution |
| Aurora repo links in onboarding pages | Marketing/help links to Aurora's own GitHub repo |

## Test path

After the rewrite:

1. Set `GH_API_BASE_URL=https://ghes.test/api/v3` and `GH_BASE_URL=https://ghes.test` in a sandbox env.
2. Confirm `register_github_app.py --base-url https://ghes.test` still completes.
3. Confirm `/github/app/install` returns an install URL pointing at the GHES host.
4. Confirm `/github/app/installations/<id>` (token mint) succeeds against GHES.
5. Confirm webhook delivery from GHES still passes signature validation
   (it will — the secret is host-agnostic).
