---
sidebar_position: 5
---

# Image Signature Verification

All Aurora container images published to `ghcr.io/arvo-ai/` are cryptographically signed using [Sigstore Cosign](https://docs.sigstore.dev/cosign/signing/overview/) with keyless (OIDC) signing. This allows you to verify that images were built by Aurora's GitHub Actions CI and haven't been tampered with.

## Prerequisites

Install [Cosign](https://docs.sigstore.dev/cosign/system_config/installation/):

```bash
# macOS
brew install cosign

# Linux (download binary)
curl -sSL https://github.com/sigstore/cosign/releases/latest/download/cosign-linux-amd64 -o /usr/local/bin/cosign
chmod +x /usr/local/bin/cosign
```

## Verify an image manually

```bash
cosign verify \
  --certificate-identity-regexp="https://github.com/Arvo-AI/aurora/.*" \
  --certificate-oidc-issuer="https://token.actions.githubusercontent.com" \
  ghcr.io/arvo-ai/aurora-server:latest
```

Replace `latest` with the specific tag you're deploying (e.g. `1.2.3`, `edge`, `sha-abc1234`).

The same command works for all Aurora images:

- `ghcr.io/arvo-ai/aurora-server`
- `ghcr.io/arvo-ai/aurora-frontend`
- `ghcr.io/arvo-ai/charts/aurora-oss` (Helm chart OCI artifact)

## Automatic verification

Aurora's Makefile verifies signatures automatically when cosign is installed:

Both commands **fail and abort** if cosign is installed and verification fails.

| Command | What it verifies |
|---------|-----------------|
| `make prod-prebuilt` | GHCR images after pull, before tagging |
| `make deploy` | Registry images before `helm upgrade` |

If cosign is not installed, both commands print a warning and continue.

## Kubernetes runtime enforcement

For continuous enforcement on a Kubernetes cluster, deploy the [Sigstore policy-controller](https://docs.sigstore.dev/policy-controller/overview/) as an admission webhook. This verifies every pod's images at scheduling time, regardless of how the deployment was created.

```bash
helm repo add sigstore https://sigstore.github.io/helm-charts
helm install policy-controller sigstore/policy-controller \
  --namespace sigstore-system --create-namespace
```

Then create a `ClusterImagePolicy` that matches Aurora images:

```yaml
apiVersion: policy.sigstore.dev/v1beta1
kind: ClusterImagePolicy
metadata:
  name: aurora-images
spec:
  images:
    - glob: "ghcr.io/arvo-ai/**"
  authorities:
    - keyless:
        identities:
          - issuerRegExp: "https://token.actions.githubusercontent.com"
            subjectRegExp: "https://github.com/Arvo-AI/aurora/.*"
```

This ensures that only images signed by Aurora's GitHub Actions workflows can run in your cluster.

## What is signed

| Artifact | Signed in CI | Workflow |
|----------|-------------|----------|
| `aurora-server` multi-arch image | Yes | `publish-images.yml` |
| `aurora-frontend` multi-arch image | Yes | `publish-images.yml` |
| `aurora-oss` Helm chart (OCI) | Yes | `publish-helm.yml` |
| Images pushed via `make deploy-build` | Yes (operator runs cosign locally) | Makefile |
