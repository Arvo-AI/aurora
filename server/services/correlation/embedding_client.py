"""
Embedding client for the t2v-transformers container.

Calls the Weaviate text2vec-transformers inference service to get
dense vector embeddings for text.
"""

import logging
import os
from functools import lru_cache
from typing import List, Optional

import requests

logger = logging.getLogger(__name__)

T2V_URL = os.getenv("T2V_TRANSFORMERS_URL", "http://t2v-transformers:8080")
T2V_TIMEOUT = float(os.getenv("T2V_TIMEOUT", "5.0"))


class EmbeddingClient:
    """Client for the t2v-transformers embedding service."""

    def __init__(self, base_url: str = T2V_URL, timeout: float = T2V_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session: Optional[requests.Session] = None

    @property
    def session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
        return self._session

    def embed(self, text: str) -> Optional[List[float]]:
        """Get embedding vector for a single text string.

        Args:
            text: The text to embed.

        Returns:
            List of floats representing the embedding, or None on failure.
        """
        if not text or not text.strip():
            return None

        try:
            response = self.session.post(
                f"{self.base_url}/vectors",
                json={"text": text},
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
            return data.get("vector")
        except requests.exceptions.Timeout:
            logger.warning(
                "[EmbeddingClient] Request timed out for text: %s...", text[:50]
            )
            return None
        except requests.exceptions.RequestException as e:
            logger.warning("[EmbeddingClient] Request failed: %s", e)
            return None
        except Exception as e:
            logger.warning("[EmbeddingClient] Unexpected error: %s", e)
            return None

    def embed_batch(self, texts: List[str]) -> List[Optional[List[float]]]:
        """Get embeddings for multiple texts.

        Note: The t2v-transformers container doesn't support batch requests,
        so this calls embed() for each text sequentially.
        """
        return [self.embed(text) for text in texts]


@lru_cache(maxsize=1)
def get_embedding_client() -> EmbeddingClient:
    """Get or create the singleton embedding client."""
    return EmbeddingClient()
