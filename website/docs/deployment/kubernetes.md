---
sidebar_position: 2
---

# Kubernetes Deployment

Deploy Aurora on any Kubernetes cluster using Helm.

## Prerequisites

### Cluster requirements

- **4+ CPU cores** and **12+ GB RAM** allocatable across your nodes
- A **working default StorageClass** (GKE and AKS have this out of the box; EKS needs the [EBS CSI driver](./eks-setup))
- **Outbound internet** from nodes (to pull container images from public registries)
- `kubectl` connected to the cluster

**Don't have a cluster yet?**
- **AWS EKS:** [EKS Cluster Setup for Aurora](./eks-setup)
- **GCP GKE / Azure AKS:** Create a cluster with default settings

:::note Third-party images
Aurora deploys several third-party images from public registries. Your nodes must be able to pull: `postgres:15-alpine`, `redis:7-alpine`, `hashicorp/vault:1.15`, `cr.weaviate.io/semitechnologies/weaviate:1.27.6`, `searxng/searxng:*`, `memgraph/memgraph-mage:3.8.1`, `cr.weaviate.io/semitechnologies/transformers-inference:*`. Optional components (e.g. `services.minio.enabled: true`) may pull additional images. For air-gapped clusters, mirror these to a private registry and review enabled services in your `values.yaml`.
:::

### Required tools

| Tool | Install |
|------|---------|
| `kubectl` | [kubernetes.io/docs/tasks/tools](https://kubernetes.io/docs/tasks/tools/) |
| `helm` | [helm.sh/docs/intro/install](https://helm.sh/docs/intro/install/) |
| `yq` | [github.com/mikefarah/yq#install](https://github.com/mikefarah/yq#install) |
| `openssl` | Usually pre-installed. macOS: `brew install openssl` |

### S3-compatible storage

Aurora stores files in S3-compatible object storage. Have your bucket details ready.

| Provider | Endpoint URL | Notes |
|----------|-------------|-------|
| AWS S3 | `https://s3.amazonaws.com` | [EKS guide](./eks-setup) covers bucket creation |
| GCS (S3 interop) | `https://storage.googleapis.com` | [Create HMAC keys](https://cloud.google.com/storage/docs/authentication/hmackeys) |
| Cloudflare R2 | `https://<ACCOUNT_ID>.r2.cloudflarestorage.com` | Region: `auto` |
| MinIO | `http://minio:9000` | Self-hosted |

### LLM provider

Aurora's AI agents need an LLM. Have your provider credentials ready before deploying.

**Quickstart:** Use [OpenRouter](https://openrouter.ai/keys) -- one API key, no model prefix config needed.

For full setup details (all providers, model configuration, Vertex AI/Bedrock auth), see the **[LLM Providers guide](../integrations/llm-providers)**.

The key values you'll set during deployment:

```yaml
config:
  LLM_PROVIDER_MODE: "openrouter"  # or "vertex", "anthropic", "openai", "bedrock"
  # For non-OpenRouter providers, also set:
  # MAIN_MODEL: "vertex/gemini-2.5-pro"
  # RCA_MODEL: "vertex/gemini-2.5-flash"
secrets:
  llm:
    OPENROUTER_API_KEY: ""  # or OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.
```

For Vertex AI on Kubernetes, store credentials as a secret and reference with `existingSecret`:

```bash
kubectl create secret generic aurora-llm-vertex -n aurora-oss \
  --from-literal=VERTEX_AI_PROJECT="your-gcp-project-id" \
  --from-literal=VERTEX_AI_LOCATION="us-central1" \
  --from-literal=VERTEX_AI_SERVICE_ACCOUNT_JSON="$(cat service-account.json)"
```

```yaml
secrets:
  llm:
    existingSecret: "aurora-llm-vertex"
```

---

## Choose Your Deployment Method

| Method | Best for | What it does |
|--------|----------|--------------|
| [**Interactive Deploy Script**](#interactive-deploy-script) | First-time setup, getting running quickly | Script handles secrets, ingress, Vault automatically |
| [**Manual Helm Deployment**](#manual-helm-deployment) | GitOps, custom configs, version-controlled values | You edit values and run Helm yourself |

Both produce the same result. The script requires cloning the repo. Manual can use either a local clone or a published chart from a Helm registry.

---

## Interactive Deploy Script

Best for: first-time setup, getting Aurora running quickly.

### 1. Clone the repo

```bash
git clone https://github.com/arvo-ai/aurora.git
cd aurora
```

### 2. Preflight check

```bash
./deploy/preflight.sh
```

Fix any `FAIL` items before continuing.

### 3. Run the deploy script

```bash
# Standard (public LB, nip.io URLs for quick testing):
./deploy/k8s-deploy.sh --skip-build

# Private/VPN (internal LB, your own hostname):
./deploy/k8s-deploy.sh --private --skip-build

# Build your own images instead of using prebuilt GHCR ones:
./deploy/k8s-deploy.sh --private
```

The script prompts for: container registry, storage bucket, LLM provider/key, and (for `--private`) a hostname. It then:

1. Installs nginx ingress controller if missing
2. Generates secrets and `values.generated.yaml`
3. Deploys with Helm
4. Initializes and configures Vault
5. Prints access URLs

**After deployment**, open the frontend URL. The first user to register becomes the org admin.

You're done. Skip to [Post-Deploy: DNS & TLS](#dns--tls) if you need to configure a real domain or HTTPS.

---

## Manual Helm Deployment

Best for: GitOps workflows, custom configurations, teams that manage values files in version control.

### 1. Get the chart

**Option A -- Clone the repo (gives you deploy scripts + local chart):**

```bash
git clone https://github.com/arvo-ai/aurora.git
cd aurora
cp deploy/helm/aurora/values.yaml deploy/helm/aurora/values.generated.yaml
```

If you cloned the repo, run the preflight check to verify cluster readiness:

```bash
./deploy/preflight.sh
```

**Option B -- Published chart (no clone needed, for GitOps tooling):**

```bash
# Traditional Helm repo
helm repo add aurora https://raw.githubusercontent.com/Arvo-AI/aurora/gh-pages
helm repo update
helm show values aurora/aurora-oss > values.generated.yaml

# Or OCI registry (ArgoCD, Flux)
helm show values oci://ghcr.io/arvo-ai/charts/aurora-oss > values.generated.yaml
```

### 2. Edit values.generated.yaml

Set at minimum:

```yaml
image:
  registry: "ghcr.io/arvo-ai"   # prebuilt public images (no auth needed)
  tag: "latest"                  # or "edge" for latest main, or "sha-<7char>"

config:
  NEXT_PUBLIC_BACKEND_URL: "https://api.yourdomain.com"
  NEXT_PUBLIC_WEBSOCKET_URL: "wss://ws.yourdomain.com"
  FRONTEND_URL: "https://yourdomain.com"
  STORAGE_BUCKET: "my-bucket"
  STORAGE_ENDPOINT_URL: "https://s3.amazonaws.com"
  STORAGE_REGION: "us-east-1"
  LLM_PROVIDER_MODE: "openrouter"     # see "LLM provider" above
  # For non-OpenRouter providers, uncomment and set:
  # MAIN_MODEL: "vertex/gemini-2.5-pro"
  # RCA_MODEL: "vertex/gemini-2.5-flash"

secrets:
  db:
    POSTGRES_PASSWORD: ""              # openssl rand -base64 32
  backend:
    STORAGE_ACCESS_KEY: ""             # S3/GCS HMAC key (optional with IRSA/pod identity)
    STORAGE_SECRET_KEY: ""
  app:
    FLASK_SECRET_KEY: ""               # openssl rand -base64 32
    AUTH_SECRET: ""                     # openssl rand -base64 32
    SEARXNG_SECRET: ""                 # openssl rand -base64 32
    INTERNAL_API_SECRET: ""            # openssl rand -base64 32
  llm:
    OPENROUTER_API_KEY: ""             # or OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.
  # Or use pre-existing K8s secrets instead -- see "Using Pre-Existing Kubernetes Secrets" below

ingress:
  hosts:
    frontend: "yourdomain.com"
    api: "api.yourdomain.com"
    ws: "ws.yourdomain.com"
  # For private/VPN deployments (internal LB, not internet-facing):
  # internal: true
  # annotations:
  #   cloud.google.com/load-balancer-type: "Internal"                  # GKE
  #   service.beta.kubernetes.io/aws-load-balancer-internal: "true"    # EKS
  #   service.beta.kubernetes.io/azure-load-balancer-internal: "true"  # AKS
```

For LLM model configuration details (required for non-OpenRouter providers), see the [LLM Providers guide](../integrations/llm-providers). To use externally-managed secrets instead of inline values, see [Using Pre-Existing Kubernetes Secrets](#using-pre-existing-kubernetes-secrets).

:::tip No domain yet?
[nip.io](https://nip.io) is a free wildcard DNS service -- any hostname like `app.10.0.0.1.nip.io` resolves to that embedded IP automatically. Use it for quick testing without DNS config. Replace `yourdomain.com` with `aurora-oss.<INGRESS_IP>.nip.io` (you'll get the IP after installing the ingress controller).
:::

:::note Private/VPN deployments
If using `internal: true`, your hostnames must resolve on the VPN or internal network. Options: split-horizon DNS, Tailscale MagicDNS, or `/etc/hosts` entries.
:::

:::note Guardrails require an LLM
AI safety guardrails are enabled by default (`GUARDRAILS_ENABLED: "true"`). They **fail closed** on any LLM error, blocking all shell commands. Ensure your LLM key is valid, or set `GUARDRAILS_ENABLED: "false"`.
:::

### 3. Install ingress controller (if not present)

```bash
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.8.1/deploy/static/provider/cloud/deploy.yaml
kubectl rollout status deployment/ingress-nginx-controller -n ingress-nginx --timeout=120s
```

### 4. Create namespace

```bash
kubectl create namespace aurora-oss
```

If using `existingSecret` for any secret group (e.g., Vertex AI), create those secrets now before deploying.

### 5. Deploy

```bash
# From local clone:
helm upgrade --install aurora-oss ./deploy/helm/aurora \
  --namespace aurora-oss --reset-values \
  -f deploy/helm/aurora/values.generated.yaml

# From published chart:
helm upgrade --install aurora-oss aurora/aurora-oss \
  --namespace aurora-oss --reset-values \
  -f values.generated.yaml

# From OCI:
helm upgrade --install aurora-oss oci://ghcr.io/arvo-ai/charts/aurora-oss \
  --namespace aurora-oss --reset-values \
  -f values.generated.yaml
```

### 6. Initialize Vault

Aurora uses Vault to store user credentials (cloud tokens, API keys). This step is **required** -- without it, users can't connect integrations.

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

Put the app token in `values.generated.yaml` under `secrets.backend.VAULT_TOKEN`, then redeploy:

```bash
helm upgrade aurora-oss ./deploy/helm/aurora \
  --namespace aurora-oss --reset-values \
  -f deploy/helm/aurora/values.generated.yaml
```

:::tip
With manual unsealing, you must run `vault operator unseal` after every Vault pod restart. For production, use [KMS auto-unseal](./vault-kms-setup) to eliminate this.
:::

:::info Alternative: AWS Secrets Manager
You can use AWS Secrets Manager instead of Vault by setting `SECRETS_BACKEND=aws_secrets_manager`. This requires no Vault initialization or unsealing. See the [AWS Secrets Manager guide](../configuration/aws-secrets-manager) for full setup.
:::

### 7. Verify

```bash
kubectl get pods -n aurora-oss          # all Running
kubectl get ingress -n aurora-oss       # hosts + IP assigned
curl http://api.yourdomain.com/health/  # {"status": "ok"}
```

Open the frontend URL. The first user to register becomes the org admin.

**You're done.** Aurora is running. The sections below are optional configuration for production hardening.

---

## Optional: DNS & TLS

By default, Aurora works over HTTP with nip.io or `/etc/hosts`. Set up DNS and TLS when you're ready to give users a real URL with HTTPS.

### DNS

Point your three hostnames at the ingress load balancer IP:

```bash
# Get the ingress IP (or hostname on EKS)
kubectl get svc -n ingress-nginx ingress-nginx-controller \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
```

```
yourdomain.com      A  <INGRESS_IP>
api.yourdomain.com  A  <INGRESS_IP>
ws.yourdomain.com   A  <INGRESS_IP>
```

### TLS with cert-manager (Let's Encrypt)

Only needed if the deployment is internet-facing and you want automatic HTTPS certificates.

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
          ingressClassName: nginx
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
```

### Manual TLS certificate

For private deployments with an internal CA or self-signed cert:

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

---

## Ingress Controller

Aurora's chart is **controller-agnostic** -- it uses the standard `ingressClassName` field. Set `ingress.className` to match your controller.

| Controller | `className` | Install |
|-----------|-------------|---------|
| NGINX Ingress | `nginx` | `helm install ingress-nginx ingress-nginx/ingress-nginx -n ingress-nginx --create-namespace` |
| Traefik | `traefik` | Often bundled with k3s |
| AWS ALB | `alb` | [AWS Load Balancer Controller](https://kubernetes-sigs.github.io/aws-load-balancer-controller/) |
| HAProxy | `haproxy` | `helm install haproxy haproxytech/kubernetes-ingress -n haproxy --create-namespace` |

### Required controller settings

Regardless of controller, ensure these are configured:

| Setting | Value | Why |
|---------|-------|-----|
| Read/send timeout | `3600s` | RCA analysis can run 30+ minutes |
| HTTP version | `1.1` | Required for WebSocket upgrade |
| Max body size | `50m` | File uploads |

When `className` is `nginx`, these are auto-applied as annotations by the chart.

### MCP Ingress {#mcp-ingress}

The MCP server runs on port 8811. By default it's reachable at `https://api.<domain>/mcp`. For local-only access:

```bash
kubectl port-forward svc/aurora-oss-mcp 8811:8811 -n aurora-oss
```

:::warning Security - unauthenticated MCP = remote code execution
Exposing the MCP server without authentication allows unauthenticated remote code execution through the MCP protocol. For a dedicated hostname, set `ingress.hosts.mcp` in your values but always place an auth proxy (e.g. OAuth2 Proxy, Keycloak Gatekeeper, or nginx `auth_request`) in front of any internet-facing MCP ingress.
:::

## Local Kubernetes

For local dev on OrbStack, Docker Desktop, or Rancher Desktop:

```bash
./deploy/k8s-deploy.sh --local
```

This builds images locally (no push), enables built-in MinIO for S3 storage, and uses nip.io URLs.

## Upgrading

```bash
# Image-only update (reuse all existing config):
helm upgrade aurora-oss ./deploy/helm/aurora \
  --namespace aurora-oss --reuse-values \
  --set image.tag=sha-<7char>

# Full config update:
helm upgrade aurora-oss ./deploy/helm/aurora \
  --namespace aurora-oss --reset-values \
  -f deploy/helm/aurora/values.generated.yaml

# Rollback:
helm rollback aurora-oss -n aurora-oss
```

## Building Custom Images

If you need images in a private registry instead of GHCR:

```bash
# Automated (reads registry from values.generated.yaml, builds multi-arch, pushes):
make deploy-build

# Manual:
GIT_SHA=$(git rev-parse --short HEAD)
REGISTRY="your-registry.example.com"

docker buildx build ./server --target=prod --platform linux/amd64 --push \
  -t $REGISTRY/aurora-server:$GIT_SHA

docker buildx build ./client --target=prod --platform linux/amd64 --push \
  -t $REGISTRY/aurora-frontend:$GIT_SHA
```

The frontend image uses runtime environment injection (`docker-entrypoint.sh` generates `env-config.js` at startup), so a single prebuilt image works with any domain without rebuilding.

## Uninstalling

```bash
helm uninstall aurora-oss -n aurora-oss
kubectl delete namespace aurora-oss
```

## Autoscaling {#autoscaling}

The chart includes optional HPAs for server and Celery workers. Disabled by default.

```yaml
autoscaling:
  server:
    enabled: true
    minReplicas: 2
    maxReplicas: 6
    targetCPU: 70
    targetMemory: 80
  celeryWorker:
    enabled: true
    minReplicas: 2
    maxReplicas: 6
    targetCPU: 70
```

When enabled, `replicaCounts.*` values are ignored -- HPA manages replicas.

### Per-pod concurrency

```yaml
config:
  GUNICORN_WORKERS: "4"    # 1 per vCPU
  GUNICORN_THREADS: "4"    # threads per worker
  DB_POOL_MAX: "20"        # >= workers x threads
  CELERY_CONCURRENCY: "4"  # parallel tasks per pod
```

Ensure PostgreSQL `max_connections` can handle `pods x DB_POOL_MAX`.

## Using Pre-Existing Kubernetes Secrets

For production deployments where secrets are managed externally (Terraform, External Secrets Operator, Sealed Secrets).

### Creating the secrets

Each secret is a standard Kubernetes `Opaque` secret whose keys match the environment variables Aurora expects:

```bash
kubectl create namespace aurora-oss  # if not already created

# Database secret
kubectl create secret generic my-db-secret -n aurora-oss \
  --from-literal=POSTGRES_USER=aurora \
  --from-literal=POSTGRES_PASSWORD="$(openssl rand -base64 32)"

# Backend secret
kubectl create secret generic my-backend-secret -n aurora-oss \
  --from-literal=VAULT_TOKEN="" \
  --from-literal=STORAGE_ACCESS_KEY="your-access-key" \
  --from-literal=STORAGE_SECRET_KEY="your-secret-key"

# App secret
kubectl create secret generic my-app-secret -n aurora-oss \
  --from-literal=FLASK_SECRET_KEY="$(openssl rand -base64 32)" \
  --from-literal=AUTH_SECRET="$(openssl rand -base64 32)" \
  --from-literal=SEARXNG_SECRET="$(openssl rand -base64 32)" \
  --from-literal=INTERNAL_API_SECRET="$(openssl rand -base64 32)"

# LLM secret
kubectl create secret generic my-llm-secret -n aurora-oss \
  --from-literal=OPENROUTER_API_KEY="sk-or-..."
```

### Referencing in values.generated.yaml

```yaml
secrets:
  db:
    existingSecret: "my-db-secret"
  backend:
    existingSecret: "my-backend-secret"
  app:
    existingSecret: "my-app-secret"
  llm:
    existingSecret: "my-llm-secret"
```

The Helm chart mounts the secret as `envFrom` -- each key in the secret becomes an environment variable in the pod. This is how Aurora knows which key is `VAULT_TOKEN` vs `STORAGE_ACCESS_KEY`.

### Required keys per group

| Group | Required Keys |
|-------|--------------|
| `db` | `POSTGRES_USER`, `POSTGRES_PASSWORD` |
| `backend` | `VAULT_TOKEN` (if using Vault). `STORAGE_ACCESS_KEY`/`STORAGE_SECRET_KEY` optional with IRSA. |
| `app` | `FLASK_SECRET_KEY`, `AUTH_SECRET`, `SEARXNG_SECRET`, `INTERNAL_API_SECRET` |
| `llm` | At least one LLM API key, or Vertex/Bedrock env vars |

Secrets must exist in the namespace **before** `helm install`. You can mix `existingSecret` for some groups and inline values for others.

## Troubleshooting

### StatefulSets stuck in `Pending`

```bash
kubectl get pvc -n aurora-oss
kubectl get storageclass
```

**EKS:** Almost always a missing EBS CSI driver. See [EKS setup guide](./eks-setup).

After fixing storage, delete stuck PVCs to force recreation:

```bash
kubectl delete pvc --all -n aurora-oss
kubectl delete pods --all -n aurora-oss
```

### Frontend loads but API returns 403

1. Check backend: `curl https://api.yourdomain.com/health/`
2. Check frontend config: `curl https://yourdomain.com/env-config.js`
3. Check logs: `kubectl logs -n aurora-oss deploy/aurora-oss-server --tail=20`

### Vault sealed after restart

```bash
kubectl -n aurora-oss exec -it statefulset/aurora-oss-vault -- \
  vault operator unseal <UNSEAL_KEY>
```

For production, use [KMS auto-unseal](./vault-kms-setup).

### Image pull errors

```bash
kubectl get events -n aurora-oss --sort-by='.lastTimestamp'
```

GHCR prebuilt images are public (no auth needed). If using a private registry, configure `image.pullSecrets` in values.

### EKS: nip.io URLs not working

AWS load balancers return a hostname, not an IP. The deploy script resolves this automatically. To fix manually:

```bash
ELB_HOST=$(kubectl get svc -n ingress-nginx ingress-nginx-controller \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')
INGRESS_IP=$(dig +short "$ELB_HOST" | head -1)
echo "Use $INGRESS_IP in your nip.io URLs"
```

:::warning
ELB IPs can change. For production, use real DNS CNAME records pointing to the ELB hostname.
:::

## Configuration Reference

See `deploy/helm/aurora/values.yaml` for the complete list of options with inline documentation.
