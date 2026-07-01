"""
Provider-agnostic embedding client.

Parses the EMBEDDING_MODEL env var (format: "provider/model-name") and calls
the appropriate embedding API. Falls back to None when unconfigured, allowing
the caller to use a non-vector similarity method.

Supported providers:
  - openai/   → uses OPENAI_API_KEY
  - google/   → uses GOOGLE_AI_API_KEY
  - bedrock/  → uses BEDROCK_ACCESS_KEY_ID + BEDROCK_SECRET_ACCESS_KEY
"""

import logging
import os
from functools import lru_cache
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


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

    @property
    def is_configured(self) -> bool:
        return self.provider is not None and self.model is not None

    def embed(self, text: str) -> Optional[List[float]]:
        """Get embedding vector for a single text string.

        Returns None if unconfigured or on failure.
        """
        if not self.is_configured:
            return None

        if not text or not text.strip():
            return None

        try:
            if self.provider == "openai":
                return self._embed_openai(text)
            elif self.provider == "google":
                return self._embed_google(text)
            elif self.provider == "bedrock":
                return self._embed_bedrock(text)
            else:
                logger.warning(
                    "[EmbeddingClient] Unknown provider: %s", self.provider
                )
                return None
        except Exception as e:
            logger.warning("[EmbeddingClient] Embedding failed (%s): %s", self.provider, e)
            return None

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

    def _embed_bedrock(self, text: str) -> Optional[List[float]]:
        """Call AWS Bedrock embeddings via boto3."""
        import json
        import boto3

        access_key = os.getenv("BEDROCK_ACCESS_KEY_ID", "").strip()
        secret_key = os.getenv("BEDROCK_SECRET_ACCESS_KEY", "").strip()
        region = os.getenv("BEDROCK_REGION", os.getenv("AWS_DEFAULT_REGION", "us-east-1"))

        if not access_key or not secret_key:
            logger.warning("[EmbeddingClient] BEDROCK_ACCESS_KEY_ID/SECRET not set")
            return None

        if self._client is None:
            self._client = boto3.client(
                "bedrock-runtime",
                region_name=region,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
            )

        response = self._client.invoke_model(
            modelId=self.model,
            body=json.dumps({"inputText": text}),
            contentType="application/json",
            accept="application/json",
        )
        result = json.loads(response["body"].read())
        return result.get("embedding")


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
