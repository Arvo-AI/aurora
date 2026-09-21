# Plan: multiple Datadog connections

Status: awaiting approval. No code written.

## Problem

Bombora runs two Datadog instances, dev and prod. Both alert into a single PagerDuty,
and PagerDuty forwards everything to Aurora. Aurora holds one Datadog credential, and
they connected prod.

So when a dev alert arrives, the agent investigates it against prod telemetry. From the
Sep 17 meeting:

> we only have one PagerDuty... we have one Datadog in dev or one Datadog in prod. And
> all both of them alert to PagerDuty... So this alert that I got was for dev. So it
> tried to investigation on the prod project. Oh, but it was a dev problem. It was a
> deployment... you should have flagged CloudTrail, not Firemesh... it's a wrong context.
>
> The agent has the wrong context. So that's the reason that the RCA is not accurate.

Two failures stacked:

1. **Missing data.** The dev Datadog is not connected, so telemetry for the thing that
   actually broke is unreachable.
2. **Misleading data.** Because prod *is* connected, the agent does not get back "nothing
   found" -- it gets healthy-looking prod data, concludes the service is fine, and hunts
   elsewhere until it lands on a wrong culprit.

Failure 2 is why connecting a second Datadog is necessary but not sufficient on its own.
The agent also has to know the second org exists and pick the right one.

## Current state: this is a data-loss bug, not just a missing feature

Two facts worth stating before the design:

- `POST /connect` (`server/routes/datadog/datadog_routes.py:337`) calls `store_tokens_in_db`
  with the whole payload, and every branch ends in `ON CONFLICT (org_id, provider) DO
  UPDATE`. Connecting a second org **overwrites the first org's credentials.**
- `DatadogConnectionStep` renders only when `!isConnected`
  (`client/src/app/datadog/auth/page.tsx:259`), so there is currently **no reachable UI
  path to add a second org at all.**

So the feature is not merely absent; the one route that could add an org silently destroys
the existing one.

## Scope decision

Connection support plus agent-side selection. **No backend environment detection**: nothing
will populate `incidents.alert_environment`, and there is no dev/prod inference from the
PagerDuty payload. The agent discovers the connected orgs and chooses.

This was a deliberate call. It is also the main residual risk, recorded in "Risks" below.

## Why not the AWS/Azure pattern

AWS and Azure are multi-account because one credential spans N accounts:

- AWS stores no per-account secret at all, just a `role_arn` per row, assumed on demand
  (`server/routes/aws/onboarding.py:479`, and the note at
  `server/utils/auth/stateless_auth.py:175`).
- Azure has one service principal and enumerates subscriptions at login
  (`server/connectors/azure_connector/auth.py:127`).

Both express multiplicity as rows in `user_connections`, which holds **no credentials**.

Datadog has no equivalent -- each org needs its own API + application key pair. So
`user_connections` gives us nothing, and the alternative is per-account Vault secrets.
That means adding an account parameter to `store_tokens_in_db` (a ~480-line if/elif chain
shared by roughly 30 connectors, with `ON CONFLICT (org_id, provider)` on every branch),
plus `get_token_data` and `delete_user_secret`. High blast radius for no benefit here.

## Storage shape

Keep the single Vault secret. Store a list inside it -- the blob is already free-form JSON.

```
{
  # primary mirrored at the top level; these keys are unchanged from today
  "api_key", "app_key", "site", "base_url", "org_name",
  "org_id", "validated_at", "service_account_name", "label": "prod",

  "accounts": [
    {"label": "prod", "api_key": ..., "app_key": ..., "site": ..., ...},
    {"label": "dev",  "api_key": ..., "app_key": ..., "site": ..., ...}
  ]
}
```

Mirroring the primary at the top level is what makes this cheap. It means:

- `store_tokens_in_db`'s datadog branch (`server/utils/auth/token_management.py:172-190`)
  keeps reading `org_name` / `org_id` / `site` / `service_account_name` for its
  `user_tokens` display columns -- **no change to shared token code**.
- The skill's `connection_check` keeps finding `api_key`, so Datadog does not vanish from
  the agent's toolset.
- The webhook's `get_token_data` truthiness check keeps working.
- Any other `creds.get("api_key")` reader keeps working.

No schema change. No migration. No new table.

An account's identity is its **label**, falling back `label` -> `org_name` -> `site` ->
`"default"`. A pre-existing single-credential blob reads as a one-element list, so
already-connected users are unaffected and need no reconnect.

## Changes

### Backend

**1. `server/routes/datadog/datadog_routes.py`**

Five helpers after `_build_client_from_creds` (~`:283`):

| Helper | Purpose |
|---|---|
| `_account_label(account)` | label with the fallback chain |
| `_account_summary(account)` | credential-free view, safe for API responses and the agent |
| `_list_datadog_accounts(user_id)` | all orgs, primary first; legacy blob reads as one; drops entries missing either key |
| `_select_account(accounts, selector)` | by label, case-insensitive; no selector means primary |
| `_resolve_client(user_id, selector)` | `(client, None)` or `(None, flask_error_tuple)` |

Route changes:

- `/connect` `:286` -- accept an optional `label`; upsert into the account list by label
  instead of overwriting. Writes `{**accounts[0], "accounts": accounts}`.
- `/status` `:354` -- return an `accounts` array with per-account `valid` / `error`.
  `connected` becomes "any account valid", so a revoked dev key does not read as
  "Datadog disconnected" and hide the working prod org. Top-level fields stay as the
  primary's, preserving the current response contract.
- `/disconnect` `:386` -- optional `?account=<label>` removes one org and rewrites the
  blob. No selector keeps today's remove-everything behaviour, so existing callers are
  unaffected. Removing the last org falls through to the existing full teardown.
- `/logs/search`, `/metrics/query`, `/events`, `/monitors` -- the repeated 6-line
  creds-then-client preamble collapses into a 3-line `_resolve_client(user_id,
  request.args.get("account"))`. **Net fewer lines than today.**
- `/webhook/<user_id>` `:612` -- untouched.

**2. `server/chat/backend/agent/tools/datadog_tool.py`**

- `is_datadog_connected` `:112` becomes `bool(_list_datadog_accounts(user_id))`.
  Load-bearing: miss this and the new blob shape reads as disconnected, silently dropping
  Datadog from the agent's toolset.
- `QueryDatadogArgs` -- `resource_type` gains `'accounts'`; new optional `account` field.
- `query_datadog` `:570` -- `resource_type='accounts'` short-circuits before client
  resolution (a local read, zero Datadog API calls). An unknown label errors back with
  the valid labels listed. Every result is stamped `"account": <label>` so the RCA can
  state which org answered.

**3. `server/chat/backend/agent/tools/cloud_tools.py` `:2331`**

One **static** sentence added to the tool description: multiple orgs may be connected,
call `resource_type='accounts'` to list them, pass `account=<label>` to choose. No
per-user interpolation, no dynamically built description.

**4. `server/chat/backend/agent/skills/integrations/datadog/SKILL.md`**

- `connection_check.required_any_fields` gains `accounts`, decoupling the check from the
  top-level mirror.
- Document the `accounts` resource type and the `account` argument.
- Add a Step 0 to the RCA workflow: list the connected orgs, pick the one matching the
  incident's environment, and say which one was used. Written in the file's existing
  prose voice -- no shouting.

**5. The two other status paths.** `/datadog/status` is not the only place Datadog
connectivity is decided; two independent paths each validate the primary only, and both
need the same "any account valid" fold or a revoked prod key renders "Datadog
disconnected" while a healthy dev org sits there queryable:

- `server/routes/account_management.py:88` -> `_validate_provider_connection` (`:34-42`)
  -> `_check_datadog`. Feeds `/api/connected-accounts`, i.e. the connector card.
- `server/routes/connector_status.py:912` -> `_check_datadog` (`:65-83`, registered
  `:807`). Returns `{"connected", "site"}`; the return type `Dict[str, Dict[str, Any]]`
  permits adding `accounts`. Also `_count_connected_connectors` (~`:855-862`) counts
  Datadog as 1 regardless of org count.

Both read the top-level mirror, so neither *breaks* -- they just under-report. Minimal fix
is the any-valid fold in `_check_datadog`, which both share.

**6. `server/aurora_mcp/registry.py:186-220`** -- four Datadog dispatch entries
(`datadog_logs_search`, `datadog_metrics_query`, `datadog_events`, `datadog_monitors`), all
with `query_keys` unset. Per the dataclass docs at `:174-176`, forwarding already routes
any non-body/non-path arg to the query string -- so `account=` would *work* but is **never
advertised in the tool schema**, leaving MCP clients silently pinned to the primary. Add
`query_keys=("account",)` to all four. Note it belongs in `query_keys`, not `body_keys`,
even for the two POST entries.

Out of scope within MCP: `tools_gated.py`'s `_resolve_source` (`:95-101`) picks a provider
for the cross-provider `query_logs`/`query_alerts` tools. Adding an org axis there would
leak a Datadog-only concept into a provider-agnostic signature; MCP callers wanting a
specific org can use the Datadog-specific tools.

### Frontend

**5. `client/src/lib/services/datadog.ts`** -- `DatadogAccount` type, `accounts` on
`DatadogStatus`, `label` on the connect payload, `disconnect(label?)`. The current
`getStatus()` mapping (`:55-63`) builds a flat object field by field, so an `accounts`
array would be silently dropped if not added here.

**6. `client/src/components/datadog/DatadogConnectionStep.tsx`** -- one label `Input`
(placeholder `prod` / `dev`), retitled so it reads correctly for both the first and the
Nth connection. The copy at `:73` already says "switch to the desired organization if
needed", so it is half org-aware already.

**7. `client/src/app/datadog/auth/page.tsx`** -- when connected, list the orgs with a
validity badge, an "Add another organization" toggle that reveals the existing form, and
per-org remove. Reuses the existing component; no new multi-account UI framework. For
contrast, `client/src/app/aws/onboarding/page.tsx` carries ~26 state variables -- not
copying that. Also note the localStorage cache at `:11-14,72-99` stores the flat
single-org status blob and will hold a stale prod-shaped value after dev is added.

**8. `client/src/components/datadog/DatadogWebhookStep.tsx`** -- the highest-value UI
change, and the one most easily missed.

The webhook URL is per **user**, not per org (`server/routes/datadog/datadog_routes.py:651`
builds `/datadog/webhook/{user_id}`). With two orgs connected, **the same URL must be
pasted into both Datadog orgs.** Today:

- the only scoping hint is a `Per user` badge (`:44`), which reads as "unique to you", not
  "reuse this in every org";
- all eight numbered steps (`:55-83`) are written in the singular, describing a one-time
  action;
- the three tiles at `:25-38` render `status.site`, `status.serviceAccountName` and
  `status.org.name` from the top-level mirror, so with dev connected **they describe prod**
  while sitting directly above instructions meant for dev.

Net failure mode: the user adds dev credentials, the panel renders green with a
ready-to-copy URL, they never repeat the Datadog-side webhook creation inside the dev org,
and dev alerts never arrive. Nothing in the UI reports this.

Changes: badge becomes "Per user -- add to every org"; the step list says to repeat them in
each connected organization; the three tiles move into the per-account list from item 7 so
they stop claiming to describe the connection as a whole.

**9. `client/src/app/datadog/overview/page.tsx`** -- add an account dropdown. The page is an
ad-hoc query explorer whose subtitle currently reads "Run ad-hoc queries against your
connected Datadog **instance**" (`:100-103`) while silently querying the primary only. Its
three fetchers (`fetchLogs` `:39-57`, `fetchMetrics` `:59-76`, `fetchEvents` `:78-95`) all
hit routes that now accept `?account=`, so this is a select populated from `status.accounts`
plus the selector threaded through the three `datadogService` calls, and the answering org
echoed next to each result.

Without it, a human debugging a dev incident here hits exactly the wrong-context trap the
agent did -- an empty or healthy-looking result set from the wrong org, with no indication
which org answered. Cheap, and it keeps the human and agent paths honest in the same way.

**10. `client/src/app/api/datadog/disconnect/route.ts`** -- new file (~35 lines), forwarding
`?account=` through the proxy per the backend-proxy rule. Required because the route the
page actually calls today, `client/src/app/api/connected-accounts/[provider]/route.ts:28`,
issues a bare `DELETE` and **forwards no query string**, so the selector cannot reach the
backend through it. The auth page must be repointed at the new route or per-org disconnect
will nuke every org.

The four existing read proxies (`client/src/app/api/datadog/{logs,metrics,events,monitors}`)
must also forward the `account` query param, or the dropdown above cannot reach the backend.

### Documentation

**10. `server/connectors/datadog_connector/README.md`** -- Setup (`:5-27`) is a single
linear one-org sequence. The webhook section (`:29-36`) gives the `{user_id}` URL in the
singular; it must say the same URL goes in **every** org, and that naming the webhook
`aurora` in each makes `@webhook-aurora` work uniformly. The site table (`:19-24`) implies
one site per install -- two orgs may be on different sites (prod US1, dev EU), which the
per-account `site` field makes legal but no doc states. Troubleshooting (`:38-40`) needs a
"wrong org answered" entry, the most likely new failure.

**11. `website/docs/integrations/connectors.md:947-986`** -- public mirror of the above,
same four gaps.

**12. `website/docs/configuration/data-access/datadog.md`** -- the one doc change with
compliance weight, not cosmetics. It documents PII filtering via Sensitive Data Scanner
(`:111-151`), an `Aurora Restricted` role, restriction queries, and a dedicated service
account. **All of that is configured per org.** A hardened prod org plus an unhardened dev
org silently breaks the guarantee this page makes. Needs an explicit statement that the
whole procedure repeats for every connected org. The mermaid diagram at `:25` says
`subgraph datadog["Your Datadog Org"]`, singular.

Unaffected, confirmed: `website/docs/configuration/aws-secrets-manager.md:163` shows the
example secret path `...-datadog-token`, which **stays valid** because the design keeps one
secret. Useful evidence of low blast radius.

### Test

**9. `server/tests/chat/test_datadog_accounts.py`** -- pure-function tests on the helpers
with `_get_stored_datadog_credentials` monkeypatched. No fixtures, no live API:

- legacy bare blob resolves to exactly one account, label derived from `org_name`
- two-account blob resolves to both, order preserved
- `_select_account` is case-insensitive, returns the primary with no selector, `None` on a miss
- entries missing `api_key` or `app_key` are filtered out
- `is_datadog_connected` is true for both blob shapes

## Explicitly out of scope

- **Environment detection.** `incidents.alert_environment` (`server/utils/db/db_utils.py:750`)
  stays unpopulated for PagerDuty. It exists but only the Jenkins webhook writes it
  (`server/routes/jenkins/tasks.py:238`).
- **Per-account event attribution in the database.** `datadog_events` (`db_utils.py:603`)
  has no account column, so ingested alerts are not attributable to an org at the schema
  level. Consequence: per-account disconnect deliberately keeps events; only a full
  disconnect deletes them (`datadog_routes.py:398`), so "remove dev then prod" and "remove
  both at once" leave different amounts of history.

  Correction to an earlier draft of this plan: a **best-effort** origin badge is cheaper
  than stated. Datadog webhook payloads carry `$ORG_NAME`/`$ORG_ID`, the full payload is
  already stored as JSONB and already rendered in the events page `<details>` block. So
  `client/src/app/datadog/events/page.tsx` could read origin from the payload with no
  schema change. Deferred, not impossible -- worth doing if the mixed prod/dev event
  stream proves confusing.
- **No fan-out.** One query hits one org. No N-fold API cost, no changed response shapes.
- **Datadog console deep links.** `server/routes/incidents_routes.py:44-58` builds
  `https://app.{client_id}` where `client_id` is the **primary's** site (written by
  `token_management.py:172-190`). With prod on `datadoghq.com` and dev on `datadoghq.eu`,
  every incident's "view in Datadog" link lands in the prod org -- the UI mirror of the
  agent's wrong-context bug. Fixing it requires per-event org attribution, so it is blocked
  on the item above.
- **Alert correlation across orgs.** `server/services/correlation/alert_correlator.py` keys
  on `source_type` (`'datadog'`), so prod and dev alerts correlate as if from one system and
  recurrence detection could fold a dev alert into a prod incident's history. Behaviour is
  unchanged by this work but becomes reachable by it.
- **The empty `service_name` bug.** RCA skill templates read
  `alert_details["labels"]["service"]` or `["title"]`
  (`server/chat/backend/agent/skills/registry.py:519`), but PagerDuty's `trigger_metadata`
  has neither (`server/routes/pagerduty/tasks.py:391-396`), so `service_name` renders empty
  for every PagerDuty RCA. Independently contributes to "wrong context". Separate fix.
- **`ProviderPolling` per-org state.** `client/src/components/cloud-provider/core/ProviderPolling.ts`
  types provider state as `Record<string, boolean>` (`:54-59`, `:114`, `:187-194`), which
  cannot express "prod connected, dev not". Two consequences accepted as-is: the two status
  sources can disagree under partial key failure (`:54-59` marks connected on mere row
  existence, `:64-70` uses validated status, last writer wins), and
  `clearProviderCache('datadog')` (`:88-92`) is all-or-nothing. Same for the single
  `isDatadogConnected` localStorage flag.

## Risks

1. **The agent may not bother checking.** With no environment detection, the only thing
   preventing the original bug is the agent calling `accounts` and choosing well. Mitigated
   by the skill instruction and by stamping the answering org onto every result so a wrong
   choice is visible in the RCA output rather than silent.
2. **Connection-check regression.** Both `is_datadog_connected` and the skill's
   `connection_check` must accept the new shape or Datadog disappears from the toolset with
   no error. Mitigated by the top-level mirror plus the test.
3. **Status cost.** Validating every org takes 2N Datadog API calls per poll instead of 2.
   Accepted -- N is 2-3, and the alternative hides a dead key.
4. **Half-finished webhook setup.** The likeliest support ticket: credentials added for dev,
   Datadog-side webhook never created in the dev org, dev alerts never arrive, UI shows
   green. Mitigated by the item 8 copy changes. A per-org "webhook received" indicator would
   close it properly but needs the payload-derived attribution above.
5. **Payload drill-down degrades.** `server/chat/backend/agent/tools/alert_payload_tool.py:111-126`
   falls back to a +/-10-minute `received_at` window and returns `None` unless **exactly one**
   row matches. Two orgs webhooking into one table make multi-row matches more common, so
   `get_alert_field` starts returning "No payload found in datadog_events" mid-RCA. The
   fail-closed guard is correct -- better than a wrong payload -- but fires more often
   precisely when alert noise is highest. Not fixed here; flagged because it is a real
   regression in RCA quality that multi-org introduces.
6. **Context-safety test.** `server/tests/auth/test_integration_credentials_context_safe.py:40`
   asserts `_get_stored_datadog_credentials` is context-safe. Any refactor of the resolver
   must keep it passing.

## Known ceilings

One Vault blob for all orgs means no per-org key rotation or RBAC, and the blob grows with
org count. Upgrade path if it is ever needed: `user_connections` rows plus one Vault secret
per account, which is also what would be required to attribute ingested events.

## Size

Roughly 200 lines backend, 220 frontend, one new file, one test file, plus doc edits across
four files.
