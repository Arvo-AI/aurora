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

```bash
STORAGE_TYPE=s3
STORAGE_BUCKET=your-bucket-name
STORAGE_REGION=us-east-1
STORAGE_ACCESS_KEY=AKIA...
STORAGE_SECRET_KEY=...
```

### Cloudflare R2

```bash
STORAGE_TYPE=r2
STORAGE_BUCKET=your-bucket-name
STORAGE_ENDPOINT=https://accountid.r2.cloudflarestorage.com
STORAGE_ACCESS_KEY=...
STORAGE_SECRET_KEY=...
```

### MinIO

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
make prod-local-clean

# Preserves storage data
make prod-local-down
```
