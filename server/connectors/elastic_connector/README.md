# Elastic Cloud Connector (Elasticsearch + Kibana)

Read-only connector for Elastic Cloud Hosted, Elastic Cloud Serverless and self-managed clusters. Aurora searches logs (Lucene / ES|QL), reads Kibana alert documents, lists alerting rules, and can ingest Kibana alert actions through a per-user webhook.

Feature-flagged: set `NEXT_PUBLIC_ENABLE_ELASTIC=true` (default `false`).

## Files

| Path | Purpose |
|------|---------|
| `connectors/elastic_connector/client.py` | `ElasticClient` (retries, error mapping), `parse_cloud_id`, `normalize_url`, `normalize_api_key`, `build_log_query`, `compact_hits` |
| `routes/elastic/elastic_routes.py` | `/elastic/connect`, `/status`, `/disconnect`, `/alerts`, `/alerts/webhook/<user_id>`, `/alerts/webhook-url`, `/rca-settings` |
| `routes/elastic/search_routes.py` | `/elastic/indices`, `/fields`, `/search`, `/esql`, `/alerts/active`, `/rules` |
| `routes/elastic/tasks.py` | Celery `elastic.process_alert` (webhook → `elastic_alerts` → incident → RCA) |
| `chat/backend/agent/tools/elastic_tool.py` | Agent tools: `elastic_list_indices`, `elastic_get_fields`, `elastic_search_logs`, `elastic_esql`, `elastic_get_alerts` |
| `chat/backend/agent/skills/integrations/elastic/SKILL.md` | Agent skill (RCA workflow, query cheat sheet) |
| `utils/elastic_config.py` | `ELASTIC_SSL_VERIFY` (default `true`; `false` or CA-bundle path) |

## Setup (short version)

1. In Kibana (as an admin) create an API key under Stack Management → API keys with **Restrict privileges** and a read-only role descriptor (see docs), or as a user holding **Viewer** + a custom role with `manage_own_api_key`. Copy the **Encoded** value.
2. In Aurora open **Connectors → Elastic Cloud**, paste the **Cloud ID** (Hosted) or the Elasticsearch/Kibana URLs (Serverless, self-managed), paste the key, connect.
3. Optional: create a Kibana **Webhook** connector (Basic auth `aurora` / the secret shown in Aurora), attach it to rules with action frequency **On status changes**, paste the action body template from Aurora, and add a **Recovered** action.
4. Turn on **Enable Alert RCA** if webhook alerts should create incidents (off by default; alerts are always stored under View Alerts).

Full guide with the least-privilege role descriptor and troubleshooting: `website/docs/integrations/connectors.md` → *Elastic Cloud*.

## Stored credential payload (Vault, provider `elastic`)

```
api_key, elasticsearch_url, kibana_url, cloud_id, deployment_type (cloud_hosted|serverless|self_managed),
cluster_name, version, username, index_pattern, kibana_reachable, webhook_secret, validated_at
```

`user_tokens.client_id` holds the Kibana URL (or ES URL) so incidents can deep-link without a Vault read.

## Webhook contract

`POST /elastic/alerts/webhook/<user_id>` authenticated by the stored `webhook_secret` (constant-time compare) presented as HTTP Basic password, `Authorization: Bearer`, or `X-Aurora-Webhook-Secret`. Body is the JSON produced by the action template in `elastic_routes.KIBANA_ACTION_BODY_TEMPLATE`; raw `{{context}}` / `{{rule}}` objects are also tolerated.

Processing rules (`tasks.py`):
- `action_group == "recovered"` → stored as `recovered`, previous active rows for the same `alert_uuid` flipped to recovered, no incident.
- Active re-fire for an `alert_uuid` already linked to an incident → payload refreshed, incident `updated_at` bumped, nothing else (Kibana re-fires every interval unless the action is "On status changes").
- Otherwise → row inserted, correlator consulted, and only when `elastic_rca_enabled` is true an incident + summary + background RCA are created.

## Elastic API notes

- Auth header: `Authorization: ApiKey <base64(id:api_key)>`; Kibana also needs `kbn-xsrf: true`.
- Serverless has no `_cluster/health` / nodes APIs; the client never calls them. `GET /` → `version.build_flavor == "serverless"` is used to detect it.
- `GET /` and `_cat/indices` need cluster `monitor` (the built-in Viewer role lacks it). Connect/status validate via `_security/_authenticate`; `GET /` is best-effort (403 → version/cluster unknown), `_cat/indices` best-effort (403 → stats omitted).
- Kibana `/api/alerting/rules/_find` needs Kibana application privileges (Viewer role or `kibana-.kibana` read).
- Kibana Webhook connector is Gold+ on self-managed.
