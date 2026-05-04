# GitHub App Migration Runbook

For Aurora workspaces that already use the legacy OAuth GitHub connector
and want to move to (or add) the new GitHub App auth path.

This document is for **end users and workspace admins**. For the operator
one-time setup of the App itself, see
[server/connectors/github_connector/SETUP_GITHUB_APP.md](../../server/connectors/github_connector/SETUP_GITHUB_APP.md).

---

## Overview: dual-mode is permanent

Aurora's GitHub connector now supports **two independent authentication
paths**, both available indefinitely:

- **OAuth App** (legacy) — per-user OAuth token, every existing connection
  keeps working with no action required.
- **GitHub App** (new, recommended) — installation token, finer
  permissions, real-time webhooks.

There is **no automatic migration** and **no forced cutover**. Existing
OAuth connections stay intact; the App path is purely additive. You can
run both side-by-side forever.

When a user has **both** auth paths connected for the same repo, Aurora
automatically prefers the GitHub App installation token (better rate
limits, installation-scoped audit trail). The OAuth token stays cached as
a transparent fallback if the install becomes unavailable.

---

## For new installs: pick the GitHub App path

If you are setting up the GitHub connector for the first time after the
operator has configured the App (per
[SETUP_GITHUB_APP.md](../../server/connectors/github_connector/SETUP_GITHUB_APP.md)):

1. Open Aurora and navigate to **Connectors > GitHub**.
2. You will see two CTAs:
   - **Install GitHub App** (recommended)
   - **Connect via OAuth** (legacy)
3. Click **Install GitHub App**. You are redirected to GitHub.
4. Pick the account (your personal user or an org), choose **Only select
   repositories**, pick the repos Aurora should access, and click
   **Install**.
5. GitHub redirects back to Aurora. The new installation appears on the
   **Connectors > GitHub** page with the selected repos and a
   **Connected** status.

That's it — no env-var edits, no token generation. Aurora handles JWT
signing, installation-token refresh (1-hour rotation, server-side), and
webhook delivery transparently.

If the operator has not yet configured the App, only the **Connect via
OAuth** CTA is visible and the App CTA is hidden / disabled. Hand them
this guide and [SETUP_GITHUB_APP.md](../../server/connectors/github_connector/SETUP_GITHUB_APP.md).

---

## For existing OAuth users: optional manual migration

You do **not** have to migrate. Your existing OAuth connection keeps
working. Migrate only if you want the benefits listed in the connector
[README](../../server/connectors/github_connector/README.md) (rate-limit
isolation, fine-grained permissions, webhook-driven correlation, etc.).

**Steps:**

1. Open **Connectors > GitHub** in Aurora.
2. Notice your existing **OAuth** connection card stays visible — leave
   it alone.
3. Click **Install GitHub App**.
4. On GitHub's install page, pick the same account and the same set of
   repos you previously connected via OAuth.
5. Finish the install. You are redirected back to Aurora; the installation
   appears alongside the OAuth card.
6. From now on, for any repo present in **both** connections, Aurora's
   auth router automatically uses the App installation token. No code
   change, no setting to flip.
7. (Optional) To fully retire the OAuth connection: click the
   **Disconnect** button under the OAuth card. The App connection stays
   intact and continues to handle every repo that was selected during
   install.

> Step 7 is the **only** destructive action in this runbook. If you
> disconnect OAuth and the App install is later revoked or suspended, no
> fallback is available and the affected repos go offline until the
> install is restored. Keep both connected unless you are sure.

---

## Behavioral differences after migration

These are user-visible differences when Aurora operates through the App
instead of OAuth. None of them changes Aurora's read-only posture toward
your code.

| Area | OAuth | GitHub App |
|---|---|---|
| **Author of any Aurora-created comments / commits / PRs** | The connecting user (e.g. `alice`) | `aurora-<slug>[bot]` (e.g. `aurora-acme[bot]`) — visibly attributed to the App, not to a person |
| **Audit log entries** | Attributed to the connecting user | Attributed to the App installation (with the installer's ID recorded) |
| **API rate limit budget** | Shared with everything else `alice` does on GitHub | Isolated per-installation, scales with installed repo count |
| **Repo access changes** | Manual — user must revoke / regrant | Real-time via `installation_repositories` webhook — Aurora syncs additions and removals automatically |
| **Permissions** | `repo` scope (full read / write on every repo the user can see) | Granular per-resource (Contents R, Issues R, PRs R, Actions R, Deployments R, Commit statuses R, Metadata R, Webhooks R+W, Email R) |
| **What happens when the connecting user leaves the org** | Connection breaks; someone else must reconnect | Install survives; only the user→installation link is removed |
| **Webhook events** | None | `installation`, `installation_repositories`, `pull_request`, `issues`, `deployment`, `deployment_status`, `workflow_run`, `check_run`, `check_suite` |
| **Historical event backfill on first install** | N/A | None — Aurora only sees events that fire **after** the App is installed |

> **Future-readiness note**: The App requests `Contents: Read and write`
> so Aurora can later support auto-fix PRs without users needing to
> re-approve permissions. Today, write access is **not used** by any code
> path; the auth router enforces read-only at runtime.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Install card shows **Suspended** | An account owner suspended the App from <https://github.com/settings/installations> (or the org equivalent) | Owner re-enables the install from the same page; Aurora picks it up on the next webhook delivery (or within ~1 hour on the next token refresh) |
| Install card shows **Pending org admin approval** | A non-admin org member started the install on an org that requires admin approval | An org owner approves the request at `https://github.com/organizations/<org>/settings/installations` |
| Install card shows **Pending permissions update** | The Aurora App's permission set changed since this install was created | An org owner reviews and accepts the new permissions on the same settings page |
| Multiple installations listed for the same user | Same user installed Aurora separately on a personal account and on one or more orgs | Pick the one whose repos you actually want from the installation picker on the connector page; you can leave the others or unlink them via the trash icon |
| **No repos accessible** banner under an install | The install was completed without selecting any repos (or all repos were later deselected) | Click **Manage on GitHub** from the install card; reselect repos on the App's install page |
| Repo selection works in GitHub but Aurora still shows zero repos after a few minutes | `installation_repositories` webhook delivery failed (signature mismatch, network) | Operator: check `webhook_deliveries` table and the App's **Recent Deliveries** page on GitHub; rewrite the webhook secret per [SETUP_GITHUB_APP.md Step 7](../../server/connectors/github_connector/SETUP_GITHUB_APP.md#step-7-write-secrets-to-vault) |
| Aurora keeps using OAuth even after I installed the App | The repo you tested with is not in the App's selected-repos list | Reopen the install on GitHub and add the repo; the auth router picks the App as soon as the install covers that repo |
| OAuth disconnect button is missing | You only ever connected via the App | Expected — there is no OAuth connection to disconnect |

---

## See also

- [server/connectors/github_connector/README.md](../../server/connectors/github_connector/README.md) — Connector overview and OAuth-vs-App comparison
- [server/connectors/github_connector/SETUP_GITHUB_APP.md](../../server/connectors/github_connector/SETUP_GITHUB_APP.md) — Operator one-time App setup
- GitHub docs: [Installing your own GitHub App](https://docs.github.com/en/apps/using-github-apps/installing-your-own-github-app)
- GitHub docs: [Reviewing and modifying installed GitHub Apps](https://docs.github.com/en/apps/using-github-apps/reviewing-and-modifying-installed-github-apps)
