---
sidebar_position: 5
---

# Multi-architecture Images (ARM64 + AMD64)

Aurora's published container images work transparently on both `linux/amd64` (Intel/AMD x86_64) and `linux/arm64` (Apple Silicon, AWS Graviton, Ampere) hosts. The same image tag serves both architectures — Docker automatically picks the right one based on your host, so there is no "choose your arch" step during install.

## TL;DR

```bash
docker pull ghcr.io/arvo-ai/aurora-server:edge
# On an Apple Silicon Mac       → pulls linux/arm64
# On an EC2 x86_64 instance     → pulls linux/amd64
# On an EKS Graviton node       → pulls linux/arm64
# No flags, no configuration.
```

## Which images are multi-arch?

- `ghcr.io/arvo-ai/aurora-server` — Flask API, Celery worker, Celery beat, and chatbot are all built from `server/Dockerfile`.
- `ghcr.io/arvo-ai/aurora-frontend` — Next.js app built from `client/Dockerfile`.

Both are published as **OCI image indexes** (a.k.a. manifest lists) containing `linux/amd64` and `linux/arm64` variants under the same tag.

## How it works (OCI manifest lists)

When you run `docker pull` against a multi-arch image, the registry returns a manifest list describing every available architecture. The Docker client reads your host's `GOARCH` and pulls only the matching variant. This is the same mechanism used by official images like `python:3.12-slim`, `node:20-alpine`, and `postgres:15-alpine`.

You don't need to pick or configure anything — the registry serves the correct image automatically based on where you're running `docker pull`.

## Forcing a specific architecture

If you need to pull or run a non-native arch (for example, debugging an amd64-only issue on an Apple Silicon Mac via Rosetta), use `--platform`:

```bash
# Force amd64 pull on any host
docker pull --platform linux/amd64 ghcr.io/arvo-ai/aurora-server:edge

# Force amd64 at run time
docker run --platform linux/amd64 ghcr.io/arvo-ai/aurora-server:edge

# In docker-compose.yaml, pin a service to a specific arch:
services:
  aurora-server:
    image: ghcr.io/arvo-ai/aurora-server:edge
    platform: linux/amd64
```

## Kubernetes behavior

No Helm chart changes are required. When the kubelet on each node pulls an image, it automatically requests the variant matching that node's architecture. On mixed amd64/arm64 clusters (e.g. EKS with both `m5` and `m7g` node groups), the same Helm release runs seamlessly on every node.

If you want to pin a workload to a specific architecture, use a standard `nodeSelector`:

```yaml
spec:
  nodeSelector:
    kubernetes.io/arch: arm64
```

## Verifying a multi-arch image

```bash
docker buildx imagetools inspect ghcr.io/arvo-ai/aurora-server:edge
```

The output should list both platforms under `Manifests`:

```text
Name:      ghcr.io/arvo-ai/aurora-server:edge
MediaType: application/vnd.oci.image.index.v1+json
...
Manifests:
  Name:      ghcr.io/arvo-ai/aurora-server:edge@sha256:...
  Platform:  linux/amd64
  ...
  Name:      ghcr.io/arvo-ai/aurora-server:edge@sha256:...
  Platform:  linux/arm64
```

## Make commands

Most `make` targets that build or pull images auto-detect the host architecture — on Apple Silicon you get arm64 everywhere by default; on an x86_64 CI runner you get amd64. No flags, no branching.

The one exception is **`make deploy-build`**, which is intentionally *not* host-scoped: it defaults to building both `linux/amd64` and `linux/arm64` regardless of where it runs, so a single push produces a multi-arch manifest list suitable for any target cluster. Override this with the `PLATFORMS` variable (e.g. `make deploy-build PLATFORMS=linux/arm64`) if you only need one arch for a quick demo.

| Command | Arch behavior |
|---|---|
| `make dev` / `make dev-build` | Builds for host arch from local source. |
| `make prod` / `make prod-prebuilt` | Pulls the matching arch from GHCR (Docker auto-resolves the manifest list). |
| `make prod-build` / `make prod-local` | Builds for host arch from local source. |
| `make prod-airtight` | Loads whichever per-arch tarball you provide (see the airtight section below). |
| `make deploy-build` | Defaults to building **both** `linux/amd64` and `linux/arm64` and pushes a multi-arch manifest list to your configured registry. Override with `PLATFORMS=linux/arm64` (or any subset) to build a single arch. |

### Note on `make deploy-build`

`deploy-build` produces a multi-arch manifest list by default so the same tag deploys cleanly to mixed-arch Kubernetes clusters. Because it runs on a single host, the non-native architecture is built via QEMU emulation and is noticeably slower than a native build — especially for the Python server image, which compiles wheels for `grpcio`, `psycopg2`, and `cryptography`. For routine publishes, prefer the CI workflow (`.github/workflows/publish-images.yml`), which builds each arch natively on GitHub-hosted runners in parallel.

If you only need a single arch for a quick demo push, override the platform list:

```bash
make deploy-build PLATFORMS=linux/arm64
```

## Publishing workflow

`.github/workflows/publish-images.yml` builds `aurora-server` and `aurora-frontend` natively on both `ubuntu-24.04` (amd64) and `ubuntu-24.04-arm` (arm64) GitHub-hosted runners in parallel, pushes each arch as a digest-only image to GHCR, and then merges the per-arch digests into a single manifest list under each published tag (`:edge`, `:1.2.3`, `:1.2`, `:sha-abc123`, `:latest`). `:latest` is only pushed for stable release-tag events (`refs/tags/v*.*.*`, excluding prereleases); pushes to `main` publish `:edge` and `:sha-<short>` but not `:latest`. Native builds avoid QEMU emulation overhead and keep publish times reasonable even as the Python layer grows.

Every merge also runs `docker buildx imagetools inspect` and fails the job if both `linux/amd64` and `linux/arm64` aren't present in the published manifest list, so a broken multi-arch publish is caught in CI rather than at pull time.

## Air-gapped deployments

For air-gapped environments, `.github/workflows/publish-airtight.yml` builds `scripts/package-airtight.sh` natively for each arch and publishes a single gzipped tarball per build:

```text
aurora-airtight-<version>-<arch>.tar.gz
```

The bundles are uploaded to per-arch GCS buckets:

- `aurora-airtight-bucket` — `linux/amd64`
- `aurora-airtight-bucket-arm64` — `linux/arm64`

Download the bundle that matches your target host, transfer it across the air gap, and point `make prod-airtight` at it with the `AIRTIGHT_BUNDLE` variable:

```bash
make prod-airtight AIRTIGHT_BUNDLE=/path/to/aurora-airtight-<version>-<arch>.tar.gz
```

`prod-airtight` runs `docker load < $AIRTIGHT_BUNDLE` to import the gzipped tarball before starting Aurora (`docker load` auto-detects gzip). If `AIRTIGHT_BUNDLE` is not set, the target skips the load step and assumes the images are already present in the local Docker daemon — so first-time installs must always set it.
