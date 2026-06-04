# Bitbucket Cloud Connector

Connects Aurora to Bitbucket Cloud for repository browsing, pull requests, issue tracking, and CI/CD pipelines.

## Authentication Methods

### Option 1: API Token (Recommended)

1. Go to **[Atlassian Account > Security > API tokens](https://id.atlassian.com/manage-profile/security/api-tokens)**.
2. Click **Create API token with scopes** (not the legacy "Create API token" button).
3. Select these scopes:
   - `read:user:bitbucket`
   - `read:workspace:bitbucket`
   - `read:project:bitbucket`
   - `read:repository:bitbucket`
   - `write:repository:bitbucket`
   - `read:pullrequest:bitbucket`
   - `write:pullrequest:bitbucket`
   - `read:issue:bitbucket`
   - `write:issue:bitbucket`
   - `read:pipeline:bitbucket`
   - `write:pipeline:bitbucket`
4. Save the generated token.

Users provide their Bitbucket email and API token via the Aurora UI.

> **Note:** Bitbucket deprecated App Passwords in September 2025 and will fully disable them in June 2026. Use scoped API tokens.

### Option 2: OAuth 2.0 (Self-hosted only)

OAuth requires server-side configuration (`BB_OAUTH_CLIENT_ID` + `BB_OAUTH_CLIENT_SECRET`). The UI hides the OAuth tab when these aren't set.

To set up:

1. Go to **Bitbucket Settings > OAuth consumers** (workspace-level).
2. Click **Add consumer**.
3. Fill in:
   - **Name**: Aurora
   - **Callback URL**: `<NEXT_PUBLIC_BACKEND_URL>/bitbucket/callback`
   - **Permissions**: Account Read, Workspace membership Read, Projects Read, Repositories Write, Pull requests Write, Issues Write, Pipelines Write
4. Save and copy the **Key** (client ID) and **Secret** (client secret).

## Required Environment Variables

```env
# OAuth (optional — only needed if you want to offer the OAuth flow)
BB_OAUTH_CLIENT_ID=<your-oauth-consumer-key>
BB_OAUTH_CLIENT_SECRET=<your-oauth-consumer-secret>
```

## API Endpoints

All endpoints are prefixed with `/bitbucket` and require authentication.

| Method | Path                                        | Description                       |
|--------|---------------------------------------------|-----------------------------------|
| POST   | `/bitbucket/login`                          | Initiate OAuth or API token       |
| GET    | `/bitbucket/callback`                       | OAuth callback                    |
| GET    | `/bitbucket/status`                         | Check connection status           |
| POST   | `/bitbucket/disconnect`                     | Disconnect account                |
| GET    | `/bitbucket/workspaces`                     | List workspaces                   |
| GET    | `/bitbucket/projects/<workspace>`           | List projects in workspace        |
| GET    | `/bitbucket/repos/<workspace>`              | List repositories (opt. ?project=)|
| GET    | `/bitbucket/branches/<workspace>/<repo>`    | List branches                     |
| GET    | `/bitbucket/pull-requests/<workspace>/<repo>`| List pull requests (opt. ?state=)|
| GET    | `/bitbucket/issues/<workspace>/<repo>`      | List issues                       |
| GET    | `/bitbucket/workspace-selection`            | Get stored selection              |
| POST   | `/bitbucket/workspace-selection`            | Save selection                    |
| DELETE | `/bitbucket/workspace-selection`            | Clear selection                   |
