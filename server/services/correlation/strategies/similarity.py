"""
Text-similarity correlation strategy.

Scores an alert against an incident using cosine similarity on
dense vector embeddings from the t2v-transformers service, with
Jaccard fallback when embeddings are unavailable.
"""

import logging
import math
import re
from typing import List, Optional, Set

from services.correlation.embedding_client import get_embedding_client
from services.correlation.strategies.base import CorrelationStrategy

logger = logging.getLogger(__name__)

_STOPWORDS: Set[str] = {"the", "a", "an", "is", "are", "was", "for", "in", "on", "at"}
_TOKEN_RE = re.compile(r"[a-z0-9]+")


class SimilarityStrategy(CorrelationStrategy):
    """Vector-based similarity scoring for titles, with service-name overlap."""

    TITLE_WEIGHT = 0.7
    SERVICE_WEIGHT = 0.3

    def score(
        self,
        alert_title: str,
        alert_service: str,
        incident_title: str,
        incident_services: List[str],
    ) -> float:
        """Score based on semantic similarity between alert and incident.

        Uses cosine similarity on embeddings from t2v-transformers, falling
        back to Jaccard token similarity if embeddings unavailable.

        Args:
            alert_title: Title / summary of the alert.
            alert_service: Service name from the alert.
            incident_title: Title / summary of the incident.
            incident_services: Services already associated with the incident.

        Returns:
            float: Weighted combination of title and service similarity in [0.0, 1.0].
        """
        if not alert_title or not incident_title:
            return 0.0

        title_sim = self._vector_similarity(alert_title, incident_title)
        if title_sim is None:
            title_sim = self._jaccard_similarity(alert_title, incident_title)

        service_sim = self._service_similarity(alert_service, incident_services)

        return self.TITLE_WEIGHT * title_sim + self.SERVICE_WEIGHT * service_sim

    def _vector_similarity(self, text_a: str, text_b: str) -> Optional[float]:
        """Compute cosine similarity using embeddings.

        Returns None if embeddings couldn't be retrieved.
        """
        try:
            client = get_embedding_client()
            vec_a = client.embed(text_a)
            vec_b = client.embed(text_b)

            if vec_a is None or vec_b is None:
                return None

            return self._cosine_similarity(vec_a, vec_b)
        except Exception as e:
            logger.debug("[SimilarityStrategy] Vector similarity failed: %s", e)
            return None

    @staticmethod
    def _cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
        """Compute cosine similarity between two vectors."""
        if len(vec_a) != len(vec_b) or len(vec_a) == 0:
            return 0.0

        dot_product = sum(a * b for a, b in zip(vec_a, vec_b))
        norm_a = math.sqrt(sum(a * a for a in vec_a))
        norm_b = math.sqrt(sum(b * b for b in vec_b))

        if norm_a == 0 or norm_b == 0:
            return 0.0

        similarity = dot_product / (norm_a * norm_b)
        return max(0.0, min(1.0, similarity))

    def _jaccard_similarity(self, text_a: str, text_b: str) -> float:
        """Fallback: Jaccard token similarity."""
        tokens_a = self._tokenize(text_a)
        tokens_b = self._tokenize(text_b)
        return self._jaccard(tokens_a, tokens_b)

    @staticmethod
    def _tokenize(text: str) -> Set[str]:
        """Lowercase, extract [a-z0-9]+ tokens, remove stopwords and short tokens."""
        tokens = set(_TOKEN_RE.findall(text.lower()))
        return {t for t in tokens if t not in _STOPWORDS and len(t) >= 2}

    @staticmethod
    def _jaccard(set_a: Set[str], set_b: Set[str]) -> float:
        """Jaccard index of two sets."""
        if not set_a and not set_b:
            return 0.0
        intersection = set_a & set_b
        union = set_a | set_b
        return len(intersection) / len(union) if union else 0.0

    @staticmethod
    def _service_similarity(alert_service: str, incident_services: List[str]) -> float:
        """Compute service-name similarity.

        Exact match gives 1.0; otherwise fall back to Jaccard on service name tokens.
        """
        if not alert_service or not incident_services:
            return 0.0

        if alert_service in incident_services:
            return 1.0

        alert_tokens = set(_TOKEN_RE.findall(alert_service.lower()))
        alert_tokens = {t for t in alert_tokens if len(t) >= 2}

        incident_tokens: Set[str] = set()
        for svc in incident_services:
            tokens = _TOKEN_RE.findall(svc.lower())
            incident_tokens.update(t for t in tokens if len(t) >= 2)

        if not alert_tokens or not incident_tokens:
            return 0.0

        intersection = alert_tokens & incident_tokens
        union = alert_tokens | incident_tokens
        return len(intersection) / len(union) if union else 0.0
