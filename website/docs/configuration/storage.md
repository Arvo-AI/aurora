---
sidebar_position: 3
---

# Object Storage

Aurora uses S3-compatible object storage for file uploads and artifacts. SeaweedFS is the default backend (Apache 2.0 licensed).

## Default: SeaweedFS

SeaweedFS runs as part of the Docker Compose stack with no additional setup required.

### Access Points

| Service | URL | Description |
|---------|-----|-------------|
| S3 API | http://localhost:8333 | S3-compatible endpoint |
| File Browser | http://localhost:8888 | Web UI for files |
| Cluster Status | http://localhost:9333 | Master status |

### Default Credentials

```bash
STORAGE_ACCESS_KEY=admin
STORAGE_SECRET_KEY=admin
```

## Configuration

```bash
# Storage type: seaweedfs, s3, r2, minio
STORAGE_TYPE=seaweedfs

# Bucket name
STORAGE_BUCKET=aurora

# Endpoint (for S3-compatible backends)
STORAGE_ENDPOINT=http://seaweedfs-filer:8333

# Credentials
STORAGE_ACCESS_KEY=admin
STORAGE_SECRET_KEY=admin

# Region (for AWS S3)
STORAGE_REGION=us-east-1
```

## Supported Backends

Aurora supports any S3-compatible storage:

### AWS S3

#### With static credentials

```bash
STORAGE_TYPE=s3
STORAGE_BUCKET=your-bucket-name
STORAGE_REGION=us-east-1
STORAGE_ACCESS_KEY=AKIA...
STORAGE_SECRET_KEY=...
```

#### With IRSA / pod identity (EKS)

When running on EKS with IRSA, credentials are injected automatically via the ServiceAccount. Leave access keys empty:

```bash
STORAGE_TYPE=s3
STORAGE_BUCKET=your-bucket-name
STORAGE_REGION=us-east-1
# STORAGE_ACCESS_KEY and STORAGE_SECRET_KEY are omitted —
# boto3 uses the IRSA credential chain automatically
```

See the [EKS setup guide](../deployment/eks-setup#step-3-configure-s3-storage) for IRSA role and ServiceAccount configuration.

### Cloudflare R2

```bash
STORAGE_TYPE=r2
STORAGE_BUCKET=your-bucket-name
STORAGE_ENDPOINT=https://accountid.r2.cloudflarestorage.com
STORAGE_ACCESS_KEY=...
STORAGE_SECRET_KEY=...
```

### MinIO

:::warning MinIO images were removed from public registries
In September 2026 MinIO deleted `minio/minio` and `minio/mc` from Docker Hub, closed
anonymous pulls on `quay.io`, and `dl.min.io` now returns `410 Gone`. The community
edition is [source-only and no longer maintained](https://github.com/minio/minio).

Aurora's Helm chart (`services.minio.enabled: true`) therefore uses
[`pgsty/silo`](https://github.com/pgsty/silo), a maintained AGPL fork of upstream's
final community release that keeps the S3 API, `MINIO_*` variables, health routes,
and the `.minio.sys` on-disk format. Unlike upstream, it still ships CVE fixes.

**Upgrading an existing `minio-data` volume is safe.** The `xl.meta` and
`format.json` version constants are unchanged from MinIO, so no migration runs on
first boot and data remains readable by either server. Take a `VolumeSnapshot`
first anyway if your storage class supports it.

In-cluster storage is still intended for local dev. For production, use one of the
managed options above.
:::

```bash
STORAGE_TYPE=minio
STORAGE_BUCKET=aurora
STORAGE_ENDPOINT=http://minio:9000
STORAGE_ACCESS_KEY=minioadmin
STORAGE_SECRET_KEY=minioadmin
```

### Backblaze B2

```bash
STORAGE_TYPE=s3
STORAGE_BUCKET=your-bucket-name
STORAGE_ENDPOINT=https://s3.us-west-000.backblazeb2.com
STORAGE_REGION=us-west-000
STORAGE_ACCESS_KEY=...
STORAGE_SECRET_KEY=...
```

### Google Cloud Storage (S3 Interop)

Create HMAC keys in GCP Console: Cloud Storage → Settings → Interoperability → Create a key.

```bash
STORAGE_TYPE=s3
STORAGE_BUCKET=your-bucket-name
STORAGE_ENDPOINT=https://storage.googleapis.com
STORAGE_ACCESS_KEY=...  # HMAC key
STORAGE_SECRET_KEY=...  # HMAC secret
```

## Usage in Code

```python
from utils.storage.storage import get_storage_manager

storage = get_storage_manager()

# Upload
storage.upload_file(local_path, remote_key)

# Download
storage.download_file(remote_key, local_path)

# Delete
storage.delete_file(remote_key)
```

## SeaweedFS Web UI

Browse uploaded files at http://localhost:8888:

1. Navigate directories
2. Preview files
3. Download/upload via browser

## Persistence

Storage data is persisted in Docker volumes. Use cleanup commands carefully:

```bash
# Removes storage data
make prod-clean

# Preserves storage data
make down
```
