"""
Text-similarity correlation strategy.

Scores an alert against an incident using Jaccard token similarity
on titles, with service-name overlap.
"""

import logging
import re
from typing import List, Set

from services.correlation.strategies.base import CorrelationStrategy

logger = logging.getLogger(__name__)

_STOPWORDS: Set[str] = {"the", "a", "an", "is", "are", "was", "for", "in", "on", "at"}
_TOKEN_RE = re.compile(r"[a-z0-9]+")


class SimilarityStrategy(CorrelationStrategy):
    """Token-based similarity scoring for titles, with service-name overlap."""

    TITLE_WEIGHT = 0.7
    SERVICE_WEIGHT = 0.3

    def score(
        self,
        alert_title: str,
        alert_service: str,
        incident_title: str,
        incident_services: List[str],
    ) -> float:
        """Score based on text similarity between alert and incident.

        Uses Jaccard token similarity on titles and service names.

        Args:
            alert_title: Title / summary of the alert.
            alert_service: Service name from the alert.
            incident_title: Title / summary of the incident.
            incident_services: Services already associated with the incident.

        Returns:
            float: Weighted combination of title and service similarity in [0.0, 1.0].
        """
        if not alert_title or not incident_title:
            logger.warning(
                "[SimilarityStrategy] Empty title input - alert_title=%r, incident_title=%r",
                bool(alert_title),
                bool(incident_title),
            )
            return 0.0

        title_sim = self._jaccard_similarity(alert_title, incident_title)
        service_sim = self._service_similarity(alert_service, incident_services)

        return self.TITLE_WEIGHT * title_sim + self.SERVICE_WEIGHT * service_sim

    def _jaccard_similarity(self, text_a: str, text_b: str) -> float:
        """Jaccard token similarity between two texts."""
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
            logger.warning(
                "[SimilarityStrategy] Empty service input - alert_service=%r, incident_services=%r",
                bool(alert_service),
                bool(incident_services),
            )
            return 0.0

        # Exact match — no fuzzy comparison needed
        if alert_service in incident_services:
            return 1.0

        alert_tokens = set(_TOKEN_RE.findall(alert_service.lower()))
        alert_tokens = {t for t in alert_tokens if len(t) >= 2 and t not in _STOPWORDS}

        incident_tokens: Set[str] = set()
        for svc in incident_services:
            tokens = _TOKEN_RE.findall(svc.lower())
            incident_tokens.update(
                t for t in tokens if len(t) >= 2 and t not in _STOPWORDS
            )

        if not alert_tokens or not incident_tokens:
            return 0.0

        intersection = alert_tokens & incident_tokens
        union = alert_tokens | incident_tokens
        return len(intersection) / len(union) if union else 0.0
