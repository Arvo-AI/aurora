---
sidebar_position: 4
---

# Air-Gapped Kubernetes Deployment

Deploy Aurora on a Kubernetes cluster with no internet access. All container images are pre-built and bundled into a single tarball on a machine with internet access, then pushed to a private registry inside the cluster. Nothing is fetched from the internet during deployment.

**Prerequisites:**

- Kubernetes 1.25+ with a default StorageClass
- A private container registry accessible from the cluster (Harbor, Docker Distribution, Zot, etc.) — if you don't have one, see [Setting Up a Private Registry](#setting-up-a-private-registry)
- `kubectl`, `helm`, `docker`, `yq` on a machine that can reach the registry
- Network access from cluster nodes to the private registry
- You can SSH (or equivalent) into a machine that can reach the cluster and the registry

:::tip Already familiar with air-gapped deployments?
Skip to the [Quick Reference](#quick-reference) for the condensed command sequence.
:::

### 1. Download the Bundle

On a machine with internet access, download the latest airtight bundle:

```bash
curl -fsSL https://raw.githubusercontent.com/arvo-ai/aurora/main/scripts/download-bundle.sh | bash
```

This auto-resolves the latest release from GitHub and downloads the tarball + checksum from GCS (~11 GB).

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

:::tip Build your own bundle
If you prefer to build from source instead of downloading, see [Creating the Air-Tight Bundle](#creating-the-air-tight-bundle) below.
:::

### 2. Extract and Load Images

The download script also downloads the matching source archive. Extract it and run the push script:

```bash
tar xzf aurora-*.tar.gz
cd aurora-*/
./scripts/push-to-registry.sh registry.internal:5000
```

The script:
1. Runs `docker load` to import all images from the tarball
2. Retags every image with the `registry.internal:5000/` prefix
3. Pushes each image to the registry
4. Creates `values.generated.yaml` and sets `image.registry` and `thirdPartyImages.registry` automatically

<details>
<summary><strong>Manual alternative (without the script)</strong></summary>

```bash
# 1. Load images
docker load < /tmp/aurora-airtight-*.tar.gz

# 2. List what was loaded
docker images | grep -E 'aurora_|postgres|redis|vault|weaviate|searxng|transformers|minio'

# 3. Retag and push each image
REGISTRY=registry.internal:5000

# Aurora images
for img in aurora-server aurora-frontend; do
  docker tag aurora_${img#aurora-}:latest $REGISTRY/$img:latest
  docker push $REGISTRY/$img:latest
done

# Third-party images
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

# 4. Manually create values.generated.yaml
cp deploy/helm/aurora/values.yaml deploy/helm/aurora/values.generated.yaml
# Set image.registry, image.tag, and thirdPartyImages.registry to your registry URL
```

</details>

### 3. Configure Helm Values

The script already set the registry values. Edit `values.generated.yaml` to configure everything else:

```bash
nano deploy/helm/aurora/values.generated.yaml
```

If your registry requires auth, create a pull secret and reference it:

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

**LLM API Key** — set at least one:

```yaml
config:
  OPENROUTER_API_KEY: "sk-or-v1-..."     # Recommended — one key, many models
  LLM_PROVIDER_MODE: "openrouter"
```

:::tip No outbound internet? Use Ollama
If the cluster cannot reach external LLM APIs, run models locally with [Ollama](https://ollama.com/). Transfer the Ollama binary and model weights offline, then configure:
```yaml
config:
  LLM_PROVIDER_MODE: "direct"
  OLLAMA_BASE_URL: "http://ollama-service:11434"
  MAIN_MODEL: "ollama/llama3.1"
  RCA_MODEL: "ollama/llama3.1"
```
See [LLM Providers — Ollama](/docs/integrations/llm-providers#ollama-local-models) for full setup details.
:::

**URLs, secrets, storage, ingress** — configure these the same as a standard Kubernetes deployment. See the [Kubernetes deployment guide](./kubernetes#step-2-configure-required-values) for full details.

### 4. Deploy with Helm

```bash
helm upgrade --install aurora-oss ./deploy/helm/aurora \
  --namespace aurora --create-namespace \
  --reset-values \
  -f deploy/helm/aurora/values.generated.yaml
```

### 5. Get and Set the Vault Token

Follow the same Vault setup as a standard deployment — see [Vault Setup](./kubernetes#vault-setup).

### 6. Verify

```bash
kubectl get pods -n aurora
```

All pods should reach `Running` status within a few minutes. If an image pull fails, check:

```bash
kubectl describe pod <pod-name> -n aurora
kubectl get events -n aurora --sort-by='.lastTimestamp'
```

### Deploying Updates

Each new Aurora release requires a fresh bundle. On a machine with internet access:

```bash
curl -fsSL https://raw.githubusercontent.com/arvo-ai/aurora/main/scripts/download-bundle.sh | bash
```

Transfer the new tarball, then from the repo root:

```bash
./scripts/push-to-registry.sh registry.internal:5000
helm upgrade aurora-oss ./deploy/helm/aurora -n aurora --reset-values -f deploy/helm/aurora/values.generated.yaml
```

### Creating the Air-Tight Bundle

Prebuilt bundles are available for download (see [step 1](#1-download-the-bundle) above). Use this section only if you need to build a custom bundle from source.

```bash
git clone https://github.com/arvo-ai/aurora.git && cd aurora
make package-airtight
```

This builds all Aurora images, pulls all third-party images, and saves everything into `aurora-airtight-<version>-<arch>.tar.gz` (~11 GB) with a SHA-256 checksum. The default target architecture is `linux/amd64`.

To target ARM clusters:

```bash
PLATFORM=linux/arm64 make package-airtight
```

If building on Apple Silicon for an x86 cluster, the default `linux/amd64` cross-compiles automatically — no extra flags needed.

---

## Setting Up a Private Registry

If your air-gapped cluster doesn't have a registry yet, here are two options.

### Option A: Docker Distribution (simplest)

Deploy the official Docker registry as a single pod inside the cluster:

```bash
# Include the registry:2 image in your airgap bundle, or pre-load it on a node:
docker pull registry:2
docker save registry:2 | gzip > registry2.tar.gz
# Transfer to a cluster node, then:
# ctr -n k8s.io images import registry2.tar.gz  (containerd)
# docker load < registry2.tar.gz                 (Docker)

# Deploy as a pod
kubectl create namespace registry
kubectl -n registry run registry --image=registry:2 --port=5000
kubectl -n registry expose pod registry --port=5000 --type=ClusterIP
```

The registry is then reachable from within the cluster at `registry.registry.svc.cluster.local:5000`.

To push images to it, port-forward:

```bash
kubectl -n registry port-forward pod/registry 5000:5000 &
# Then push to localhost:5000, which forwards into the cluster
```

### Option B: Harbor (enterprise)

[Harbor](https://goharbor.io/) provides vulnerability scanning, RBAC, replication, and a web UI. It can be installed via its own Helm chart — include the Harbor images in your airgap tarball.

---

## Quick Reference

Condensed command sequence for experienced operators:

```bash
# === INTERNET-CONNECTED MACHINE ===
curl -fsSL https://raw.githubusercontent.com/arvo-ai/aurora/main/scripts/download-bundle.sh | bash

# === DEPLOY MACHINE (with access to cluster + registry) ===
tar xzf aurora-*.tar.gz && cd aurora-*/
./scripts/push-to-registry.sh registry.internal:5000

# Edit values.generated.yaml: LLM key, URLs, secrets, storage, ingress
nano deploy/helm/aurora/values.generated.yaml

helm upgrade --install aurora-oss ./deploy/helm/aurora \
  -n aurora --create-namespace --reset-values \
  -f deploy/helm/aurora/values.generated.yaml

# Vault init (first deploy only)
kubectl -n aurora exec -it statefulset/aurora-oss-vault -- vault operator init -key-shares=1 -key-threshold=1
kubectl -n aurora exec -it statefulset/aurora-oss-vault -- vault operator unseal <UNSEAL_KEY>
# Add VAULT_TOKEN to values.generated.yaml, redeploy

kubectl get pods -n aurora
```

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
