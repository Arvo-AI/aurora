# Deploying Change Gating (Incident Prevention) to Production

Branch: `sms10221/dev-1252-alert-prediction-via-change-gate`

## GitHub Apps (already created)

| Env | App | Slug | App ID | Client ID |
|-----|-----|------|--------|-----------|
| Staging | [Arvo AI Staging](https://github.com/apps/arvo-ai-staging) | `arvo-ai-staging` | 4088260 | `Iv23liK3x9Wh7SF8Vxpo` |
| Prod | [Arvo AI](https://github.com/apps/arvo-ai) | `arvo-ai` | 4088434 | `Iv23li1c91KQ05OjE9Ha` |

## Secrets (already stored in AWS SM)

| Secret | Staging (us-west-1) | Prod (us-west-2) |
|--------|:--:|:--:|
| `aurora/system/github-app/private-key` | Done | Done |
| `aurora/system/github-app/webhook-secret` | Done | Done |
| `GITHUB_APP_WEBHOOK_SECRET` in backend secret | Done (`aurora/staging/backend`) | Done (`aurora/infra/backend`) |

## Staging Deploy

PR: https://github.com/Arvo-AI/aurora-saas-prod/pull/46

Merging that PR applies terraform which sets:
```hcl
GITHUB_AUTH_MODE                        = "app"
GITHUB_APP_ID                           = "4088260"
GITHUB_APP_CLIENT_ID                    = "Iv23liK3x9Wh7SF8Vxpo"
NEXT_PUBLIC_GITHUB_APP_SLUG             = "arvo-ai-staging"
GITHUB_APP_WEBHOOK_URL                  = "https://${local.api_host}/github/webhook"
GITHUB_APP_SETUP_URL                    = "https://${local.frontend_host}/github"
NEXT_PUBLIC_ENABLE_INCIDENT_PREVENTION  = "true"
```

Requires OSS branch merged to main first (staging pulls images from GHCR).

## Production Deploy

After OSS branch merges to main:

```bash
# 1. Note the 7-char SHA
git fetch origin && git log origin/main --oneline -1

# 2. Wait for OSS CI images (~5-8 min)
docker manifest inspect ghcr.io/arvo-ai/aurora-server:sha-<7chars>

# 3. Build frontend overlay
gh workflow run "Deploy to prod" --repo Arvo-AI/aurora-saas-prod -f oss_ref=<7chars>

# 4. Update values.generated.yaml
```

Add to `config:` section in `deploy/helm/aurora/values.generated.yaml`:
```yaml
  GITHUB_AUTH_MODE: "app"
  GITHUB_APP_ID: "<prod app id>"
  GITHUB_APP_CLIENT_ID: "<prod client id>"
  NEXT_PUBLIC_GITHUB_APP_SLUG: "arvo-ai"
  GITHUB_APP_WEBHOOK_URL: "https://api.aurora-ai.net/github/webhook"
  GITHUB_APP_SETUP_URL: "https://api.aurora-ai.net/github/app/install/callback"
  NEXT_PUBLIC_ENABLE_INCIDENT_PREVENTION: "true"
```

```bash
# 5. Helm deploy
helm upgrade aurora-oss ./deploy/helm/aurora \
  --namespace aurora --reuse-values \
  --set image.tag=sha-<7chars> \
  --set frontendImage.tag=sha-<7chars> \
  --set config.GITHUB_AUTH_MODE=app \
  --set config.GITHUB_APP_ID=<prod-id> \
  --set config.GITHUB_APP_CLIENT_ID=<prod-client-id> \
  --set config.NEXT_PUBLIC_GITHUB_APP_SLUG=arvo-ai \
  --set "config.GITHUB_APP_WEBHOOK_URL=https://api.aurora-ai.net/github/webhook" \
  --set "config.GITHUB_APP_SETUP_URL=https://api.aurora-ai.net/github/app/install/callback" \
  --set config.NEXT_PUBLIC_ENABLE_INCIDENT_PREVENTION=true \
  --kube-context gke_aurora-saas-prod_us-west1_aurora-prod

# 6. Force-sync the ESO secret (picks up GITHUB_APP_WEBHOOK_SECRET)
kubectl annotate es aurora-secret-backend -n aurora force-sync=$(date +%s) --overwrite \
  --context gke_aurora-saas-prod_us-west1_aurora-prod
```

## What Happens Automatically

- **DB migration**: `change_gating_enabled` column added to `connected_repos` on boot (idempotent)
- **Existing OAuth users**: unaffected — tokens still honored in `app` mode
- **Redis**: uses existing Memorystore for dedup keys
- **No new Python deps**

## Kill Switch

```bash
helm upgrade aurora-oss ./deploy/helm/aurora \
  --namespace aurora --reuse-values \
  --set config.NEXT_PUBLIC_ENABLE_INCIDENT_PREVENTION=false \
  --kube-context gke_aurora-saas-prod_us-west1_aurora-prod
```

## GitHub App Settings (UI)

Configure these in each App's settings page on github.com:

| Field | Staging (`arvo-ai-staging`) | Prod (`arvo-ai`) |
|-------|---------------------------|-------------------|
| Webhook URL | `https://api.infrapoo.org/github/webhook` | `https://api.aurora-ai.net/github/webhook` |
| Setup URL | `https://api.infrapoo.org/github/app/install/callback` | `https://api.aurora-ai.net/github/app/install/callback` |
| Webhook secret | (stored in AWS SM `aurora/system/github-app/webhook-secret`) | same |

## Verification

1. Install the App on Arvo-AI org: https://github.com/apps/arvo-ai-staging/installations/new (staging) or https://github.com/apps/arvo-ai/installations/new (prod)
2. Open a PR against a connected repo's default branch
3. Check celery logs: `kubectl logs -f deployment/aurora-aurora-oss-celery-worker -n aurora`
4. Look for: `change_gating: enqueued repo=... pr=... head_sha=...`
5. PR gets APPROVE (SAFE) or COMMENT (RISKY) review within ~1-2 min
