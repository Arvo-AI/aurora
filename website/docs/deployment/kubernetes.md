---
sidebar_position: 2
---

# Kubernetes Deployment

Deploy Aurora on Kubernetes using Helm.

## Prerequisites

- Kubernetes 1.25+ with a default StorageClass
- `kubectl`, `helm`, and `docker` (with buildx)
- Container registry accessible from your cluster
- Nginx Ingress Controller installed in your cluster
- TLS certificate (wildcard certificate recommended for subdomains)
- S3-compatible object storage (AWS S3, MinIO, Cloudflare R2, etc.)

**Cluster resources**: The default configuration requires approximately 4 CPU cores and 12GB memory across all pods. Adjust `resources` in `values.yaml` for smaller clusters.

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
  registry: "gcr.io/my-project"  # Your registry (docker.io, gcr.io, ghcr.io, etc.)
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

**Secrets** (in `secrets` section):
```yaml
secrets:
  # Generate random secrets with: openssl rand -base64 32
  POSTGRES_PASSWORD: ""         # REQUIRED
  STORAGE_ACCESS_KEY: ""        # REQUIRED - Your S3 access key
  STORAGE_SECRET_KEY: ""        # REQUIRED - Your S3 secret key
  FLASK_SECRET_KEY: ""          # REQUIRED
  AUTH_SECRET: ""               # REQUIRED
  SEARXNG_SECRET: ""            # REQUIRED
  VAULT_TOKEN: ""               # Set after Vault initialization
  
  # At least one LLM API key required
  OPENROUTER_API_KEY: ""        # Get from: https://openrouter.ai/keys
  # OR
  OPENAI_API_KEY: ""            # Get from: https://platform.openai.com/api-keys
```

See the comments in `values.yaml` for all available options.

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
  -n aurora

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

## Deployment

### Install Nginx Ingress Controller (if not already installed)

```bash
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.8.1/deploy/static/provider/cloud/deploy.yaml

# Wait for external IP
kubectl get svc -n ingress-nginx ingress-nginx-controller --watch
```

### Build and deploy

```bash
# Build images and deploy
make deploy
```

This reads `values.generated.yaml` and deploys with Helm.

### Initialize Vault (first deployment only)

Vault requires manual initialization on first deployment:

```bash
# Initialize Vault
kubectl -n aurora exec -it statefulset/aurora-oss-vault -- \
  vault operator init -key-shares=1 -key-threshold=1
```

**Save the output securely** — you need the Unseal Key and Root Token.

```bash
# Unseal Vault
kubectl -n aurora exec -it statefulset/aurora-oss-vault -- \
  vault operator unseal <UNSEAL_KEY>

# Verify Vault is ready
kubectl -n aurora exec -it statefulset/aurora-oss-vault -- \
  vault status
```

Add the Root Token to `values.generated.yaml` as `secrets.VAULT_TOKEN`, then redeploy:

```bash
make deploy
```

### Configure Vault KV Mount and Policy (first deployment only)

After Vault is unsealed, set up the KV mount and application policy:

```bash
# Login with root token
kubectl -n aurora exec statefulset/aurora-oss-vault -- sh -c \
  'export VAULT_ADDR=http://127.0.0.1:8200 && echo "<ROOT_TOKEN>" | vault login -'

# Enable KV v2 secrets engine at path 'aurora'
kubectl -n aurora exec statefulset/aurora-oss-vault -- sh -c \
  'export VAULT_ADDR=http://127.0.0.1:8200 && vault secrets enable -path=aurora kv-v2'

# Create Aurora application policy
kubectl -n aurora exec statefulset/aurora-oss-vault -- sh -c \
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
kubectl -n aurora exec statefulset/aurora-oss-vault -- sh -c \
  'export VAULT_ADDR=http://127.0.0.1:8200 && vault token create -policy=aurora-app -ttl=0'
```

**Update `values.generated.yaml`** with the token from the last command (replace `<ROOT_TOKEN>` with the token output):

```yaml
secrets:
  VAULT_TOKEN: "<TOKEN_FROM_ABOVE>"
```

Then redeploy:

```bash
make deploy
```

:::warning Vault Auto-Unseal
Vault must be unsealed after every pod restart. For production, see [Vault Auto-Unseal with KMS](./vault-kms-setup).
:::

## Verify deployment

```bash
# Check all pods are running
kubectl get pods -n aurora

# Check Ingress has an external IP and all hosts are configured
kubectl get ingress -n aurora

# View logs
kubectl logs -n aurora deploy/aurora-oss-server --tail=50
kubectl logs -n aurora deploy/aurora-oss-chatbot --tail=50
kubectl logs -n aurora deploy/aurora-oss-frontend --tail=50

# Test the API
curl https://api.aurora.yourdomain.com/health
```

Open `https://aurora.yourdomain.com` in your browser.

## Upgrading

### Update configuration only
```bash
helm upgrade aurora-oss ./deploy/helm/aurora \
  -f deploy/helm/aurora/values.generated.yaml -n aurora
```

**Note:** Pods automatically restart only when ConfigMap/Secret values change (env vars). For other changes (replicas, resources, ingress), pods won't restart automatically.

### Update with new code/images
```bash
git pull
make deploy
```

### Rollback if needed
```bash
helm rollback aurora-oss -n aurora
```

## Uninstalling

```bash
helm uninstall aurora-oss -n aurora
kubectl delete namespace aurora
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
kubectl describe pod -n aurora <pod-name>
```

**Vault sealed after restart**: Re-run the unseal command with your saved Unseal Key.

**Image pull errors**: Verify registry credentials and that images were pushed successfully.
```bash
kubectl get events -n aurora --sort-by='.lastTimestamp'
```

**Database connection errors**: Ensure PostgreSQL pod is running and the password matches.
```bash
kubectl logs -n aurora statefulset/aurora-oss-postgres
```

**API returns 404**: Verify DNS records point to the Ingress controller IP and the Ingress has an ADDRESS.
```bash
kubectl get ingress -n aurora
kubectl describe ingress -n aurora
nslookup api.aurora.yourdomain.com
```

**WebSocket connection failures**: Check that the chatbot pod is running and DNS is configured for the ws subdomain.
```bash
kubectl logs -n aurora deploy/aurora-oss-chatbot --tail=100
nslookup ws.aurora.yourdomain.com
```

**TLS certificate errors**: Ensure your certificate covers all three subdomains (wildcard recommended).
```bash
kubectl describe secret aurora-tls -n aurora
openssl s_client -connect api.aurora.yourdomain.com:443 -servername api.aurora.yourdomain.com
```

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
