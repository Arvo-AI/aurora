# GitHub Connector

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
