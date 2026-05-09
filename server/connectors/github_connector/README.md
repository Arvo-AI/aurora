# GitHub Connector

Aurora connects to GitHub via a **GitHub App** installation. The
prior OAuth flow was removed in `feat/github-app-only`.

## What the App gives Aurora

| Capability | Notes |
|---|---|
| **Auth model** | Installation token (acts as the App, not as a user) |
| **Rate limit** | 5,000 req/hr per installation, isolated from user limits; scales with installed repo count |
| **Permissions** | Fine-grained read on Contents, Issues, Pull requests, Actions, Deployments, Checks, Metadata |
| **Webhooks** | Real-time delivery of `installation`, `installation_repositories`, `pull_request`, `issues`, `deployment`, `deployment_status`, `workflow_run`, `check_run`, `check_suite` |
| **Who installs** | Org admin (or user, on a personal account) installs once and selects exactly which repos to grant |
| **Org UX** | Single install per org; new members inherit access |
| **Survives user departure** | Yes — install is owned by the account, not by an individual user |
| **Attribution** | Aurora appears as `<app-slug>[bot]` in any GitHub UI surface |

## Setup

| If you want to... | Read |
|---|---|
| Bootstrap a fresh App (one-time, scripted) | Run `python3 server/scripts/register_github_app.py --org <your-org>` |
| Operator manual walkthrough (web UI, Vault paths, troubleshooting) | [SETUP_GITHUB_APP.md](./SETUP_GITHUB_APP.md) |
| On-prem deployment, OAuth fallback, GHES status | [Aurora docs site → Connectors → GitHub](../../../website/docs/integrations/connectors.md#github) |

The bootstrap script drives GitHub's App Manifest flow: opens a browser
tab, captures the post-create redirect, and writes `.env` + Vault for
you. The manual walkthrough is the fallback when you want to register
through GitHub's web UI directly.

## Required `.env` keys

```bash
GITHUB_APP_ID=                    # numeric, from App settings page
GITHUB_APP_CLIENT_ID=             # starts with Iv1. or Iv23.
NEXT_PUBLIC_GITHUB_APP_SLUG=      # the URL slug, e.g. aurora-by-arvo
GITHUB_APP_WEBHOOK_URL=           # https://<host>/github/webhook (must be public)
GITHUB_APP_SETUP_URL=             # https://<host>/github/app/install/callback
GITHUB_APP_WEBHOOK_SECRET=        # fallback if Vault path not set
```

Plus the App's private key PEM in Vault at
`aurora/system/github-app/private-key` (field `value`).
