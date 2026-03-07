---
sidebar_position: 2
---

# Kubernetes Deployment

Deploy Aurora on Kubernetes using Helm.

## Prerequisites

- Kubernetes 1.25+ with a default StorageClass
- `kubectl`, `helm`, `docker` (with buildx), [`yq`](https://github.com/mikefarah/yq), `openssl`, and `python3`
- Container registry accessible from your cluster (Docker Hub, GHCR, Artifact Registry, ECR, etc.)
- Nginx Ingress Controller installed in your cluster
- TLS certificate (wildcard certificate recommended for subdomains)
- S3-compatible object storage (AWS S3, MinIO, Cloudflare R2, GCS, etc.)

**Cluster resources**: The default configuration requires approximately 4 CPU cores and 12GB memory across all pods. Adjust `resources` in `values.yaml` for smaller clusters.

## Quick Start

The fastest way to deploy Aurora is with the interactive deploy script:

```bash
# Cloud deployment (prompts for registry, storage, LLM key, etc.)
./deploy/k8s-deploy.sh

# Local Kubernetes (OrbStack, Docker Desktop, Rancher Desktop)
./deploy/k8s-deploy.sh --local
```

The script handles values generation, image building, Helm deployment, and Vault initialization in one command. Use `--skip-build` to skip image builds or `--skip-vault` if Vault is already set up. Use `--values-only` to generate the values file without deploying.

For manual setup or more control, follow the step-by-step sections below.

## Architecture

Aurora uses subdomain-based routing:

| Subdomain | Service | Description |
|-----------|---------|-------------|
| `aurora.example.com` | Frontend | Next.js web application |
| `api.aurora.example.com` | API Server | Flask REST API |
| `ws.aurora.example.com` | WebSocket | Real-time chatbot server |

## Configuration

### Step 1: Create your values file

Copy `values.yaml` to `values.generated.yaml` and edit it with your deployment settings:

```bash
cp deploy/helm/aurora/values.yaml deploy/helm/aurora/values.generated.yaml
```

**Files**:
- `values.yaml` — Default configuration (version controlled)
- `values.generated.yaml` — Your deployment config with secrets (**do not commit**)

### Step 2: Configure required values

Edit `values.generated.yaml` and update these sections:

**Container Registry** (top of file):
```yaml
image:
  registry: "us-docker.pkg.dev/my-project/aurora"  # Your registry (docker.io, ghcr.io, Artifact Registry, etc.)
  tag: "latest"                  # Version tag
```

**URLs** (in `config` section):
```yaml
config:
  # Subdomain-based routing
  NEXT_PUBLIC_BACKEND_URL: "https://api.yourdomain.com"
  NEXT_PUBLIC_WEBSOCKET_URL: "wss://ws.yourdomain.com"
  FRONTEND_URL: "https://yourdomain.com"

  # S3-Compatible Storage (REQUIRED)
  STORAGE_BUCKET: "my-aurora-storage"
  STORAGE_ENDPOINT_URL: "https://s3.amazonaws.com"
  STORAGE_REGION: "us-east-1"
```

**Secrets** (in `secrets` section, note the nested structure):
```yaml
secrets:
  # --- Database (secret-db.yaml) ---
  db:
    POSTGRES_PASSWORD: ""         # REQUIRED - Generate with: openssl rand -base64 32

  # --- Backend (secret-backend.yaml) ---
  backend:
    VAULT_TOKEN: ""               # Set after Vault initialization
    STORAGE_ACCESS_KEY: ""        # REQUIRED - Your S3 access key
    STORAGE_SECRET_KEY: ""        # REQUIRED - Your S3 secret key

  # --- Application (secret-app.yaml) ---
  app:
    FLASK_SECRET_KEY: ""          # REQUIRED - Generate with: openssl rand -base64 32
    AUTH_SECRET: ""               # REQUIRED - Generate with: openssl rand -base64 32
    SEARXNG_SECRET: ""            # REQUIRED - Generate with: openssl rand -base64 32

  # --- LLM API Keys (secret-llm.yaml) ---
  llm:
    OPENROUTER_API_KEY: ""        # Get from: https://openrouter.ai/keys
    # OR
    OPENAI_API_KEY: ""            # Get from: https://platform.openai.com/api-keys
```

See the comments in `values.yaml` for all available options.

#### Object Storage Setup

Aurora requires S3-compatible object storage. Choose the provider that fits your environment:

<details>
<summary><strong>AWS S3</strong></summary>

Create a bucket and IAM user with S3 access:

```yaml
config:
  STORAGE_BUCKET: "my-aurora-storage"
  STORAGE_ENDPOINT_URL: "https://s3.amazonaws.com"
  STORAGE_REGION: "us-east-1"

secrets:
  backend:
    STORAGE_ACCESS_KEY: "<AWS_ACCESS_KEY_ID>"
    STORAGE_SECRET_KEY: "<AWS_SECRET_ACCESS_KEY>"
```

</details>

<details>
<summary><strong>MinIO (self-hosted)</strong></summary>

Deploy MinIO in your cluster or use an existing instance:

```yaml
config:
  STORAGE_BUCKET: "aurora-storage"
  STORAGE_ENDPOINT_URL: "http://minio:9000"
  STORAGE_REGION: "us-east-1"
  STORAGE_USE_SSL: "false"
  STORAGE_VERIFY_SSL: "false"

secrets:
  backend:
    STORAGE_ACCESS_KEY: "<MINIO_ACCESS_KEY>"
    STORAGE_SECRET_KEY: "<MINIO_SECRET_KEY>"
```

</details>

<details>
<summary><strong>Cloudflare R2</strong></summary>

Create an R2 bucket and API token in the Cloudflare dashboard:

```yaml
config:
  STORAGE_BUCKET: "aurora-storage"
  STORAGE_ENDPOINT_URL: "https://<ACCOUNT_ID>.r2.cloudflarestorage.com"
  STORAGE_REGION: "auto"

secrets:
  backend:
    STORAGE_ACCESS_KEY: "<R2_ACCESS_KEY_ID>"
    STORAGE_SECRET_KEY: "<R2_SECRET_ACCESS_KEY>"
```

</details>

<details>
<summary><strong>GCP: Google Cloud Storage (S3 interop)</strong></summary>

GCS supports S3-compatible access via HMAC keys. Create a bucket and HMAC credentials from the [Cloud Console](https://console.cloud.google.com/storage) or using `gcloud`:

```bash
# Create a GCS bucket
gcloud storage buckets create gs://aurora-storage-<PROJECT_ID> \
  --location=us-central1 \
  --uniform-bucket-level-access

# Create HMAC keys (requires a service account, not a user account)
gcloud storage hmac create <SERVICE_ACCOUNT>@<PROJECT_ID>.iam.gserviceaccount.com --format="json"
# Save the accessId and secret from the output

# Grant the service account access to the bucket
gcloud storage buckets add-iam-policy-binding gs://aurora-storage-<PROJECT_ID> \
  --member=serviceAccount:<SERVICE_ACCOUNT>@<PROJECT_ID>.iam.gserviceaccount.com \
  --role=roles/storage.objectAdmin
```

Then in `values.generated.yaml`:
```yaml
config:
  STORAGE_BUCKET: "aurora-storage-<PROJECT_ID>"
  STORAGE_ENDPOINT_URL: "https://storage.googleapis.com"
  STORAGE_REGION: "us-central1"

secrets:
  backend:
    STORAGE_ACCESS_KEY: "<HMAC_ACCESS_ID>"
    STORAGE_SECRET_KEY: "<HMAC_SECRET>"
```

</details>

### Step 3: Configure ingress and TLS

Update the ingress section in `values.generated.yaml`:

```yaml
ingress:
  enabled: true
  className: "nginx"

  tls:
    enabled: false  # See TLS options below
    secretName: "aurora-tls"
    certManager:
      enabled: false
      issuer: "letsencrypt-prod"
      email: "admin@yourdomain.com"

  hosts:
    frontend: "aurora.yourdomain.com"
    api: "api.aurora.yourdomain.com"
    ws: "ws.aurora.yourdomain.com"
```

#### TLS/HTTPS Configuration (Choose one option)

**Option 1: cert-manager with Let's Encrypt (Recommended)**

Automatic certificate management with free Let's Encrypt certificates:

```bash
# Install cert-manager
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.13.3/cert-manager.yaml

# Wait for cert-manager pods to be ready (takes ~30 seconds)
kubectl wait --for=condition=ready pod -l app.kubernetes.io/instance=cert-manager -n cert-manager --timeout=120s

# Create Let's Encrypt issuer
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

# Enable in values.generated.yaml
tls:
  enabled: true
  certManager:
    enabled: true
    issuer: "letsencrypt-prod"
    email: "admin@yourdomain.com"
```

**Option 2: Manual TLS Certificate**

Bring your own certificate (wildcard recommended):

```bash
# Create Kubernetes secret with your certificate
kubectl create secret tls aurora-tls \
  --cert=path/to/fullchain.crt \
  --key=path/to/privkey.key \
  -n aurora-oss

# Enable in values.generated.yaml
tls:
  enabled: true
  secretName: "aurora-tls"
```

#### DNS Configuration

Create DNS records pointing to your ingress controller's external IP:

```bash
# Get your ingress IP
kubectl get svc -n ingress-nginx ingress-nginx-controller
```

Create these DNS records (A records or CNAME):
```
aurora.yourdomain.com      A/CNAME  <INGRESS_IP_OR_HOSTNAME>
api.aurora.yourdomain.com  A/CNAME  <INGRESS_IP_OR_HOSTNAME>
ws.aurora.yourdomain.com   A/CNAME  <INGRESS_IP_OR_HOSTNAME>
```

Or use a wildcard DNS record:
```
*.aurora.yourdomain.com    A  <INGRESS_IP>
aurora.yourdomain.com      A  <INGRESS_IP>
```

:::tip Quick testing without DNS
For testing without setting up real DNS, you can use [nip.io](https://nip.io) which provides wildcard DNS for any IP address:
```yaml
ingress:
  hosts:
    frontend: "aurora-oss.<INGRESS_IP>.nip.io"
    api: "api.aurora-oss.<INGRESS_IP>.nip.io"
    ws: "ws.aurora-oss.<INGRESS_IP>.nip.io"
```
Update `NEXT_PUBLIC_BACKEND_URL`, `NEXT_PUBLIC_WEBSOCKET_URL`, and `FRONTEND_URL` accordingly. Note that `NEXT_PUBLIC_*` values are baked into the frontend at **build time**, so you must rebuild the frontend image if you change them.
:::

## Local Kubernetes (OrbStack, Docker Desktop, Rancher Desktop)

For local development, you can run the full Aurora stack on your machine's built-in Kubernetes. This skips registry setup, cross-compilation, and cloud dependencies entirely.

### Differences from cloud deployment

| | Cloud (GKE, EKS, AKS) | Local K8s |
|---|---|---|
| Images | Push to registry | Build locally, available immediately |
| Cross-compile | Often needed (ARM Mac → amd64) | No — same architecture |
| Storage | External S3 provider | Built-in MinIO (set `services.minio.enabled: true`) |
| Ingress IP | Public cloud LB IP | Private IP (e.g. `192.168.x.x`) |
| Access | From anywhere | From your machine only |

### Quick start

```bash
# 1. Enable Kubernetes in your container runtime (OrbStack, Docker Desktop, etc.)

# 2. Install nginx ingress
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.8.1/deploy/static/provider/cloud/deploy.yaml
kubectl wait --for=condition=ready pod -l app.kubernetes.io/component=controller -n ingress-nginx --timeout=120s

# 3. Get the ingress IP
kubectl get svc -n ingress-nginx ingress-nginx-controller
# Note the EXTERNAL-IP

# 4. Build images locally (no --push, no --platform, no registry)
DOCKER_BUILDKIT=1 docker build ./server --target=prod -t localhost/aurora-server:local
DOCKER_BUILDKIT=1 docker build ./client --target=prod --build-arg NEXT_PUBLIC_BACKEND_URL=http://api.aurora-oss.<IP>.nip.io --build-arg NEXT_PUBLIC_WEBSOCKET_URL=ws://ws.aurora-oss.<IP>.nip.io -t localhost/aurora-frontend:local

# 5. Create and configure values
cp deploy/helm/aurora/values.yaml deploy/helm/aurora/values.generated.yaml
```

Then edit `values.generated.yaml`:

```yaml
image:
  registry: "localhost"
  tag: "local"

services:
  minio:
    enabled: true  # Built-in S3-compatible storage

config:
  AURORA_ENV: "dev"
  STORAGE_BUCKET: "aurora-storage"
  # Leave STORAGE_ENDPOINT_URL empty — auto-generated when MinIO is enabled
  STORAGE_ENDPOINT_URL: ""
  STORAGE_USE_SSL: "false"
  STORAGE_VERIFY_SSL: "false"
  NEXT_PUBLIC_BACKEND_URL: "http://api.aurora-oss.<IP>.nip.io"
  NEXT_PUBLIC_WEBSOCKET_URL: "ws://ws.aurora-oss.<IP>.nip.io"
  FRONTEND_URL: "http://aurora-oss.<IP>.nip.io"

secrets:
  backend:
    STORAGE_ACCESS_KEY: "minioadmin"
    STORAGE_SECRET_KEY: "minioadmin"
  # Generate the rest with: openssl rand -base64 32

ingress:
  hosts:
    frontend: "aurora-oss.<IP>.nip.io"
    api: "api.aurora-oss.<IP>.nip.io"
    ws: "ws.aurora-oss.<IP>.nip.io"
```

```bash
# 6. Deploy
helm upgrade --install aurora-oss ./deploy/helm/aurora --namespace aurora-oss --create-namespace --reset-values -f deploy/helm/aurora/values.generated.yaml

# 7. Vault setup (same as cloud — see Vault Setup section below)

# 8. Open http://aurora-oss.<IP>.nip.io in your browser
```

Or use the automated script:

```bash
./deploy/k8s-deploy.sh --local
```

This skips registry push, enables MinIO, and uses `localhost` as the registry.

## Deployment

### Install Nginx Ingress Controller (if not already installed)

```bash
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.8.1/deploy/static/provider/cloud/deploy.yaml

# Wait for external IP
kubectl get svc -n ingress-nginx ingress-nginx-controller --watch
```

### Build and deploy

#### Build images and deploy with `make deploy`

```bash
make deploy
```

This reads `values.generated.yaml`, builds and pushes images using `docker buildx`, and deploys with Helm. The images are tagged with the current git SHA and pushed to the registry configured in your values file.

:::note Cross-architecture builds
If you're building on a different architecture than your cluster (e.g., ARM Mac → amd64 cluster), `docker buildx` will cross-compile using QEMU emulation. This works but can be slow. For faster builds, consider using your cloud provider's build service or a CI/CD pipeline.
:::

#### Manual build and deploy

If `make deploy` doesn't fit your workflow, you can build and deploy manually:

```bash
# Authenticate with your container registry
docker login <your-registry>  # Docker Hub, GHCR, ECR, etc.

GIT_SHA=$(git rev-parse --short HEAD)
REGISTRY="your-registry.example.com"  # e.g., docker.io/myuser, ghcr.io/myorg

# Build and push backend
DOCKER_BUILDKIT=1 docker buildx build ./server \
  --target=prod \
  --platform linux/amd64 \
  --push \
  -t $REGISTRY/aurora-server:$GIT_SHA

# Build and push frontend (NEXT_PUBLIC_* vars are baked in at build time)
DOCKER_BUILDKIT=1 docker buildx build ./client \
  --target=prod \
  --platform linux/amd64 \
  --build-arg NEXT_PUBLIC_BACKEND_URL=https://api.yourdomain.com \
  --build-arg NEXT_PUBLIC_WEBSOCKET_URL=wss://ws.yourdomain.com \
  --push \
  -t $REGISTRY/aurora-frontend:$GIT_SHA

# Update image tag in values file
yq -i ".image.tag = \"$GIT_SHA\"" deploy/helm/aurora/values.generated.yaml

# Deploy with Helm
helm upgrade --install aurora-oss ./deploy/helm/aurora \
  --namespace aurora-oss --create-namespace \
  --reset-values \
  -f deploy/helm/aurora/values.generated.yaml
```

:::warning BuildKit required
The Dockerfiles use `RUN --mount=type=cache` for dependency caching, which requires BuildKit. Always set `DOCKER_BUILDKIT=1` or use Docker Desktop (BuildKit is enabled by default).
:::

<details>
<summary><strong>GCP: Using Google Cloud Build with Artifact Registry</strong></summary>

If you're on GKE and want to build images in the cloud (avoids slow cross-compilation on ARM Macs):

```bash
# Create an Artifact Registry repo (one-time setup)
gcloud artifacts repositories create aurora \
  --repository-format=docker \
  --location=us \
  --description="Aurora container images"

# Authenticate Docker with Artifact Registry
gcloud auth configure-docker us-docker.pkg.dev

GIT_SHA=$(git rev-parse --short HEAD)
REGISTRY="us-docker.pkg.dev/<PROJECT_ID>/aurora"

# Build backend image
gcloud builds submit ./server \
  --config=/dev/stdin \
  --timeout=1200 \
  --machine-type=e2-highcpu-8 <<EOF
steps:
  - name: 'gcr.io/cloud-builders/docker'
    env: ['DOCKER_BUILDKIT=1']
    args: ['build', '--target=prod', '-t', '$REGISTRY/aurora-server:$GIT_SHA', '.']
images: ['$REGISTRY/aurora-server:$GIT_SHA']
EOF

# Build frontend image (pass NEXT_PUBLIC_* build args)
gcloud builds submit ./client \
  --config=/dev/stdin \
  --timeout=1200 \
  --machine-type=e2-highcpu-8 <<EOF
steps:
  - name: 'gcr.io/cloud-builders/docker'
    env: ['DOCKER_BUILDKIT=1']
    args:
      - 'build'
      - '--target=prod'
      - '--build-arg=NEXT_PUBLIC_BACKEND_URL=https://api.yourdomain.com'
      - '--build-arg=NEXT_PUBLIC_WEBSOCKET_URL=wss://ws.yourdomain.com'
      - '--build-arg=NEXT_PUBLIC_ENABLE_OVH=false'
      - '--build-arg=NEXT_PUBLIC_ENABLE_SLACK=false'
      - '--build-arg=NEXT_PUBLIC_ENABLE_PAGERDUTY_OAUTH=false'
      - '--build-arg=NEXT_PUBLIC_ENABLE_CONFLUENCE=false'
      - '-t'
      - '$REGISTRY/aurora-frontend:$GIT_SHA'
      - '.'
images: ['$REGISTRY/aurora-frontend:$GIT_SHA']
EOF
```

Then update the tag and deploy with Helm as shown in the manual build section above.

**Note:** You **must** set `DOCKER_BUILDKIT=1` in Cloud Build steps. Using `gcloud builds submit --tag` alone will fail because the Dockerfiles require BuildKit.

</details>

## Vault Setup

### Choose Your Vault Unsealing Strategy

:::warning Production vs Development
**For production environments**: Use [Vault Auto-Unseal with KMS](./vault-kms-setup) to eliminate manual unsealing after pod restarts. Manual unsealing is not suitable for production as it requires operator intervention every time Vault restarts.

**For development/staging**: The manual setup below is fine for non-production environments.
:::

If you're deploying to production, **skip the manual setup below** and follow the [Vault KMS setup guide](./vault-kms-setup) instead.

### Initialize Vault (first deployment only)

For development/staging environments, initialize Vault manually:

```bash
# Initialize Vault
kubectl -n aurora-oss exec -it statefulset/aurora-oss-vault -- \
  vault operator init -key-shares=1 -key-threshold=1
```

**Save the output securely** — you need the Unseal Key and Root Token.

```bash
# Unseal Vault
kubectl -n aurora-oss exec -it statefulset/aurora-oss-vault -- \
  vault operator unseal <UNSEAL_KEY>

# Verify Vault is ready
kubectl -n aurora-oss exec -it statefulset/aurora-oss-vault -- \
  vault status
```

Add the Root Token to `values.generated.yaml` as `secrets.backend.VAULT_TOKEN`, then redeploy:

```bash
make deploy
```

### Configure Vault KV Mount and Policy (first deployment only)

After Vault is unsealed, set up the KV mount and application policy:

```bash
# Login with root token
kubectl -n aurora-oss exec statefulset/aurora-oss-vault -- sh -c \
  'export VAULT_ADDR=http://127.0.0.1:8200 && echo "<ROOT_TOKEN>" | vault login -'

# Enable KV v2 secrets engine at path 'aurora'
kubectl -n aurora-oss exec statefulset/aurora-oss-vault -- sh -c \
  'export VAULT_ADDR=http://127.0.0.1:8200 && vault secrets enable -path=aurora kv-v2'

# Create Aurora application policy
kubectl -n aurora-oss exec statefulset/aurora-oss-vault -- sh -c \
  'export VAULT_ADDR=http://127.0.0.1:8200 && vault policy write aurora-app - <<EOF
# Aurora application policy
path "aurora/data/users/*" {
  capabilities = ["create", "read", "update", "delete", "list"]
}
path "aurora/metadata/users/*" {
  capabilities = ["list", "read", "delete"]
}
path "aurora/metadata/" {
  capabilities = ["list"]
}
path "aurora/metadata/users" {
  capabilities = ["list"]
}
EOF'

# Create token with aurora-app policy
kubectl -n aurora-oss exec statefulset/aurora-oss-vault -- sh -c \
  'export VAULT_ADDR=http://127.0.0.1:8200 && vault token create -policy=aurora-app -ttl=0'
```

**Update `values.generated.yaml`** with the token from the last command (replace `<ROOT_TOKEN>` with the token output):

```yaml
secrets:
  backend:
    VAULT_TOKEN: "<TOKEN_FROM_ABOVE>"
```

:::danger Secure This Token
The `VAULT_TOKEN` grants access to all secrets stored in Vault. If this token is compromised, an attacker can read/modify all application secrets. Store `values.generated.yaml` securely and never commit it to version control.
:::

Then redeploy:

```bash
make deploy
```

:::tip Remember
With manual unsealing, you'll need to run `kubectl exec ... vault operator unseal <UNSEAL_KEY>` after every Vault pod restart. For production, use [KMS auto-unseal](./vault-kms-setup) instead.
:::

## Verify deployment

```bash
# Check all pods are running
kubectl get pods -n aurora-oss

# Check Ingress has an external IP and all hosts are configured
kubectl get ingress -n aurora-oss

# View logs
kubectl logs -n aurora-oss deploy/aurora-oss-server --tail=50
kubectl logs -n aurora-oss deploy/aurora-oss-chatbot --tail=50
kubectl logs -n aurora-oss deploy/aurora-oss-frontend --tail=50

# Test the API
curl https://api.aurora.yourdomain.com/health
```

Open `https://aurora.yourdomain.com` in your browser.

## Upgrading

### Update configuration only
```bash
helm upgrade aurora-oss ./deploy/helm/aurora \
  --reset-values -f deploy/helm/aurora/values.generated.yaml -n aurora-oss
```

**Note:** The `--reset-values` flag ensures Helm uses only the values from your file, ignoring any previously cached values. Pods automatically restart only when ConfigMap/Secret values change (env vars). For other changes (replicas, resources, ingress), pods won't restart automatically.

### Update with new code/images
```bash
git pull
make deploy
```

### Rollback if needed
```bash
helm rollback aurora-oss -n aurora-oss
```

## Uninstalling

```bash
helm uninstall aurora-oss -n aurora-oss
kubectl delete namespace aurora-oss
```

## Production Security

The default configuration uses a static Vault root token stored in Kubernetes Secrets. For production deployments, consider these security enhancements:

### 1. Vault Kubernetes Authentication (Recommended)

Use Vault's Kubernetes auth method so pods authenticate using their Service Account instead of a static token:

```bash
# Enable Kubernetes auth in Vault
kubectl exec -it statefulset/aurora-oss-vault -- vault auth enable kubernetes

# Configure Vault to talk to Kubernetes
kubectl exec -it statefulset/aurora-oss-vault -- vault write auth/kubernetes/config \
  kubernetes_host="https://$KUBERNETES_PORT_443_TCP_ADDR:443"
```

Then update your applications to use Vault Agent sidecars that automatically fetch secrets.

### 2. External Secrets Operator

Use the [External Secrets Operator](https://external-secrets.io/) to sync secrets from Vault into Kubernetes Secrets automatically, with proper RBAC controls.

### 3. Vault Auto-Unseal with KMS

Eliminate manual unsealing after pod restarts by using cloud KMS. **Only GCP Cloud KMS is supported at the moment.**

| Provider | Guide | Cost | Setup Time |
|----------|-------|------|------------|
| GCP | [Vault KMS Setup](./vault-kms-gcp) | ~$0.06/mo | 25-35 min |

See [Vault Auto-Unseal Overview](./vault-kms-setup) for decision framework and setup guide.

### 4. Pod Security Standards

Enable Kubernetes Pod Security Standards to restrict pod capabilities and enforce security policies.

## Troubleshooting

**Pods stuck in Pending**: Check StorageClass availability and resource limits.
```bash
kubectl describe pod -n aurora-oss <pod-name>
```

**Vault sealed after restart**: Re-run the unseal command with your saved Unseal Key.

**Image pull errors**: Verify registry credentials and that images were pushed successfully.
```bash
kubectl get events -n aurora-oss --sort-by='.lastTimestamp'
```

**Frontend CrashLoopBackOff with "Permission denied"**: The frontend entrypoint writes `/app/public/env-config.js` at startup. If you see `can't create /app/public/env-config.js: Permission denied`, the Dockerfile needs to set file ownership for the non-root user. Ensure `client/Dockerfile` includes `chown -R 1000:1000 /app` in the prod stage:
```dockerfile
RUN chmod +x /docker-entrypoint.sh && chown -R 1000:1000 /app
```
Rebuild and push the frontend image after fixing.

**Image not updating after rebuild with same tag**: Kubernetes nodes cache images locally. If you push a new image with the same tag, pods using `imagePullPolicy: IfNotPresent` (the default) won't pull the update. Either use a unique tag per build (recommended — the Makefile uses the git SHA), or force a re-pull:
```bash
kubectl rollout restart deployment/aurora-oss-frontend -n aurora-oss
```

**Database connection errors**: Ensure PostgreSQL pod is running and the password matches.
```bash
kubectl logs -n aurora-oss statefulset/aurora-oss-postgres
```

**API returns 404**: Verify DNS records point to the Ingress controller IP and the Ingress has an ADDRESS.
```bash
kubectl get ingress -n aurora-oss
kubectl describe ingress -n aurora-oss
nslookup api.aurora.yourdomain.com
```

**WebSocket connection failures**: Check that the chatbot pod is running and DNS is configured for the ws subdomain.
```bash
kubectl logs -n aurora-oss deploy/aurora-oss-chatbot --tail=100
nslookup ws.aurora.yourdomain.com
```

**TLS certificate errors**: Ensure your certificate covers all three subdomains (wildcard recommended).
```bash
kubectl describe secret aurora-tls -n aurora-oss
openssl s_client -connect api.aurora.yourdomain.com:443 -servername api.aurora.yourdomain.com
```

**Build fails with "the --mount option requires BuildKit"**: Set `DOCKER_BUILDKIT=1` before running `docker build`. Docker Desktop enables BuildKit by default, but CI environments and cloud build services may not.

## Configuration reference

See `values.yaml` for all available options including:
- Replica counts per service
- Resource requests/limits
- Persistence sizes
- Optional integrations (Slack, PagerDuty, GitHub OAuth, etc.)

### Internal service discovery

The following config values are auto-generated by Helm if left empty:
- `POSTGRES_HOST` → `<release>-postgres`
- `REDIS_URL` → `redis://<release>-redis:6379/0`
- `WEAVIATE_HOST` → `<release>-weaviate`
- `BACKEND_URL` → `http://<release>-server:5080`
- `CHATBOT_INTERNAL_URL` → `http://<release>-chatbot:5007`
- `VAULT_ADDR` → `http://<release>-vault:8200`
- `SEARXNG_URL` → `http://<release>-searxng:8080`

Leave these empty in `values.generated.yaml` unless you're using external services.
