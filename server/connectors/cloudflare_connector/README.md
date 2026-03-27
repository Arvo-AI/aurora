# Cloudflare Connector

API Token authentication for Cloudflare accounts. Supports both user-owned and account-owned (`cfat_`) tokens.

## Overview

The Cloudflare connector allows Aurora to read DNS zones, analyze permissions, and manage DNS records, purge cache, configure firewall rules, and monitor Workers. No server-side environment variables are required — users authenticate entirely through the Aurora UI.

### Current Capabilities

- **Connection management** — Connect / disconnect / status checks
- **Zone listing** — View all DNS zones with status, plan, and account info
- **Permission auditing** — Real-time detection of granted and missing permissions
- **Token type detection** — Automatic handling of user-owned vs account-owned tokens

### Planned

- DNS record management
- Cache purging
- WAF & firewall rule configuration
- Workers monitoring
- Load balancer control
- Agent tool integration (chatbot)

## Setup

### 1. Create an API Token

1. Go to [Cloudflare Dashboard](https://dash.cloudflare.com/) > **My Profile** > **API Tokens**
2. Click **Create Token**
3. Use the **"Read all resources"** template as a starting point
4. Add the **Account — API Tokens — Read** permission (required for Aurora to audit token permissions)
5. Optionally add write permissions depending on your needs:

| Permission | Purpose |
|------------|---------|
| Zone — DNS — Edit | DNS record management |
| Zone — Cache Purge — Purge | Cache purging |
| Zone — Firewall Services — Edit | WAF & firewall rules |
| Account — Load Balancers — Edit | Load balancer control |

6. Click **Continue to summary** > **Create Token**
7. Copy the token

### 2. Connect via Aurora UI

1. Navigate to **Connectors** > **Cloudflare**
2. Paste your API token
3. Click **Connect Cloudflare**

Aurora validates the token, discovers your account and zones, and stores the token securely in Vault. No environment variables needed.

### Account-Owned Tokens (`cfat_`)

Cloudflare supports account-owned API tokens (prefixed with `cfat_`), which are tied to the account rather than a specific user. Aurora detects these automatically. If you're using a user-owned token, the UI will show a recommendation to switch to an account-owned token for better security practices.

## Architecture

```
client/src/app/cloudflare/auth/page.tsx    → Frontend auth page
server/routes/cloudflare/                  → Flask blueprint (5 endpoints)
server/connectors/cloudflare_connector/
  ├── auth.py                              → Token validation (user + account tokens)
  └── api_client.py                        → CloudflareClient (zones, accounts, permissions)
```

### API Endpoints

All endpoints are mounted under `/cloudflare_api` and protected by RBAC + rate limiting.

| Endpoint | Method | Permission | Description |
|----------|--------|------------|-------------|
| `/cloudflare/connect` | POST | `connectors:write` | Validate token, store in Vault |
| `/cloudflare/status` | GET | `connectors:read` | Check connection, refresh permissions |
| `/cloudflare/zones` | GET | `connectors:read` | List zones with saved preferences |
| `/cloudflare/zones` | POST | `connectors:write` | Save zone enable/disable selections |
| `/cloudflare/disconnect` | POST | `connectors:write` | Remove token from Vault, clear prefs |

### Token Storage

Credentials are stored in HashiCorp Vault (never in the database directly). The DB holds a Vault reference of the form `vault:kv/data/aurora/users/{secret_name}`. The Vault secret contains:

- `api_token` — The Cloudflare API token
- `token_id` — Cloudflare's internal token identifier
- `token_type` — `account` or `user`
- `permissions` — List of granted permission names
- `email` — User email (user-owned tokens only)
- `account_name` / `account_id` — Primary Cloudflare account info
- `accounts` — Full list of accessible accounts

## Troubleshooting

| Error | Solution |
|-------|----------|
| "Invalid API token" | Verify the token in Cloudflare Dashboard > API Tokens — it should show as "Active" |
| "Could not determine the account for this token" | Account-owned tokens (`cfat_`) need account-level access. Check the token's permissions |
| "Access denied / token revoked" | The token was revoked or disabled. Create a new one |
| "Missing required permissions" | The permission audit in the UI lists exactly which permissions are missing. Edit the token in Cloudflare to add them |
| Connection status shows disconnected after working | The `/status` endpoint re-validates the token on every call. If Cloudflare rejects it, Aurora auto-cleans the stored secret |
