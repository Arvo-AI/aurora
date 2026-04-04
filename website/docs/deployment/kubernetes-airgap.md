---
sidebar_position: 4
---

# Air-Gapped Kubernetes Deployment

Deploy Aurora on a Kubernetes cluster with a private container registry. The deployment scripts automatically detect your environment and use the most efficient method available.

**Prerequisites:**

- Kubernetes 1.25+ with a default StorageClass
- A private container registry accessible from the cluster (Harbor, Docker Distribution, Zot, etc.)
- `kubectl`, `helm`, `yq`, and `docker` (or `skopeo`) on a machine that can reach the registry
- Network access from cluster nodes to the private registry

## Deployment Pipeline Overview

```
                  ┌─────────────────────────────────┐
                  │   Does your machine have          │
                  │   internet + cluster access?      │
                  └───────────────┬─────────────────┘
                                  │
                  ┌───────────────┴───────────────┐
                  ▼                               ▼
     ┌────────────────────────┐      ┌────────────────────────┐
     │          YES           │      │           NO           │
     │   Standard deployment  │      │    Air-gapped / split  │
     └───────────┬────────────┘      └───────────┬────────────┘
                 │                                │
                 ▼                   ┌────────────┴─────────────────────────┐
  ┌──────────────────────────┐      │                                      │
  │  Just run:               │      │   STEP 1  Get the source             │
  │                          │      │                                      │
  │  ./scripts/deploy-k8s.sh │      │   On a connected machine:            │
  │    <registry-url>        │      │   download-bundle.sh                 │
  │                          │      │   → images tarball + source archive  │
  │  Handles everything:     │      │                                      │
  │  • Pulls images from     │      ├──────────────────────────────────────┤
  │    upstream registries   │      │                                      │
  │  • Pushes to your        │      │   STEP 2  Transfer to bastion        │
  │    private registry      │      │                                      │
  │  • Configures Helm       │      │   scp / usb / file transfer          │
  │    values (interactive)  │      │   Both files → bastion / jump host   │
  │  • Deploys with Helm     │      │                                      │
  │  • Inits & unseals Vault │      ├──────────────────────────────────────┤
  │                          │      │                                      │
  │  One command, done.      │      │  ── On the bastion ──────────────    │
  │                          │      │                                      │
  │  Then (recommended):     │      │   STEP 3  Push images to registry    │
  │  Set up KMS auto-unseal  │      │                                      │
  │  for production.         │      │   ./scripts/push-to-registry.sh \    │
  │  See Vault KMS docs.     │      │     <registry-url>                   │
  └──────────────────────────┘      │   Auto-detects tarball, loads and    │
                                    │   pushes all images to registry.     │
                                    │                                      │
                                    ├──────────────────────────────────────┤
                                    │                                      │
                                    │   STEP 4  Configure                  │
                                    │                                      │
                                    │   ./scripts/configure-helm.sh        │
                                    │   Prompts for LLM key, domain, TLS.  │
                                    │   Writes values.generated.yaml       │
                                    │                                      │
                                    ├──────────────────────────────────────┤
                                    │                                      │
                                    │   STEP 5  Deploy                     │
                                    │                                      │
                                    │   helm upgrade --install aurora-oss  │
                                    │     ./deploy/helm/aurora             │
                                    │     --namespace aurora               │
                                    │     --create-namespace               │
                                    │     -f values.generated.yaml         │
                                    │                                      │
                                    ├──────────────────────────────────────┤
                                    │                                      │
                                    │   STEP 6  Vault setup                │
                                    │                                      │
                                    │   Initialize & unseal Vault.         │
                                    │   See: Vault Setup docs.             │
                                    │                                      │
                                    │   Then (recommended):                │
                                    │   Configure KMS auto-unseal          │
                                    │   for production.                    │
                                    │   See: Vault KMS docs.              │
                                    │                                      │
                                    ├──────────────────────────────────────┤
                                    │                                      │
                                    │   STEP 7  Verify                     │
                                    │                                      │
                                    │   kubectl get pods -n aurora         │
                                    │   (all pods should be Running)       │
                                    │                                      │
                                    └──────────────────────────────────────┘
```

> **Left path**: Your machine has internet and can reach the cluster. `deploy-k8s.sh` does everything — one command, fully automated.
>
> **Right path**: Air-gapped or split environment. Download the bundle on a connected machine, transfer to bastion, then follow Steps 3–7 on the bastion.

---

## Which Path Is Right for You?

| Scenario | What You Need | Path |
|---|---|---|
| **Standard** — operator has internet + registry access | Internet on your workstation, push to private registry | [Path A](#path-a-standard-internet--private-registry) |
| **True air-gap** — physically disconnected environment | Tarball transferred via USB/SCP, no internet on target | [Path B](#path-b-true-air-gap-tarball) |

Most enterprise deployments (even "air-gapped" ones) fall into **Path A** — the operator's workstation has internet, and images are pushed to an internal registry that cluster nodes can pull from. **Path B** is for defense, government, or similarly isolated environments where no machine in the deployment chain has internet.

---

## Path A: Standard (Internet + Private Registry)

Your workstation can reach both the public internet and your private registry. No tarball needed — images are pulled from upstream and pushed directly.

### 1. Get the Source

```bash
git clone https://github.com/arvo-ai/aurora.git && cd aurora
```

Or download a specific release:

```bash
VERSION="v1.2.3"
curl -fsSL -o "aurora-${VERSION#v}.tar.gz" "https://github.com/arvo-ai/aurora/archive/refs/tags/${VERSION}.tar.gz"
tar xzf "aurora-${VERSION#v}.tar.gz" && cd "aurora-${VERSION#v}"
```

### 2. Push Images to Your Registry

```bash
./scripts/push-to-registry.sh registry.internal:5000
```

The script auto-detects that upstream registries (GHCR, Docker Hub, etc.) are reachable and copies images directly — **no tarball, no extra disk usage**. It uses `skopeo` for zero-disk registry-to-registry copies, or falls back to `docker pull/push`.

### 3. Configure

```bash
./scripts/configure-helm.sh
```

Prompts for:
- LLM provider and API key
- Base domain (derives ingress hosts and public URLs)
- TLS configuration

### 4. Deploy

```bash
helm upgrade --install aurora-oss ./deploy/helm/aurora \
  --namespace aurora --create-namespace \
  --reset-values \
  -f deploy/helm/aurora/values.generated.yaml
```

### 5. Vault Setup

Follow the same Vault setup as a standard deployment — see [Vault Setup](./kubernetes#step-4-vault-setup).

After deployment, optionally set up KMS auto-unseal so Vault auto-unseals on pod restarts — see [Vault KMS Setup](./vault-kms-setup).

### 6. Verify

```bash
kubectl get pods -n aurora
```

---

## Path B: True Air-Gap (Tarball)

No internet on the target environment. Download the bundle on a connected machine, transfer it, then deploy.

### 1. Download the Bundle (on a connected machine)

```bash
curl -fsSL https://raw.githubusercontent.com/arvo-ai/aurora/main/scripts/download-bundle.sh | bash
```

This downloads the image tarball (~11 GB) and source archive from GCS.

To specify a version or architecture:

```bash
# Specific version
curl -fsSL https://raw.githubusercontent.com/arvo-ai/aurora/main/scripts/download-bundle.sh | bash -s -- v1.2.3

# Specific version + ARM
curl -fsSL https://raw.githubusercontent.com/arvo-ai/aurora/main/scripts/download-bundle.sh | bash -s -- v1.2.3 arm64
```

**Browse all available bundles:**
- [amd64 bundles](https://storage.googleapis.com/aurora-airtight-bucket/index.html)
- [arm64 bundles](https://storage.googleapis.com/aurora-airtight-bucket-arm64/index.html)

### 2. Transfer to Air-Gapped Environment

Transfer both files to the machine that can reach the cluster's private registry:

```bash
scp aurora-airtight-v1.2.3-amd64.tar.gz aurora-1.2.3.tar.gz bastion:/tmp/
```

Use whatever method your org supports — SCP, USB drive, file transfer appliance, etc.

### 3. Extract and Push Images

On the air-gapped machine:

```bash
# Extract source archive (Helm chart + scripts)
VERSION="v1.2.3"   # replace with your downloaded version
tar xzf "aurora-${VERSION#v}.tar.gz"
cd "aurora-${VERSION#v}/"

# Push images from tarball to your registry
./scripts/push-to-registry.sh registry.internal:5000 --tarball /tmp/aurora-airtight-v1.2.3-amd64.tar.gz
```

The script loads images from the tarball into Docker and pushes them to your registry, then cleans up the local images.

<details>
<summary><strong>Manual alternative (without the script)</strong></summary>

```bash
# 1. Load images
docker load < /tmp/aurora-airtight-v1.2.3-amd64.tar.gz

# 2. List what was loaded
docker images | grep -E 'aurora_|postgres|redis|vault|weaviate|searxng|transformers|minio|memgraph|ingress-nginx'

# 3. Retag and push each image
REGISTRY=registry.internal:5000

# Aurora images
for img in aurora-server aurora-frontend; do
  docker tag aurora_${img#aurora-}:latest $REGISTRY/$img:latest
  docker push $REGISTRY/$img:latest
done

# Third-party images (showing subset — see push-to-registry.sh for full list)
docker tag postgres:15-alpine $REGISTRY/postgres:15-alpine
docker push $REGISTRY/postgres:15-alpine

docker tag redis:7-alpine $REGISTRY/redis:7-alpine
docker push $REGISTRY/redis:7-alpine

docker tag hashicorp/vault:1.15 $REGISTRY/vault:1.15
docker push $REGISTRY/vault:1.15

docker tag "cr.weaviate.io/semitechnologies/weaviate:1.27.6" $REGISTRY/weaviate:1.27.6
docker push $REGISTRY/weaviate:1.27.6

docker tag "cr.weaviate.io/semitechnologies/transformers-inference:sentence-transformers-all-MiniLM-L6-v2" $REGISTRY/transformers-inference:sentence-transformers-all-MiniLM-L6-v2
docker push $REGISTRY/transformers-inference:sentence-transformers-all-MiniLM-L6-v2

docker tag "searxng/searxng:2025.5.8-7ca24eee4" $REGISTRY/searxng:2025.5.8-7ca24eee4
docker push $REGISTRY/searxng:2025.5.8-7ca24eee4

docker tag "memgraph/memgraph-mage:3.8.1" $REGISTRY/memgraph-mage:3.8.1
docker push $REGISTRY/memgraph-mage:3.8.1

# See scripts/push-to-registry.sh for the complete IMAGE_MAP
# (includes minio, ingress-nginx controller, kube-webhook-certgen)

# 4. Manually create values.generated.yaml
cp deploy/helm/aurora/values.yaml deploy/helm/aurora/values.generated.yaml
# Set image.registry, image.tag, and thirdPartyImages.registry to your registry URL
```

</details>

### 4. Configure

```bash
./scripts/configure-helm.sh
```

Prompts for LLM provider, base domain, and TLS — same as Path A.

:::tip No outbound internet? Use Ollama
If the cluster cannot reach external LLM APIs, run models locally with [Ollama](https://ollama.com/). The configuration script includes an Ollama option, or see [LLM Providers — Ollama](/docs/integrations/llm-providers#ollama-local-models) for full setup details.
:::

### 5. Deploy

```bash
helm upgrade --install aurora-oss ./deploy/helm/aurora \
  --namespace aurora --create-namespace \
  --reset-values \
  -f deploy/helm/aurora/values.generated.yaml
```

### 6. Vault Setup

Follow the same Vault setup as a standard deployment — see [Vault Setup](./kubernetes#step-4-vault-setup).

### 7. Verify

```bash
kubectl get pods -n aurora
```

All pods should reach `Running` status within a few minutes. If an image pull fails, check:

```bash
kubectl describe pod <pod-name> -n aurora
kubectl get events -n aurora --sort-by='.lastTimestamp'
```

---

## Guided Deployment (Single Command)

For convenience, the `deploy-k8s.sh` script orchestrates all steps above. It detects your environment and adapts automatically:

- **Internet available?** Pulls images directly from upstream (no tarball needed).
- **Local tarball found?** Loads and pushes from it.
- **No cluster access?** Completes what it can (push images, configure) and tells you how to finish on a machine with cluster access.

```bash
# From the repo — auto-detects best method
./scripts/deploy-k8s.sh registry.internal:5000

# With an explicit tarball
./scripts/deploy-k8s.sh registry.internal:5000 --tarball aurora-airtight-v1.2.3-amd64.tar.gz

# Pin a specific version
./scripts/deploy-k8s.sh registry.internal:5000 v1.2.3

# Skip image push (already done)
./scripts/deploy-k8s.sh registry.internal:5000 --skip-push
```

Via curl (requires internet):

```bash
curl -fsSL https://raw.githubusercontent.com/arvo-ai/aurora/main/scripts/deploy-k8s.sh | bash -s -- registry.internal:5000
```

---

## Deploying Updates

Each new Aurora release requires updated images:

**Path A (internet):**

```bash
cd aurora && git pull
./scripts/push-to-registry.sh registry.internal:5000
helm upgrade aurora-oss ./deploy/helm/aurora -n aurora --reset-values -f deploy/helm/aurora/values.generated.yaml
```

**Path B (tarball):**

```bash
# Download new bundle on connected machine, transfer to air-gapped machine, then:
./scripts/push-to-registry.sh registry.internal:5000 --tarball aurora-airtight-<new-version>-amd64.tar.gz
helm upgrade aurora-oss ./deploy/helm/aurora -n aurora --reset-values -f deploy/helm/aurora/values.generated.yaml
```

---

## Configuration Reference

### Registry Authentication

If your registry requires auth, create a pull secret and add it to `values.generated.yaml`:

```bash
kubectl create namespace aurora
kubectl create secret docker-registry regcred \
  --docker-server=registry.internal:5000 \
  --docker-username=admin \
  --docker-password=secret \
  -n aurora
```

```yaml
image:
  pullSecrets:
    - name: regcred
```

### All Configuration Options

For storage, ingress, and other configuration, see the [Kubernetes deployment guide](./kubernetes#configuration-reference).

---

## Troubleshooting

### ImagePullBackOff

The cluster can't reach the registry or the image name doesn't match. Check `kubectl describe pod` for the exact error. Verify the image exists in your registry with `docker pull` or `curl https://registry.internal:5000/v2/_catalog`.

### Registry TLS Errors

If using a self-signed cert, add the CA to containerd's config on each node (`/etc/containerd/certs.d/`) or configure an insecure registry in Docker daemon config.

### Wrong Architecture

If pods crash immediately, you may have loaded `amd64` images onto `arm64` nodes (or vice versa). Rebuild the bundle with the correct `PLATFORM=linux/<arch>`.

### Slow Weaviate Startup

The `t2v-transformers` pod loads a ~90MB ML model into memory on startup. Give it 2-3 minutes before investigating.

For general Kubernetes troubleshooting, see [Kubernetes Deployment — Troubleshooting](./kubernetes#troubleshooting).
