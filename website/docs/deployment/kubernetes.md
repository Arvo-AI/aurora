---
sidebar_position: 2
---

# Kubernetes Deployment

Deploy Aurora on any Kubernetes cluster using Helm.

## Prerequisites

You need a Kubernetes cluster with:
- **4+ CPU cores** and **12+ GB RAM** allocatable
- A **working default StorageClass** (GKE and AKS have this out of the box; EKS needs setup)
- `kubectl` connected to the cluster

**Don't have a cluster yet?** Follow one of these setup guides first:
- **AWS EKS:** [EKS Cluster Setup for Aurora](./eks-setup) — includes CSI driver and S3 bucket creation
- **GCP GKE / Azure AKS:** Create a cluster with default settings — both include working storage out of the box

### Required tools

| Tool | Install |
|------|---------|
| `kubectl` | [kubernetes.io/docs/tasks/tools](https://kubernetes.io/docs/tasks/tools/) |
| `helm` | [helm.sh/docs/intro/install](https://helm.sh/docs/intro/install/) |
| `yq` | [github.com/mikefarah/yq#install](https://github.com/mikefarah/yq#install) |
| `openssl` | Usually pre-installed. macOS: `brew install openssl` |

### Clone the repo

```bash
git clone https://github.com/arvo-ai/aurora.git
cd aurora
```

## Step 1: Preflight Check

Verify your cluster is ready:

```bash
./deploy/preflight.sh
```

This checks: kubectl connection, required tools, node resources, StorageClass, CSI driver health (EKS), and ingress controller. **Fix any `FAIL` items before continuing.**

## Step 2: Prepare S3 Storage & LLM API Key

The deploy script will ask for these. Have them ready.

### S3-compatible storage

Aurora stores files in S3-compatible storage. If you followed the [EKS setup guide](./eks-setup), you already have this.

| Provider | Endpoint URL | Notes |
|----------|-------------|-------|
| AWS S3 | `https://s3.amazonaws.com` | [EKS guide](./eks-setup) covers bucket creation |
| Cloudflare R2 | `https://<ACCOUNT_ID>.r2.cloudflarestorage.com` | Region: `auto` |
| GCS (S3 interop) | `https://storage.googleapis.com` | Create HMAC keys |
| MinIO | `http://minio:9000` | Self-hosted |

### LLM API key

Aurora needs an LLM provider for its AI agents. Pick one:

| Provider | Get a key | What to enter when prompted |
|----------|----------|----------------------------|
| **OpenRouter** (recommended) | [openrouter.ai/keys](https://openrouter.ai/keys) | Provider: `openrouter`, Key: `sk-or-v1-...` |
| OpenAI | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) | Provider: `openai`, Key: `sk-...` |
| Anthropic | [console.anthropic.com](https://console.anthropic.com/) | Provider: `anthropic`, Key: `sk-ant-...` |
| Google | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | Provider: `google`, Key: `AI...` |

OpenRouter is recommended — one key gives access to all models (GPT-4o, Claude, Gemini, etc.).

## Step 3: Deploy Aurora

### Option A: Interactive deploy script (recommended)

```bash
# Use prebuilt images from GHCR (no Docker build needed — fastest option)
./deploy/k8s-deploy.sh --skip-build
```

The script will prompt you for several values. Here's what to enter:

| Prompt | What to enter |
|--------|--------------|
| Container registry | `ghcr.io/arvo-ai` |
| Bucket name | Your S3 bucket name from Step 5 |
| Endpoint URL | `https://s3.amazonaws.com` (or your provider's endpoint) |
| Region | `us-east-1` (or your bucket's region) |
| Access key / Secret key | From Step 5 |
| LLM Provider | `openrouter` (or whichever you chose) |
| API key | Your key from Step 5 |
| Environment | `staging` (or `production`) |

The script will:
1. Install nginx ingress controller if missing
2. Detect the ingress IP (resolves AWS ELB hostnames to IPs automatically)
3. Generate `values.generated.yaml` with your config + auto-generated secrets
4. Deploy with Helm
5. Initialize Vault (init, unseal, KV engine, app token)

**After deployment**, the script prints the access URLs. Open the frontend URL in your browser.

### Option B: Manual Helm deployment

For more control, deploy step by step:

```bash
# 1. Create values file
cp deploy/helm/aurora/values.yaml deploy/helm/aurora/values.generated.yaml
```

Edit `values.generated.yaml` — see [Configuration Reference](#configuration-reference) below for all options. At minimum, set:

```yaml
image:
  registry: "ghcr.io/arvo-ai"    # or your own registry
  tag: "latest"

config:
  NEXT_PUBLIC_BACKEND_URL: "http://api.aurora-oss.<IP>.nip.io"
  NEXT_PUBLIC_WEBSOCKET_URL: "ws://ws.aurora-oss.<IP>.nip.io"
  FRONTEND_URL: "http://aurora-oss.<IP>.nip.io"
  STORAGE_BUCKET: "my-bucket"
  STORAGE_ENDPOINT_URL: "https://s3.amazonaws.com"
  STORAGE_REGION: "us-east-1"

secrets:
  db:
    POSTGRES_PASSWORD: ""         # openssl rand -base64 32
  backend:
    STORAGE_ACCESS_KEY: ""
    STORAGE_SECRET_KEY: ""
  app:
    FLASK_SECRET_KEY: ""          # openssl rand -base64 32
    AUTH_SECRET: ""               # openssl rand -base64 32
    SEARXNG_SECRET: ""            # openssl rand -base64 32
  llm:
    OPENROUTER_API_KEY: ""        # or OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.

ingress:
  hosts:
    frontend: "aurora-oss.<IP>.nip.io"
    api: "api.aurora-oss.<IP>.nip.io"
    ws: "ws.aurora-oss.<IP>.nip.io"
```

```bash
# 2. Install nginx ingress (if not already installed)
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.8.1/deploy/static/provider/cloud/deploy.yaml

# 3. Deploy
helm upgrade --install aurora-oss ./deploy/helm/aurora \
  --namespace aurora-oss --create-namespace --reset-values \
  -f deploy/helm/aurora/values.generated.yaml

# 4. Set up Vault — see Step 4 below
```

### Building your own images

If you need custom images instead of GHCR prebuilt ones:

```bash
# Build and push with make (reads registry from values.generated.yaml)
make deploy

# Or build manually
GIT_SHA=$(git rev-parse --short HEAD)
REGISTRY="your-registry.example.com"

docker buildx build ./server --target=prod --platform linux/amd64 --push \
  -t $REGISTRY/aurora-server:$GIT_SHA

docker buildx build ./client --target=prod --platform linux/amd64 --push \
  --build-arg NEXT_PUBLIC_BACKEND_URL=http://api.aurora-oss.<IP>.nip.io \
  --build-arg NEXT_PUBLIC_WEBSOCKET_URL=ws://ws.aurora-oss.<IP>.nip.io \
  -t $REGISTRY/aurora-frontend:$GIT_SHA
```

:::warning NEXT_PUBLIC_* variables are baked at build time
The frontend image must be rebuilt when `NEXT_PUBLIC_BACKEND_URL` or `NEXT_PUBLIC_WEBSOCKET_URL` change. Prebuilt GHCR images use runtime injection via `env-config.js`, so this only applies to custom builds.
:::

## Step 4: Vault Setup

### For production: KMS Auto-Unseal (recommended)

See [Vault Auto-Unseal with KMS](./vault-kms-setup) for setup. This eliminates manual unsealing after pod restarts.

### For testing: Manual Vault init

If the deploy script handled Vault automatically, you're done. If you need to do it manually, **run each command one at a time** (copy-pasting multiple heredoc commands at once breaks in zsh):

```bash
# Initialize (save the Unseal Key and Root Token!)
kubectl -n aurora-oss exec -it statefulset/aurora-oss-vault -- \
  vault operator init -key-shares=1 -key-threshold=1

# Unseal
kubectl -n aurora-oss exec -it statefulset/aurora-oss-vault -- \
  vault operator unseal <UNSEAL_KEY>

# Login
kubectl -n aurora-oss exec statefulset/aurora-oss-vault -- sh -c \
  'export VAULT_ADDR=http://127.0.0.1:8200 && echo "<ROOT_TOKEN>" | vault login -'

# Enable KV engine
kubectl -n aurora-oss exec statefulset/aurora-oss-vault -- sh -c \
  'export VAULT_ADDR=http://127.0.0.1:8200 && vault secrets enable -path=aurora kv-v2'

# Create policy
kubectl -n aurora-oss exec statefulset/aurora-oss-vault -- sh -c \
  'export VAULT_ADDR=http://127.0.0.1:8200 && vault policy write aurora-app - <<EOF
path "aurora/data/users/*" { capabilities = ["create","read","update","delete","list"] }
path "aurora/metadata/users/*" { capabilities = ["list","read","delete"] }
path "aurora/metadata/" { capabilities = ["list"] }
path "aurora/metadata/users" { capabilities = ["list"] }
EOF'

# Create app token
kubectl -n aurora-oss exec statefulset/aurora-oss-vault -- sh -c \
  'export VAULT_ADDR=http://127.0.0.1:8200 && vault token create -policy=aurora-app -ttl=0'
```

Put the app token in `values.generated.yaml` under `secrets.backend.VAULT_TOKEN`, then:

```bash
helm upgrade aurora-oss ./deploy/helm/aurora \
  --namespace aurora-oss --reset-values \
  -f deploy/helm/aurora/values.generated.yaml
```

:::tip
With manual unsealing, you'll need to run `vault operator unseal <UNSEAL_KEY>` after every Vault pod restart.
:::

## Step 5: Verify

```bash
# All pods should be Running
kubectl get pods -n aurora-oss

# Check ingress
kubectl get ingress -n aurora-oss

# Test the API
curl http://api.aurora-oss.<IP>.nip.io/health/
```

Open the frontend URL in your browser. The first user to register becomes the admin.

## EKS: Fixing nip.io URLs

AWS load balancers return a hostname instead of an IP. The deploy script resolves this automatically, but if your URLs aren't working, see the [EKS setup guide](./eks-setup) or resolve manually:

```bash
# Resolve the ELB hostname to an IP
ELB_HOST=$(kubectl get svc -n ingress-nginx ingress-nginx-controller \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')
INGRESS_IP=$(dig +short "$ELB_HOST" | head -1)

# Update values
VALUES="deploy/helm/aurora/values.generated.yaml"
yq -i ".ingress.hosts.frontend = \"aurora-oss.${INGRESS_IP}.nip.io\"" "$VALUES"
yq -i ".ingress.hosts.api = \"api.aurora-oss.${INGRESS_IP}.nip.io\"" "$VALUES"
yq -i ".ingress.hosts.ws = \"ws.aurora-oss.${INGRESS_IP}.nip.io\"" "$VALUES"
yq -i ".config.NEXT_PUBLIC_BACKEND_URL = \"http://api.aurora-oss.${INGRESS_IP}.nip.io\"" "$VALUES"
yq -i ".config.NEXT_PUBLIC_WEBSOCKET_URL = \"ws://ws.aurora-oss.${INGRESS_IP}.nip.io\"" "$VALUES"
yq -i ".config.FRONTEND_URL = \"http://aurora-oss.${INGRESS_IP}.nip.io\"" "$VALUES"

# Redeploy
helm upgrade aurora-oss ./deploy/helm/aurora \
  --namespace aurora-oss --reset-values -f "$VALUES"
```

:::warning
ELB IPs can change. For production, set up real DNS CNAME records pointing to the ELB hostname instead of nip.io.
:::

## DNS & TLS

### DNS Configuration

For testing, [nip.io](https://nip.io) works without any DNS setup. For production, create DNS records:

```
aurora.yourdomain.com      CNAME  <ingress-hostname-or-IP>
api.aurora.yourdomain.com  CNAME  <ingress-hostname-or-IP>
ws.aurora.yourdomain.com   CNAME  <ingress-hostname-or-IP>
```

### TLS with cert-manager (Let's Encrypt)

```bash
# Install cert-manager
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.13.3/cert-manager.yaml
kubectl wait --for=condition=ready pod -l app.kubernetes.io/instance=cert-manager -n cert-manager --timeout=120s

# Create issuer
cat <<EOF | kubectl apply -f -
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: admin@yourdomain.com
    privateKeySecretRef:
      name: letsencrypt-prod
    solvers:
    - http01:
        ingress:
          class: nginx
EOF
```

Then in `values.generated.yaml`:
```yaml
ingress:
  tls:
    enabled: true
    certManager:
      enabled: true
      issuer: "letsencrypt-prod"
      email: "admin@yourdomain.com"
```

### Manual TLS certificate

```bash
kubectl create secret tls aurora-tls \
  --cert=fullchain.crt --key=privkey.key -n aurora-oss
```

```yaml
ingress:
  tls:
    enabled: true
    secretName: "aurora-tls"
```

## Private / VPN Deployment

For internal-only access (no public internet exposure):

```bash
./deploy/k8s-deploy.sh --private
```

This prompts for a private hostname (e.g. `aurora.internal`), provisions an internal load balancer, and bakes the hostname into the frontend.

**DNS:** Your hostname must resolve on the VPN. Options: split-horizon DNS, Tailscale MagicDNS, or `/etc/hosts` entries.

**Internal LB annotations** (applied automatically by the script):

| Cloud | Annotation |
|-------|------------|
| GKE | `cloud.google.com/load-balancer-type: "Internal"` |
| EKS | `service.beta.kubernetes.io/aws-load-balancer-internal: "true"` |
| AKS | `service.beta.kubernetes.io/azure-load-balancer-internal: "true"` |

## Local Kubernetes

For local dev on OrbStack, Docker Desktop, or Rancher Desktop:

```bash
./deploy/k8s-deploy.sh --local
```

This skips registry push, enables built-in MinIO for S3 storage, and builds images locally.

## Upgrading

```bash
# Config-only change
helm upgrade aurora-oss ./deploy/helm/aurora \
  --reset-values -f deploy/helm/aurora/values.generated.yaml -n aurora-oss

# New code/images
git pull && make deploy

# Rollback
helm rollback aurora-oss -n aurora-oss
```

## Uninstalling

```bash
helm uninstall aurora-oss -n aurora-oss
kubectl delete namespace aurora-oss
```

## Troubleshooting

### StatefulSets stuck in `Pending`

Pods with no events usually mean PVCs can't bind.

```bash
kubectl get pvc -n aurora-oss                              # check PVC status
kubectl get storageclass                                    # is there a default?
```

**EKS:** This is almost always a missing storage driver. See the [EKS setup guide](./eks-setup) — specifically the EBS CSI Driver section and its troubleshooting.

**After fixing storage**, delete stuck PVCs and pods to force recreation:
```bash
kubectl delete pvc --all -n aurora-oss
kubectl delete pods --all -n aurora-oss
```

### Frontend loads but API returns 403

1. Check backend is reachable: `curl http://api.aurora-oss.<IP>.nip.io/health/`
2. Check URLs in frontend config: `curl http://aurora-oss.<IP>.nip.io/env-config.js`
3. Check backend logs: `kubectl logs -n aurora-oss deploy/aurora-oss-server --tail=20`

If you see `RBAC denied`, restart services:
```bash
kubectl rollout restart deployment/aurora-oss-server \
  deployment/aurora-oss-celery-worker \
  deployment/aurora-oss-chatbot -n aurora-oss
```

### Vault sealed after restart

Without KMS auto-unseal, Vault seals on every pod restart:
```bash
kubectl -n aurora-oss exec -it statefulset/aurora-oss-vault -- \
  vault operator unseal <UNSEAL_KEY>
```

For production, set up [KMS auto-unseal](./vault-kms-setup).

### AWS quota limits

See [EKS Setup — Troubleshooting](./eks-setup#troubleshooting).

### Image pull errors

```bash
kubectl get events -n aurora-oss --sort-by='.lastTimestamp'
```

If using GHCR prebuilt images, no registry auth is needed (images are public).

### Frontend CrashLoopBackOff

If logs show `Permission denied` on `env-config.js`, ensure the Dockerfile sets ownership:
```bash
kubectl logs -n aurora-oss deploy/aurora-oss-frontend --tail=10
```

## Configuration Reference

### Object Storage

Aurora requires S3-compatible storage. Options:

| Provider | `STORAGE_ENDPOINT_URL` | Notes |
|----------|----------------------|-------|
| AWS S3 | `https://s3.amazonaws.com` | Create bucket + IAM user |
| MinIO | `http://minio:9000` | Self-hosted, set `STORAGE_USE_SSL: "false"` |
| Cloudflare R2 | `https://<ACCOUNT_ID>.r2.cloudflarestorage.com` | Set region to `auto` |
| GCS (S3 interop) | `https://storage.googleapis.com` | Create HMAC keys |

### Internal Service Discovery

These are auto-generated by Helm if left empty:
- `POSTGRES_HOST` → `<release>-postgres`
- `REDIS_URL` → `redis://<release>-redis:6379/0`
- `WEAVIATE_HOST` → `<release>-weaviate`
- `VAULT_ADDR` → `http://<release>-vault:8200`

Leave them empty unless using external managed services.

### All Values

See `deploy/helm/aurora/values.yaml` for the complete list of configuration options.
