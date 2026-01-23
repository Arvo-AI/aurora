import json
import os
import hmac
import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from cachetools import TTLCache
from chat.backend.agent.utils.telemetry import emit_cache_event

try:
    import redis  # type: ignore
except Exception:  # pragma: no cover
    redis = None  # Optional dependency

logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    """Lightweight cache entry for prefix fingerprints.

    Only stores non-PII metadata in cleartext. Do not store full prompt bodies here.
    """
    key: str
    provider: str
    tenant_id: str
    prefix_sha256: str
    tools_fingerprint: str
    created_at_ms: int
    ttl_s: int
    vendor_token: Optional[str] = None  # Placeholder for providers that return rehydration tokens


class PrefixCacheBackend:
    """Abstract cache backend."""

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def set(self, key: str, value: Dict[str, Any], ttl_s: int) -> None:
        raise NotImplementedError

    def clear(self) -> None:
        raise NotImplementedError

    def invalidate_by_provider(self, provider: str) -> None:
        raise NotImplementedError


class InMemoryPrefixCacheBackend(PrefixCacheBackend):
    def __init__(self, maxsize: int, ttl_s: int):
        # TTLCache evicts LRU and respects TTL
        self.cache = TTLCache(maxsize=maxsize, ttl=ttl_s)
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self.cache.get(key)

    def set(self, key: str, value: Dict[str, Any], ttl_s: int) -> None:
        with self._lock:
            # TTLCache uses its own ttl; we can just set
            self.cache.ttl = ttl_s
            self.cache[key] = value

    def clear(self) -> None:
        with self._lock:
            self.cache.clear()

    def invalidate_by_provider(self, provider: str) -> None:
        with self._lock:
            keys_to_delete = []
            for k in list(self.cache.keys()):
                meta = self.cache.get(k)
                if isinstance(meta, dict) and meta.get("provider") == provider:
                    keys_to_delete.append(k)
            for k in keys_to_delete:
                self.cache.pop(k, None)


class RedisPrefixCacheBackend(PrefixCacheBackend):
    def __init__(self, url: str, namespace: str = "aurora:prefixcache"):
        if not redis:
            raise RuntimeError("redis library not available")
        self.client = redis.from_url(url)
        self.ns = namespace.rstrip(":")

    def _rk(self, key: str) -> str:
        return f"{self.ns}:{key}"

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        raw = self.client.get(self._rk(key))
        if not raw:
            return None
        try:
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", errors="replace")
            return json.loads(raw)
        except Exception:
            return None

    def set(self, key: str, value: Dict[str, Any], ttl_s: int) -> None:
        try:
            self.client.set(self._rk(key), json.dumps(value, ensure_ascii=False), ex=ttl_s)
        except Exception as e:
            logger.warning(f"Redis set failed: {e}")

    def clear(self) -> None:
        try:
            cursor = 0
            pattern = f"{self.ns}:*"
            while True:
                cursor, keys = self.client.scan(cursor=cursor, match=pattern, count=500)
                if keys:
                    self.client.delete(*keys)
                if cursor == 0:
                    break
        except Exception as e:
            logger.warning(f"Redis clear failed: {e}")

    def invalidate_by_provider(self, provider: str) -> None:
        try:
            cursor = 0
            pattern = f"{self.ns}:*"
            while True:
                cursor, keys = self.client.scan(cursor=cursor, match=pattern, count=500)
                if not keys:
                    if cursor == 0:
                        break
                    continue
                for k in keys:
                    try:
                        raw = self.client.get(k)
                        if not raw:
                            continue
                        data = json.loads(raw)
                        if data.get("provider") == provider:
                            self.client.delete(k)
                    except Exception:
                        continue
                if cursor == 0:
                    break
        except Exception as e:
            logger.warning(f"Redis invalidate_by_provider failed: {e}")


# Prefix Cache Configuration
PREFIX_CACHE_TTL = 3600  # 1 hour - TTL for cached prefix segments
PREFIX_CACHE_MAXSIZE = 1000  # Maximum number of cached entries
USE_REDIS_PREFIX_CACHE = False  # Whether to use Redis for prefix cache (requires REDIS_URL)

class PrefixCacheManager:
    """Singleton manager for prefix canonicalization + caching."""

    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        self.secret = os.getenv("OPENROUTER_API_KEY") or "aurora-dev-secret"
        # Config
        self.default_ttl_s = PREFIX_CACHE_TTL
        self.maxsize = PREFIX_CACHE_MAXSIZE
        self.use_redis = USE_REDIS_PREFIX_CACHE
        self.redis_url = os.getenv("REDIS_URL")

        # Backend selection
        if self.use_redis and self.redis_url and redis is not None:
            try:
                self.backend: PrefixCacheBackend = RedisPrefixCacheBackend(self.redis_url)
                logger.info("PrefixCacheManager using Redis backend")
            except Exception as e:
                logger.warning(f"Redis backend unavailable ({e}); falling back to in-memory cache")
                self.backend = InMemoryPrefixCacheBackend(self.maxsize, self.default_ttl_s)
        else:
            self.backend = InMemoryPrefixCacheBackend(self.maxsize, self.default_ttl_s)
            logger.info("PrefixCacheManager using in-memory LRU TTL cache")

        # Track last known tool fingerprint by provider for invalidation
        self._last_tools_fingerprint: Dict[str, str] = {}

    @classmethod
    def get_instance(cls) -> "PrefixCacheManager":
        with cls._lock:
            if not cls._instance:
                cls._instance = PrefixCacheManager()
            return cls._instance

    # ------------------------ Canonicalization ------------------------

    @staticmethod
    def _normalize_whitespace(text: str) -> str:
        if not isinstance(text, str):
            text = str(text)
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        # Collapse all whitespace runs to a single space, preserve basic sentence boundaries
        collapsed = re.sub(r"\s+", " ", text).strip()
        return collapsed

    @staticmethod
    def _safe_tool_schema(tool: Any) -> Dict[str, Any]:
        # Extract a deterministic, minimal schema for the tool args
        try:
            args_schema = getattr(tool, "args_schema", None)
            if args_schema is None:
                return {}
            # Pydantic v2
            if hasattr(args_schema, "model_json_schema"):
                schema = args_schema.model_json_schema()
            elif hasattr(args_schema, "schema_json"):
                schema = json.loads(args_schema.schema_json())
            elif hasattr(args_schema, "schema"):
                schema = args_schema.schema()
            else:
                schema = {"title": getattr(args_schema, "__name__", "Args"), "type": "object"}
            # Remove descriptions to reduce noise and PII risk
            if isinstance(schema, dict):
                schema.pop("description", None)
                if "properties" in schema and isinstance(schema["properties"], dict):
                    for p in schema["properties"].values():
                        if isinstance(p, dict):
                            p.pop("description", None)
            return schema
        except Exception:
            return {}

    @classmethod
    def canonicalize_tools(cls, tools: List[Any]) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for t in tools or []:
            try:
                name = getattr(t, "name", t.__class__.__name__)
                desc = getattr(t, "description", "") or ""
                item = {
                    "name": name,
                    "description": cls._normalize_whitespace(desc)[:2000],  # bound size
                    "schema": cls._safe_tool_schema(t),
                }
                items.append(item)
            except Exception:
                continue
        # Deterministic ordering
        items.sort(key=lambda x: x.get("name", ""))
        return items

    @classmethod
    def build_canonical_prefix(cls, system_text: str, tools: List[Any]) -> str:
        canonical_obj = {
            "system": cls._normalize_whitespace(system_text or ""),
            "tools": cls.canonicalize_tools(tools or []),
        }
        # Strict JSON canonicalization: sorted keys, no spaces
        return json.dumps(canonical_obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    @staticmethod
    def _sha256_hex(data: str) -> str:
        return hashlib.sha256(data.encode("utf-8")).hexdigest()

    def _hmac_key(self, message: str) -> str:
        digest = hmac.new(self.secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
        return digest

    def _compute_tools_fingerprint(self, tools: List[Any]) -> str:
        tools_canon = json.dumps(self.canonicalize_tools(tools or []), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return self._sha256_hex(tools_canon)

    # ------------------------ Public API ------------------------

    def register_prefix(
        self,
        system_text: str,
        tools: List[Any],
        provider: str,
        tenant_id: str,
        ttl_s: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Canonicalize the prefix, compute HMAC cache key, check cache, and register if miss.

        Returns a dict with {hit: bool, key: str, meta: CacheEntry-like dict}.
        """
        ttl = int(ttl_s or self.default_ttl_s)

        # Try to detect if this prefix represents a named segment to improve telemetry clarity
        segment_name: Optional[str] = None
        try:
            seg_match = re.match(r"^\[SEGMENT:(.+?)\]", (system_text or "").lstrip())
            if seg_match:
                segment_name = seg_match.group(1).strip()
        except Exception:
            segment_name = None  # Gracefully ignore parse errors

        canonical_prefix = self.build_canonical_prefix(system_text, tools)
        prefix_sha = self._sha256_hex(canonical_prefix)
        key_material = f"{canonical_prefix}:{provider}:{tenant_id}"
        key = self._hmac_key(key_material)

        # Invalidate if tools changed (explicit invalidation rule)
        tools_fp = self._compute_tools_fingerprint(tools)
        prev_fp = self._last_tools_fingerprint.get(provider)
        if prev_fp and prev_fp != tools_fp:
            logger.info(f"Tool definitions changed for provider={provider}. Invalidating provider cache.")
            try:
                self.backend.invalidate_by_provider(provider)
                emit_cache_event(
                    "invalidate_provider",
                    {"provider": provider, **({"segment": segment_name} if segment_name else {})},
                )
            except Exception as e:
                logger.warning(f"Failed to invalidate provider cache: {e}")
        self._last_tools_fingerprint[provider] = tools_fp

        cached = self.backend.get(key)
        if cached:
            # Cache hit: return metadata; providers like Azure/OpenAI may support rehydration tokens in the future
            logger.debug(f"Prefix cache HIT for provider={provider}, tenant={tenant_id}")
            emit_cache_event(
                "hit",
                {
                    "provider": provider,
                    **({"segment": segment_name} if segment_name else {}),
                },
            )
            return {"hit": True, "key": key, "meta": cached}

        # Do NOT store tenant_id in cleartext; store only a hashed form
        tenant_hash = self._sha256_hex(tenant_id)
        meta: Dict[str, Any] = {
            "key": key,
            "provider": provider,
            "tenant_hash": tenant_hash,
            "prefix_sha256": prefix_sha,
            "tools_fingerprint": tools_fp,
            "created_at_ms": int(time.time() * 1000),
            "ttl_s": ttl,
            "segment": segment_name,
            # vendor_token: None for now (no rehydration token from provider)
        }

        try:
            self.backend.set(key, meta, ttl)
            logger.debug(f"Prefix cache MISS. Registered new fingerprint for provider={provider}")
            emit_cache_event(
                "miss_register",
                {
                    "provider": provider,
                    **({"segment": segment_name} if segment_name else {}),
                },
            )
        except Exception as e:
            logger.warning(f"Failed to set prefix cache: {e}")

        return {"hit": False, "key": key, "meta": meta}

    def invalidate_all(self) -> None:
        try:
            self.backend.clear()
            logger.info("Prefix cache cleared explicitly")
            emit_cache_event("invalidate_all", {})
        except Exception as e:
            logger.warning(f"Failed to clear prefix cache: {e}")

    # ------------------------ Segmented API ------------------------
    def register_segment(
        self,
        segment_name: str,
        content: str,
        provider: str,
        tenant_id: str,
        *,
        tools: Optional[List[Any]] = None,
        ttl_s: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Register a specific prompt segment independently.

        This namespaces the content under a stable segment name so it can be cached and
        invalidated independently of other segments. Tools can be provided to tie the
        fingerprint to tool definitions when relevant (e.g., the tools manifest segment).
        """
        namespaced = f"[SEGMENT:{segment_name}]\n{content or ''}"
        info = self.register_prefix(
            system_text=namespaced,
            tools=tools or [],
            provider=(provider or "default").lower(),
            tenant_id=tenant_id or "public",
            ttl_s=ttl_s,
        )
        try:
            # Post-annotate meta with segment for telemetry/readback when backend returns it
            meta = info.get("meta")
            if isinstance(meta, dict):
                meta["segment"] = segment_name
        except Exception:
            pass
        return info


# ------------------------ Helper functions for integrations ------------------------

def extract_system_prefix_from_prompt(prompt: Any) -> str:
    """Attempt to extract the system message text from a LangChain-style prompt/messages.

    Returns a normalized string or empty string if none found.
    """
    try:
        # List of messages
        if isinstance(prompt, list):
            for message in prompt:
                role = getattr(message, "type", None) or getattr(message, "role", None)
                if role == "system" or message.__class__.__name__ == "SystemMessage":
                    content = getattr(message, "content", "")
                    return PrefixCacheManager._normalize_whitespace(content or "")
        # Single message
        content = getattr(prompt, "content", None)
        if content and prompt.__class__.__name__ == "SystemMessage":
            return PrefixCacheManager._normalize_whitespace(content)
    except Exception:
        pass
    return "" 