"""
Object Storage Module

Provides S3-compatible object storage functionality for Aurora.
Supports SeaweedFS (default), AWS S3, and any S3-compatible service.

Usage:
    from utils.storage.storage import get_storage_manager

    storage = get_storage_manager(user_id="user123")
    storage.upload_file(file_obj, "path/to/file.txt")

All components are Apache 2.0 licensed.
"""

import io
import json
import logging
import mimetypes
import os
import tempfile
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import BinaryIO, Dict, Iterator, List, Optional, Tuple, Union

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError
from werkzeug.datastructures import FileStorage

from utils.cache.redis_client import get_redis_client

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

class StorageBackendType(Enum):
    """Supported storage backend types."""

    S3 = "s3"


@dataclass
class StorageConfig:
    """Storage configuration loaded from environment variables."""
    backend_type: StorageBackendType = StorageBackendType.S3
    bucket: str = ""
    endpoint_url: Optional[str] = None
    access_key: Optional[str] = None
    secret_key: Optional[str] = None
    region: str = "us-east-1"
    use_ssl: bool = False
    verify_ssl: bool = True  # Default to True for security
    cache_enabled: bool = True
    cache_ttl: int = 60
    max_file_size_mb: int = 100  # Default 100MB limit

    # Default insecure credentials that should not be used in production
    _INSECURE_CREDENTIALS = {"admin", "password", "secret", "changeme", "default"}

    @classmethod
    def from_env(cls) -> "StorageConfig":
        """Load configuration from environment variables."""
        aurora_env = os.getenv("AURORA_ENV", "dev").lower()
        access_key = os.getenv("STORAGE_ACCESS_KEY")
        secret_key = os.getenv("STORAGE_SECRET_KEY")
        bucket = os.getenv("STORAGE_BUCKET")
        endpoint_url = os.getenv("STORAGE_ENDPOINT_URL")

        if not all([access_key, secret_key, bucket]):
            raise ValueError(
                "Missing required storage configuration. "
                "Set STORAGE_ACCESS_KEY, STORAGE_SECRET_KEY, and STORAGE_BUCKET environment variables."
            )
        use_ssl = os.getenv("STORAGE_USE_SSL", "false").lower() in ("1", "true", "yes")
        verify_ssl = os.getenv("STORAGE_VERIFY_SSL", "true").lower() in (
            "1",
            "true",
            "yes",
        )

        # Warn about insecure credentials in non-dev environments
        if aurora_env in ("prod", "production", "staging"):
            if (
                access_key in cls._INSECURE_CREDENTIALS
                or secret_key in cls._INSECURE_CREDENTIALS
            ):
                logger.warning(
                    "SECURITY WARNING: Using default/insecure storage credentials in %s environment. "
                    "Set STORAGE_ACCESS_KEY and STORAGE_SECRET_KEY to secure values.",
                    aurora_env,
                )
            if use_ssl and not verify_ssl:
                logger.warning(
                    "SECURITY WARNING: SSL verification is disabled while SSL is enabled. "
                    "This is vulnerable to MITM attacks. Set STORAGE_VERIFY_SSL=true in production."
                )

        return cls(
            backend_type=StorageBackendType.S3,
            bucket=bucket,
            endpoint_url=endpoint_url or None,
            access_key=access_key,
            secret_key=secret_key,
            use_ssl=use_ssl,
            verify_ssl=verify_ssl,
            # Other settings use dataclass defaults (region, cache_enabled, cache_ttl, max_file_size_mb)
        )


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class FileInfo:
    """Metadata about a stored file."""

    name: str
    path: str
    size: Optional[int] = None
    content_type: Optional[str] = None
    created: Optional[datetime] = None
    modified: Optional[datetime] = None
    etag: Optional[str] = None
    storage_uri: Optional[str] = None
    metadata: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "name": self.name,
            "path": self.path,
            "size": self.size,
            "content_type": self.content_type,
            "created": self.created.isoformat() if self.created else None,
            "modified": self.modified.isoformat() if self.modified else None,
            "etag": self.etag,
            "storage_uri": self.storage_uri,
            "metadata": self.metadata,
        }


# =============================================================================
# Exceptions
# =============================================================================


class StorageError(Exception):
    """Base exception for storage operations."""

    def __init__(self, message: str, path: str = None, cause: Exception = None):
        super().__init__(message)
        self.path = path
        self.cause = cause


class StorageNotFoundError(StorageError):
    """File not found in storage."""
    pass


class StorageUploadError(StorageError):
    """Failed to upload file."""
    pass


class StorageDownloadError(StorageError):
    """Failed to download file."""
    pass


class StorageConnectionError(StorageError):
    """Failed to connect to storage backend."""
    pass


# =============================================================================
# Cache Layer
# =============================================================================

class StorageCache:
    """Redis-based cache for storage listings."""

    def __init__(self, config: StorageConfig):
        self._enabled = config.cache_enabled
        self._ttl = config.cache_ttl
        self._bucket = config.bucket

    def _get_client(self):
        """Get Redis client if caching is enabled."""
        if not self._enabled:
            return None
        return get_redis_client()

    def _key(self, user_id: str, prefix: str, extension: Optional[str] = None) -> str:
        """Generate cache key."""
        ext = extension or "*"
        return f"storage:files:v1:{self._bucket}:{user_id}:{prefix.strip('/')}:{ext}"

    def get(
        self, user_id: str, prefix: str, extension: Optional[str] = None
    ) -> Optional[List[Dict]]:
        """Get cached file listing."""
        client = self._get_client()
        if not client:
            return None

        try:
            data = client.get(self._key(user_id, prefix, extension))
            if data:
                payload = json.loads(data)
                items = payload.get("items", payload)
                logger.debug(
                    f"Cache HIT: user={user_id}, prefix={prefix}, items={len(items)}"
                )
                return items
        except Exception as e:
            logger.debug(f"Cache get error: {e}")
        return None

    def set(
        self,
        user_id: str,
        prefix: str,
        items: List[Dict],
        extension: Optional[str] = None,
    ):
        """Cache file listing."""
        client = self._get_client()
        if not client:
            return

        try:
            payload = json.dumps(
                {
                    "items": items,
                    "count": len(items),
                    "cached_at": time.time(),
                }
            )
            client.setex(self._key(user_id, prefix, extension), self._ttl, payload)
            logger.debug(
                f"Cache SET: user={user_id}, prefix={prefix}, items={len(items)}"
            )
        except Exception as e:
            logger.debug(f"Cache set error: {e}")

    def invalidate(self, user_id: str, prefix: str = ""):
        """Invalidate cache entries for a user/prefix."""
        client = self._get_client()
        if not client:
            return 0

        try:
            pattern = f"storage:files:v1:{self._bucket}:{user_id}:{prefix.strip('/')}*"
            deleted = 0
            for key in client.scan_iter(pattern):
                client.delete(key)
                deleted += 1
            if deleted:
                logger.debug(f"Cache invalidated: {deleted} keys for user={user_id}")
            return deleted
        except Exception as e:
            logger.debug(f"Cache invalidate error: {e}")
            return 0


# =============================================================================
# Storage Backend (Abstract)
# =============================================================================

class StorageBackend(ABC):
    """Abstract base class for storage backends."""

    @abstractmethod
    def upload(
        self,
        file_obj: BinaryIO,
        path: str,
        content_type: Optional[str] = None,
        metadata: Optional[Dict[str, str]] = None,
    ) -> str:
        """Upload a file to storage."""
        pass

    @abstractmethod
    def download(self, path: str) -> bytes:
        """Download a file as bytes."""
        pass

    @abstractmethod
    def download_to_file(self, path: str, destination: str) -> str:
        """Download a file to local filesystem."""
        pass

    @abstractmethod
    def delete(self, path: str) -> bool:
        """Delete a file."""
        pass

    @abstractmethod
    def exists(self, path: str) -> bool:
        """Check if a file exists."""
        pass

    @abstractmethod
    def get_info(self, path: str) -> Optional[FileInfo]:
        """Get file metadata."""
        pass

    @abstractmethod
    def list_files(
        self,
        prefix: str = "",
        max_results: Optional[int] = None,
    ) -> List[FileInfo]:
        """List files with optional prefix filter."""
        pass

    @abstractmethod
    def generate_presigned_url(
        self,
        path: str,
        expiration: int = 3600,
        method: str = "GET",
    ) -> str:
        """Generate a presigned URL for direct access."""
        pass

    @abstractmethod
    def delete_prefix(self, prefix: str) -> int:
        """Delete all files with a given prefix."""
        pass

    @property
    @abstractmethod
    def bucket_name(self) -> str:
        """Get the bucket name."""
        pass


# =============================================================================
# S3 Backend Implementation
# =============================================================================

class S3Backend(StorageBackend):
    """
    S3-compatible storage backend.

    Supports:
    - SeaweedFS (default)
    - AWS S3
    - Cloudflare R2
    - Backblaze B2
    - MinIO
    - Any S3-compatible service
    """

    def __init__(self, config: StorageConfig):
        self._config = config
        self._client = None

    @property
    def client(self):
        """Lazy-initialize S3 client."""
        if self._client is None:
            self._client = self._create_client()
        return self._client

    def _create_client(self):
        """Create boto3 S3 client with configuration."""
        try:
            client_config = Config(
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=10,
                read_timeout=30,
            )

            client_kwargs = {
                "service_name": "s3",
                "config": client_config,
                "region_name": self._config.region,
                "use_ssl": self._config.use_ssl,
                "verify": self._config.verify_ssl,
            }

            # Custom endpoint for non-AWS services
            if self._config.endpoint_url:
                client_kwargs["endpoint_url"] = self._config.endpoint_url

            # Credentials
            if self._config.access_key:
                client_kwargs["aws_access_key_id"] = self._config.access_key
            if self._config.secret_key:
                client_kwargs["aws_secret_access_key"] = self._config.secret_key

            client = boto3.client(**client_kwargs)

            endpoint_desc = self._config.endpoint_url or "AWS S3"
            logger.info(
                f"S3 client initialized: endpoint={endpoint_desc}, bucket={self._config.bucket}"
            )

            return client

        except NoCredentialsError as e:
            raise StorageConnectionError(
                "S3 credentials not configured. Set STORAGE_ACCESS_KEY and STORAGE_SECRET_KEY.",
                cause=e,
            )
        except Exception as e:
            raise StorageConnectionError(
                f"Failed to initialize S3 client: {e}", cause=e
            )

    def _guess_content_type(self, path: str) -> str:
        """Guess content type from file path."""
        content_type, _ = mimetypes.guess_type(path)
        return content_type or "application/octet-stream"

    def _normalize_path(self, path: str) -> str:
        """Normalize path by removing leading slashes."""
        return path.lstrip("/")

    def _handle_error(self, e: ClientError, path: str, operation: str):
        """Convert boto3 ClientError to StorageError."""
        error_code = e.response.get("Error", {}).get("Code", "")
        error_message = e.response.get("Error", {}).get("Message", str(e))

        if error_code in ("404", "NoSuchKey", "NotFound"):
            raise StorageNotFoundError(f"File not found: {path}", path=path, cause=e)
        elif error_code in ("NoSuchBucket",):
            raise StorageConnectionError(
                f"Bucket not found: {self._config.bucket}", cause=e
            )
        else:
            if operation == "upload":
                raise StorageUploadError(
                    f"Upload failed for {path}: {error_message}", path=path, cause=e
                )
            elif operation == "download":
                raise StorageDownloadError(
                    f"Download failed for {path}: {error_message}", path=path, cause=e
                )
            else:
                raise StorageError(
                    f"Operation '{operation}' failed for {path}: {error_message}",
                    path=path,
                    cause=e,
                )

    @property
    def bucket_name(self) -> str:
        return self._config.bucket

    def upload(
        self,
        file_obj: BinaryIO,
        path: str,
        content_type: Optional[str] = None,
        metadata: Optional[Dict[str, str]] = None,
    ) -> str:
        """Upload a file to S3."""
        path = self._normalize_path(path)

        # Check file size if possible
        max_bytes = self._config.max_file_size_mb * 1024 * 1024
        if hasattr(file_obj, "seek") and hasattr(file_obj, "tell"):
            try:
                current_pos = file_obj.tell()
                file_obj.seek(0, 2)  # Seek to end
                size = file_obj.tell()
                file_obj.seek(current_pos)  # Reset to original position

                if size > max_bytes:
                    raise StorageUploadError(
                        f"File too large: {size} bytes (max {max_bytes} bytes / {self._config.max_file_size_mb}MB)",
                        path=path,
                    )
            except (OSError, IOError) as e:
                logger.debug(
                    f"Could not determine file size for {path}, proceeding with upload: {e}"
                )

        # Seek to start if possible
        if hasattr(file_obj, "seek"):
            try:
                file_obj.seek(0)
            except Exception:
                pass

        extra_args = {
            "ContentType": content_type or self._guess_content_type(path),
        }
        if metadata:
            extra_args["Metadata"] = metadata

        try:
            self.client.upload_fileobj(
                file_obj,
                self._config.bucket,
                path,
                ExtraArgs=extra_args,
            )
            uri = f"s3://{self._config.bucket}/{path}"
            logger.debug(f"Uploaded: {uri}")
            return uri

        except ClientError as e:
            self._handle_error(e, path, "upload")
        except EndpointConnectionError as e:
            raise StorageConnectionError(
                f"Cannot connect to storage endpoint: {self._config.endpoint_url}",
                path=path,
                cause=e,
            )

    def upload_bytes(
        self,
        data: bytes,
        path: str,
        content_type: Optional[str] = None,
        metadata: Optional[Dict[str, str]] = None,
    ) -> str:
        """Upload bytes to S3."""
        return self.upload(io.BytesIO(data), path, content_type, metadata)

    def upload_from_file(
        self,
        local_path: str,
        storage_path: str,
        content_type: Optional[str] = None,
        metadata: Optional[Dict[str, str]] = None,
    ) -> str:
        """Upload a local file to S3."""
        storage_path = self._normalize_path(storage_path)

        extra_args = {
            "ContentType": content_type or self._guess_content_type(storage_path),
        }
        if metadata:
            extra_args["Metadata"] = metadata

        try:
            self.client.upload_file(
                local_path,
                self._config.bucket,
                storage_path,
                ExtraArgs=extra_args,
            )
            uri = f"s3://{self._config.bucket}/{storage_path}"
            logger.info(f"Uploaded: {uri}")
            return uri

        except ClientError as e:
            self._handle_error(e, storage_path, "upload")

    def download(self, path: str) -> bytes:
        """Download a file as bytes."""
        path = self._normalize_path(path)
        buffer = io.BytesIO()

        try:
            self.client.download_fileobj(self._config.bucket, path, buffer)
            buffer.seek(0)
            return buffer.read()

        except ClientError as e:
            self._handle_error(e, path, "download")

    def download_to_file(self, path: str, destination: str) -> str:
        """Download a file to local filesystem."""
        path = self._normalize_path(path)

        try:
            self.client.download_file(self._config.bucket, path, destination)
            return destination

        except ClientError as e:
            self._handle_error(e, path, "download")

    def download_to_temp(
        self, path: str, suffix: Optional[str] = None
    ) -> Tuple[str, str]:
        """Download to a temporary file. Returns (temp_path, filename)."""
        path = self._normalize_path(path)
        filename = os.path.basename(path)

        if suffix is None and "." in filename:
            suffix = "." + filename.rsplit(".", 1)[-1]

        temp_dir = tempfile.mkdtemp()
        temp_path = os.path.join(temp_dir, filename)

        self.download_to_file(path, temp_path)
        return temp_path, filename

    def delete(self, path: str) -> bool:
        """Delete a file."""
        path = self._normalize_path(path)

        try:
            if not self.exists(path):
                return False

            self.client.delete_object(Bucket=self._config.bucket, Key=path)
            logger.info(f"Deleted: s3://{self._config.bucket}/{path}")
            return True

        except ClientError as e:
            self._handle_error(e, path, "delete")

    def delete_prefix(self, prefix: str) -> int:
        """Delete all files with a given prefix."""
        prefix = self._normalize_path(prefix)
        deleted = 0

        try:
            paginator = self.client.get_paginator("list_objects_v2")
            pages = paginator.paginate(Bucket=self._config.bucket, Prefix=prefix)

            for page in pages:
                contents = page.get("Contents", [])
                if not contents:
                    continue

                # Delete in batches (S3 limit is 1000)
                delete_keys = [{"Key": obj["Key"]} for obj in contents]
                response = self.client.delete_objects(
                    Bucket=self._config.bucket,
                    Delete={"Objects": delete_keys},
                )
                deleted += len(response.get("Deleted", []))

            if deleted:
                logger.info(f"Deleted {deleted} files with prefix: {prefix}")
            return deleted

        except ClientError as e:
            self._handle_error(e, prefix, "delete")

    def exists(self, path: str) -> bool:
        """Check if a file exists."""
        path = self._normalize_path(path)

        try:
            self.client.head_object(Bucket=self._config.bucket, Key=path)
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                return False
            raise

    def get_info(self, path: str) -> Optional[FileInfo]:
        """Get file metadata."""
        path = self._normalize_path(path)

        try:
            response = self.client.head_object(Bucket=self._config.bucket, Key=path)

            return FileInfo(
                name=os.path.basename(path),
                path=path,
                size=response.get("ContentLength"),
                content_type=response.get("ContentType"),
                modified=response.get("LastModified"),
                etag=response.get("ETag", "").strip('"'),
                storage_uri=f"s3://{self._config.bucket}/{path}",
                metadata=response.get("Metadata", {}),
            )

        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                return None
            self._handle_error(e, path, "get_info")

    def list_files(
        self,
        prefix: str = "",
        max_results: Optional[int] = None,
    ) -> List[FileInfo]:
        """List files with optional prefix filter."""
        prefix = self._normalize_path(prefix) if prefix else ""
        files = []

        try:
            paginator = self.client.get_paginator("list_objects_v2")
            pagination_config = {}
            if max_results:
                pagination_config["MaxItems"] = max_results

            pages = paginator.paginate(
                Bucket=self._config.bucket,
                Prefix=prefix,
                PaginationConfig=pagination_config,
            )

            for page in pages:
                for obj in page.get("Contents", []):
                    files.append(
                        FileInfo(
                            name=os.path.basename(obj["Key"]),
                            path=obj["Key"],
                            size=obj.get("Size"),
                            modified=obj.get("LastModified"),
                            etag=obj.get("ETag", "").strip('"'),
                            storage_uri=f"s3://{self._config.bucket}/{obj['Key']}",
                        )
                    )
                    if max_results and len(files) >= max_results:
                        return files

            return files

        except ClientError as e:
            self._handle_error(e, prefix, "list")

    # Maximum presigned URL expiration: 7 days (AWS S3 limit)
    MAX_PRESIGNED_EXPIRATION = 7 * 24 * 3600  # 604800 seconds

    def generate_presigned_url(
        self,
        path: str,
        expiration: int = 3600,
        method: str = "GET",
    ) -> str:
        """Generate a presigned URL for direct access."""
        # Validate expiration to prevent extremely long-lived URLs
        if expiration > self.MAX_PRESIGNED_EXPIRATION:
            raise ValueError(
                f"Expiration too long: {expiration}s (max {self.MAX_PRESIGNED_EXPIRATION}s / 7 days)"
            )
        if expiration < 1:
            raise ValueError(f"Expiration must be positive: {expiration}s")

        path = self._normalize_path(path)
        client_method = "get_object" if method.upper() == "GET" else "put_object"

        try:
            url = self.client.generate_presigned_url(
                ClientMethod=client_method,
                Params={"Bucket": self._config.bucket, "Key": path},
                ExpiresIn=expiration,
            )
            return url

        except ClientError as e:
            self._handle_error(e, path, "presign")


# =============================================================================
# Storage Manager
# =============================================================================


class StorageManager:
    """
    High-level storage manager with user scoping and caching.

    Provides:
    - User-scoped file paths (users/{user_id}/...)
    - Redis caching for file listings
    - Convenient methods for common operations
    """

    def __init__(
        self,
        backend: StorageBackend,
        user_id: Optional[str] = None,
        cache: Optional[StorageCache] = None,
    ):
        self._backend = backend
        self._user_id = user_id
        self._cache = cache

    @property
    def backend(self) -> StorageBackend:
        """Access the underlying storage backend."""
        return self._backend

    @property
    def user_id(self) -> Optional[str]:
        """Get the user ID this manager is scoped to."""
        return self._user_id

    @property
    def bucket_name(self) -> str:
        """Get the bucket name."""
        return self._backend.bucket_name

    def _user_path(self, path: str, user_id: Optional[str] = None) -> str:
        """Build user-scoped path with path traversal protection."""
        effective_user_id = user_id or self._user_id
        if not effective_user_id:
            return path.lstrip("/")

        # Sanitize path to prevent directory traversal attacks
        clean_path = path.lstrip("/")
        # Normalize the path and convert to forward slashes
        clean_path = os.path.normpath(clean_path).replace("\\", "/")
        # Reject paths that try to escape the user directory
        if (
            clean_path.startswith("..")
            or "/../" in clean_path
            or clean_path.endswith("/..")
        ):
            raise ValueError(
                f"Invalid path: '{path}' (directory traversal not allowed)"
            )

        # Avoid double-prefixing
        if clean_path.startswith(f"users/{effective_user_id}/"):
            return clean_path
        return f"users/{effective_user_id}/{clean_path}"

    def _invalidate_cache(self, path: str, user_id: Optional[str] = None):
        """Invalidate cache for affected paths."""
        if not self._cache:
            return

        effective_user_id = user_id or self._user_id
        if effective_user_id:
            # Invalidate parent directory cache
            parent = "/".join(path.split("/")[:-1])
            if parent:
                self._cache.invalidate(effective_user_id, parent)
            self._cache.invalidate(effective_user_id, "")

    # -------------------------------------------------------------------------
    # Upload Operations
    # -------------------------------------------------------------------------

    def upload_file(
        self,
        file_obj: Union[BinaryIO, FileStorage],
        path: str,
        user_id: Optional[str] = None,
        content_type: Optional[str] = None,
        metadata: Optional[Dict[str, str]] = None,
    ) -> str:
        """
        Upload a file to storage.

        Args:
            file_obj: File-like object or Flask FileStorage
            path: Destination path
            user_id: Optional user ID override
            content_type: MIME type (auto-detected if not provided)
            metadata: Optional metadata dict

        Returns:
            Storage URI (s3://bucket/path)
        """
        full_path = self._user_path(path, user_id)

        # Get content type from FileStorage if available
        if content_type is None and hasattr(file_obj, "content_type"):
            content_type = file_obj.content_type

        result = self._backend.upload(file_obj, full_path, content_type, metadata)
        self._invalidate_cache(full_path, user_id)
        return result

    def upload_bytes(
        self,
        data: bytes,
        path: str,
        user_id: Optional[str] = None,
        content_type: Optional[str] = None,
        metadata: Optional[Dict[str, str]] = None,
    ) -> str:
        """Upload bytes to storage."""
        full_path = self._user_path(path, user_id)
        result = self._backend.upload(
            io.BytesIO(data), full_path, content_type, metadata
        )
        self._invalidate_cache(full_path, user_id)
        return result

    def upload_from_file(
        self,
        local_path: str,
        storage_path: str,
        user_id: Optional[str] = None,
        content_type: Optional[str] = None,
        metadata: Optional[Dict[str, str]] = None,
    ) -> str:
        """Upload a local file to storage."""
        full_path = self._user_path(storage_path, user_id)
        result = self._backend.upload_from_file(
            local_path, full_path, content_type, metadata
        )
        self._invalidate_cache(full_path, user_id)
        return result

    # -------------------------------------------------------------------------
    # Download Operations
    # -------------------------------------------------------------------------

    def download_bytes(self, path: str, user_id: Optional[str] = None) -> bytes:
        """Download file contents as bytes."""
        full_path = self._user_path(path, user_id)
        return self._backend.download(full_path)

    def download_to_file(
        self,
        path: str,
        destination: str,
        user_id: Optional[str] = None,
    ) -> str:
        """Download a file to local filesystem."""
        full_path = self._user_path(path, user_id)
        return self._backend.download_to_file(full_path, destination)

    def download_to_temp_file(
        self,
        path: str,
        user_id: Optional[str] = None,
    ) -> Tuple[str, str]:
        """
        Download to a temporary file.

        Returns:
            Tuple of (temp_file_path, original_filename)
        """
        full_path = self._user_path(path, user_id)
        return self._backend.download_to_temp(full_path)

    def download_to_stream(self, path: str, user_id: Optional[str] = None) -> bytes:
        """Download file to memory. Alias for download_bytes."""
        return self.download_bytes(path, user_id)

    # -------------------------------------------------------------------------
    # Delete Operations
    # -------------------------------------------------------------------------

    def delete_file(self, path: str, user_id: Optional[str] = None) -> bool:
        """Delete a file."""
        full_path = self._user_path(path, user_id)
        result = self._backend.delete(full_path)
        self._invalidate_cache(full_path, user_id)
        return result

    def delete_files_with_prefix(
        self, prefix: str, user_id: Optional[str] = None
    ) -> int:
        """Delete all files with a given prefix."""
        full_prefix = self._user_path(prefix, user_id)
        result = self._backend.delete_prefix(full_prefix)
        self._invalidate_cache(full_prefix, user_id)
        return result

    # -------------------------------------------------------------------------
    # Query Operations
    # -------------------------------------------------------------------------

    def file_exists(self, path: str, user_id: Optional[str] = None) -> bool:
        """Check if a file exists."""
        full_path = self._user_path(path, user_id)
        return self._backend.exists(full_path)

    def get_file_info(self, path: str, user_id: Optional[str] = None) -> Optional[Dict]:
        """Get file metadata as dict."""
        full_path = self._user_path(path, user_id)
        info = self._backend.get_info(full_path)
        return info.to_dict() if info else {}

    def list_files(
        self,
        prefix: str = "",
        user_id: Optional[str] = None,
        max_results: Optional[int] = None,
    ) -> List[Dict]:
        """List files with optional prefix filter."""
        full_prefix = (
            self._user_path(prefix, user_id) if (user_id or self._user_id) else prefix
        )
        files = self._backend.list_files(full_prefix, max_results)
        return [f.to_dict() for f in files]

    def list_user_files(
        self,
        user_id: Optional[str] = None,
        prefix: str = "",
        extension: Optional[str] = None,
        max_results: int = 200,
    ) -> List[Dict]:
        """
        List files for a user with caching support.

        Args:
            user_id: User ID (uses manager's user_id if not provided)
            prefix: Optional path prefix filter
            extension: Optional file extension filter
            max_results: Maximum files to return
        """
        effective_user_id = user_id or self._user_id
        if not effective_user_id:
            raise ValueError("user_id is required")

        # Check cache
        if self._cache:
            cached = self._cache.get(effective_user_id, prefix, extension)
            if cached is not None:
                return cached

        # Build full prefix
        full_prefix = (
            f"users/{effective_user_id}/{prefix.lstrip('/')}"
            if prefix
            else f"users/{effective_user_id}/"
        )

        # Fetch from backend
        files = self._backend.list_files(full_prefix, max_results)

        # Filter by extension if specified
        if extension:
            files = [f for f in files if f.path.endswith(extension)]

        result = [f.to_dict() for f in files]

        # Update cache
        if self._cache:
            self._cache.set(effective_user_id, prefix, result, extension)

        return result

    # -------------------------------------------------------------------------
    # URL Generation
    # -------------------------------------------------------------------------

    def generate_presigned_url(
        self,
        path: str,
        user_id: Optional[str] = None,
        expiration: int = 3600,
        method: str = "GET",
    ) -> str:
        """Generate a presigned URL for direct access."""
        full_path = self._user_path(path, user_id)
        return self._backend.generate_presigned_url(full_path, expiration, method)

    # -------------------------------------------------------------------------
    # Utility Methods
    # -------------------------------------------------------------------------

    def generate_unique_filename(
        self,
        filename: str,
        user_id: Optional[str] = None,
    ) -> str:
        """Generate a unique filename if the original already exists."""
        if not self.file_exists(filename, user_id):
            return filename

        base, ext = os.path.splitext(filename)
        counter = 1

        while counter < 1000:
            new_name = f"{base} ({counter}){ext}"
            if not self.file_exists(new_name, user_id):
                return new_name
            counter += 1

        # Fallback to UUID
        return f"{base}_{uuid.uuid4().hex[:8]}{ext}"


# =============================================================================
# Factory Functions
# =============================================================================

_config: Optional[StorageConfig] = None
_backend: Optional[StorageBackend] = None
_cache: Optional[StorageCache] = None
_manager_cache: Dict[str, StorageManager] = {}


def _get_config() -> StorageConfig:
    """Get or create the global storage configuration."""
    global _config
    if _config is None:
        _config = StorageConfig.from_env()
    return _config


def _get_backend() -> StorageBackend:
    """Get or create the global storage backend."""
    global _backend
    if _backend is None:
        config = _get_config()
        _backend = S3Backend(config)

        # Verify bucket exists on startup
        try:
            _backend.client.head_bucket(Bucket=config.bucket)
            logger.info(f"Verified bucket exists: {config.bucket}")
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code in ("404", "NoSuchBucket"):
                raise StorageConnectionError(
                    f"Bucket '{config.bucket}' does not exist. "
                    "Ensure the storage backend is running and the bucket is created."
                )
            # Other errors (e.g., access denied) - log but don't fail
            logger.warning(f"Could not verify bucket '{config.bucket}': {e}")
        except EndpointConnectionError as e:
            logger.warning(
                f"Could not connect to storage endpoint to verify bucket: {e}. "
                "Storage operations may fail."
            )

    return _backend


def _get_cache() -> StorageCache:
    """Get or create the global cache."""
    global _cache
    if _cache is None:
        config = _get_config()
        _cache = StorageCache(config)
    return _cache


def get_storage_manager(user_id: Optional[str] = None) -> StorageManager:
    """
    Get a storage manager instance.

    Args:
        user_id: Optional user ID for scoped operations.
                 Paths will be prefixed with users/{user_id}/.

    Returns:
        StorageManager instance
    """
    cache_key = user_id or "__global__"

    if cache_key not in _manager_cache:
        _manager_cache[cache_key] = StorageManager(
            backend=_get_backend(),
            user_id=user_id,
            cache=_get_cache(),
        )

    return _manager_cache[cache_key]


def reset_storage():
    """Reset all storage instances. Useful for testing."""
    global _config, _backend, _cache, _manager_cache
    _config = None
    _backend = None
    _cache = None
    _manager_cache = {}


# =============================================================================
# Convenience Functions
# =============================================================================


def upload_zip_to_storage(
    file_obj: Union[BinaryIO, FileStorage],
    user_id: str,
    filename: Optional[str] = None,
) -> str:
    """
    Upload a zip file to storage.

    Args:
        file_obj: File object to upload
        user_id: User ID
        filename: Optional filename (defaults to file_obj.filename or 'upload.zip')

    Returns:
        Storage URI
    """
    storage = get_storage_manager(user_id=user_id)

    if filename is None:
        filename = getattr(file_obj, "filename", "upload.zip")

    unique_filename = storage.generate_unique_filename(filename)
    return storage.upload_file(
        file_obj,
        f"uploads/{unique_filename}",
        content_type="application/zip",
    )


def download_zip_from_storage(
    storage_path: str,
    user_id: Optional[str] = None,
) -> Tuple[str, str]:
    """
    Download a zip file from storage.

    Args:
        storage_path: Storage path or URI (s3://bucket/path)
        user_id: Optional user ID

    Returns:
        Tuple of (local_temp_path, filename)
    """
    # Parse storage URI if provided
    if storage_path.startswith("s3://"):
        # Extract path from URI
        parts = storage_path[5:].split("/", 1)
        if len(parts) > 1:
            storage_path = parts[1]

    storage = get_storage_manager(user_id=user_id)
    return storage.download_to_temp_file(storage_path)


def upload_directory_to_storage(
    local_dir: str,
    user_id: str,
    session_id: str,
    storage_subdir: str = "terraform_dir",
) -> List[str]:
    """
    Upload all files in a directory to storage.

    Args:
        local_dir: Local directory path
        user_id: User ID
        session_id: Session ID for path organization
        storage_subdir: Subdirectory name in storage

    Returns:
        List of storage URIs for uploaded files
    """
    storage = get_storage_manager(user_id=user_id)
    uploaded = []

    for root, _, files in os.walk(local_dir):
        for filename in files:
            local_path = os.path.join(root, filename)
            rel_path = os.path.relpath(local_path, local_dir)
            storage_path = f"{session_id}/{storage_subdir}/{rel_path}"

            with open(local_path, "rb") as f:
                uri = storage.upload_file(f, storage_path)
                uploaded.append(uri)

    return uploaded
