# Bitbucket Cloud Connector

Connects Aurora to Bitbucket Cloud for repository browsing, pull requests, and issue tracking.

## Authentication Methods

### Option 1: OAuth 2.0 (Recommended)

1. Go to **Bitbucket Settings > OAuth consumers** (workspace-level) or **Personal Bitbucket settings > OAuth consumers**.
2. Click **Add consumer**.
3. Fill in:
   - **Name**: Aurora
   - **Callback URL**: `<NEXT_PUBLIC_BACKEND_URL>/bitbucket/callback`
   - **Permissions**: Check `Account: Read`, `Repositories: Read`, `Pull requests: Read`, `Issues: Read`, `Projects: Read`
4. Save and copy the **Key** (client ID) and **Secret** (client secret).

### Option 2: App Password

1. Go to **Personal Bitbucket settings > App passwords**.
2. Click **Create app password**.
3. Select permissions: `Account: Read`, `Repositories: Read`, `Pull requests: Read`, `Issues: Read`, `Projects: Read`.
4. Save the generated password.

Users provide their Bitbucket email and app password via the Aurora UI.

## Required Environment Variables

```env
# OAuth (required for OAuth flow)
BB_OAUTH_CLIENT_ID=<your-oauth-consumer-key>
BB_OAUTH_CLIENT_SECRET=<your-oauth-consumer-secret>

# Feature flag (enables the Bitbucket integration)
NEXT_PUBLIC_ENABLE_BITBUCKET=true
```

## OAuth Scopes

The following Bitbucket OAuth scopes are requested:

| Scope          | Purpose                        |
|----------------|--------------------------------|
| `repository`   | Read access to repositories    |
| `pullrequest`  | Read access to pull requests   |
| `issue`        | Read access to issues          |
| `account`      | Read access to user account    |
| `project`      | Read access to projects        |

## API Endpoints

All endpoints are prefixed with `/bitbucket` and require the `X-User-ID` header.

| Method | Path                                        | Description                       |
|--------|---------------------------------------------|-----------------------------------|
| POST   | `/bitbucket/login`                          | Initiate OAuth or app password    |
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
