"""
Provider-agnostic embedding client.

Parses the EMBEDDING_MODEL env var (format: "provider/model-name") and calls
the appropriate embedding API. Falls back to None when unconfigured, allowing
the caller to use a non-vector similarity method.

Supported providers:
  - openai/   → uses OPENAI_API_KEY
  - google/   → uses GOOGLE_AI_API_KEY
  - bedrock/  → same region / credential resolution as the Bedrock chat provider
                (BEDROCK_REGION → AWS_REGION → AWS_DEFAULT_REGION; IAM role / IRSA
                through boto3's default chain, static BEDROCK_* / AWS_* keys as the
                fallback). Base in-region model id, e.g. amazon.titan-embed-text-v2:0
                (Titan Text Embeddings V2 has no geo inference profile).

Embeddings are memoized per process (SHA-256 of provider/model/text, bounded LRU),
so a correlation run embeds the incoming alert once instead of once per candidate
incident, and incident titles are embedded once across runs.
"""

import hashlib
import logging
import os
import threading
from collections import OrderedDict
from functools import lru_cache
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# Distinct texts remembered per process. An alert storm correlates against at most
# 20 open incidents per alert, so this covers hours of titles in a few MB.
EMBED_CACHE_MAXSIZE = 2048

# botocore client settings for the embedding call: three attempts with exponential
# backoff on throttling / 5xx, short timeouts (one embedding is a sub-second call and
# the correlator runs inline in the alert ingestion path).
_BEDROCK_MAX_ATTEMPTS = 3
_BEDROCK_CONNECT_TIMEOUT = 5
_BEDROCK_READ_TIMEOUT = 20
# Historical fallback when no region is configured anywhere (boto3 would otherwise
# raise NoRegionError). Logged when used.
_BEDROCK_REGION_FALLBACK = "us-east-1"


def _parse_embedding_model() -> Tuple[Optional[str], Optional[str]]:
    """Parse EMBEDDING_MODEL env var into (provider, model) tuple.

    Returns (None, None) if unset or invalid.
    """
    raw = os.getenv("EMBEDDING_MODEL", "").strip()
    if not raw or "/" not in raw:
        return None, None

    provider, _, model = raw.partition("/")
    provider = provider.lower().strip()
    model = model.strip()

    if not provider or not model:
        return None, None

    return provider, model


class EmbeddingClient:
    """Provider-agnostic embedding client."""

    def __init__(self):
        self.provider, self.model = _parse_embedding_model()
        self._initialized = False
        self._client = None
        # sha256(provider/model:text) -> vector. Thread-safe bounded LRU; failures
        # (None) are never cached so a transient error is retried next time.
        self._cache: "OrderedDict[str, List[float]]" = OrderedDict()
        self._cache_lock = threading.Lock()
        self.cache_hits = 0
        self.cache_misses = 0

    @property
    def is_configured(self) -> bool:
        return self.provider is not None and self.model is not None

    def _cache_key(self, text: str) -> str:
        return hashlib.sha256(f"{self.provider}/{self.model}:{text}".encode("utf-8")).hexdigest()

    def _cache_get(self, key: str) -> Optional[List[float]]:
        with self._cache_lock:
            vector = self._cache.get(key)
            if vector is None:
                self.cache_misses += 1
                return None
            self._cache.move_to_end(key)
            self.cache_hits += 1
            return list(vector)

    def _cache_put(self, key: str, vector: List[float]) -> None:
        with self._cache_lock:
            self._cache[key] = list(vector)
            self._cache.move_to_end(key)
            while len(self._cache) > EMBED_CACHE_MAXSIZE:
                self._cache.popitem(last=False)

    def clear_cache(self) -> None:
        with self._cache_lock:
            self._cache.clear()

    def embed(self, text: str) -> Optional[List[float]]:
        """Get embedding vector for a single text string.

        Returns None if unconfigured or on failure. Identical texts hit the
        process-local cache instead of the provider.
        """
        if not self.is_configured:
            return None

        if not text or not text.strip():
            return None

        key = self._cache_key(text)
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        try:
            if self.provider == "openai":
                vector = self._embed_openai(text)
            elif self.provider == "google":
                vector = self._embed_google(text)
            elif self.provider == "bedrock":
                vector = self._embed_bedrock(text)
            else:
                logger.warning(
                    "[EmbeddingClient] Unknown provider: %s", self.provider
                )
                return None
        except Exception as e:
            logger.warning("[EmbeddingClient] Embedding failed (%s): %s", self.provider, e)
            return None

        if vector is not None:
            self._cache_put(key, vector)
        return vector

    def _embed_openai(self, text: str) -> Optional[List[float]]:
        """Call OpenAI embeddings API."""
        from openai import OpenAI

        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            logger.warning("[EmbeddingClient] OPENAI_API_KEY not set")
            return None

        if self._client is None:
            self._client = OpenAI(api_key=api_key)

        response = self._client.embeddings.create(
            input=text,
            model=self.model,
        )
        return response.data[0].embedding

    def _embed_google(self, text: str) -> Optional[List[float]]:
        """Call Google AI embeddings API."""
        from google import genai

        api_key = os.getenv("GOOGLE_AI_API_KEY", "").strip()
        if not api_key:
            logger.warning("[EmbeddingClient] GOOGLE_AI_API_KEY not set")
            return None

        if self._client is None:
            self._client = genai.Client(api_key=api_key)

        response = self._client.models.embed_content(
            model=self.model,
            contents=text,
        )
        return response.embeddings[0].values

    def _bedrock_client(self):
        """Build the ``bedrock-runtime`` client the way the chat provider does.

        Region: ``BEDROCK_REGION`` → ``AWS_REGION`` → ``AWS_DEFAULT_REGION`` with the
        ``global`` profile geo mapped to a real signing region. Credentials: explicit
        ``BEDROCK_*`` / ``AWS_*`` keys only when both halves are set, a named
        ``BEDROCK_PROFILE`` when given, otherwise boto3's default chain (IAM role,
        ECS task role, EKS IRSA) — static keys are no longer required.
        """
        import boto3
        from botocore.config import Config

        from chat.backend.agent.bedrock_env import (
            bedrock_credential_kwargs,
            bedrock_profile_name,
            resolve_bedrock_region,
            resolve_client_region,
        )

        region = resolve_client_region(resolve_bedrock_region())
        if not region:
            logger.warning(
                "[EmbeddingClient] No Bedrock region configured "
                "(BEDROCK_REGION / AWS_REGION / AWS_DEFAULT_REGION); using %s",
                _BEDROCK_REGION_FALLBACK,
            )
            region = _BEDROCK_REGION_FALLBACK

        client_kwargs = {
            "region_name": region,
            "config": Config(
                retries={"mode": "standard", "total_max_attempts": _BEDROCK_MAX_ATTEMPTS},
                connect_timeout=_BEDROCK_CONNECT_TIMEOUT,
                read_timeout=_BEDROCK_READ_TIMEOUT,
            ),
        }
        client_kwargs.update(bedrock_credential_kwargs())

        profile = bedrock_profile_name()
        if profile:
            return boto3.Session(profile_name=profile).client("bedrock-runtime", **client_kwargs)
        return boto3.client("bedrock-runtime", **client_kwargs)

    def _embed_bedrock(self, text: str) -> Optional[List[float]]:
        """Call AWS Bedrock embeddings (Titan Text Embeddings) via boto3 InvokeModel."""
        import json

        if self._client is None:
            self._client = self._bedrock_client()

        # Titan Text Embeddings V2 is only served under its base in-region id
        # (no us./eu. inference profile), so the configured model id goes as-is.
        response = self._client.invoke_model(
            modelId=self.model,
            body=json.dumps({"inputText": text}),
            contentType="application/json",
            accept="application/json",
        )
        result = json.loads(response["body"].read())
        embedding = result.get("embedding") if isinstance(result, dict) else None
        if (
            not isinstance(embedding, list)
            or not embedding
            or not all(isinstance(v, (int, float)) for v in embedding)
        ):
            logger.warning(
                "[EmbeddingClient] Unexpected Bedrock embedding response shape for %s", self.model
            )
            return None
        return [float(v) for v in embedding]


@lru_cache(maxsize=1)
def get_embedding_client() -> EmbeddingClient:
    """Get or create the singleton embedding client."""
    client = EmbeddingClient()
    if client.is_configured:
        logger.info(
            "[EmbeddingClient] Configured with provider=%s, model=%s",
            client.provider,
            client.model,
        )
    else:
        logger.info("[EmbeddingClient] No EMBEDDING_MODEL set — using Jaccard fallback")
    return client
