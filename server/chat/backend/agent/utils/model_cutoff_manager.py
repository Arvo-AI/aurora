"""
Model Knowledge Cutoff Manager

Provides automatic web search triggering based on model training data cutoffs.
Maps model names to their knowledge cutoff dates and determines if searches are needed.
"""

import re
import logging
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ModelInfo:
    """Information about a language model"""

    name: str
    provider: str
    knowledge_cutoff: datetime
    supports_vision: bool = False
    supports_reasoning: bool = False


class ModelCutoffManager:
    """
    Manages model knowledge cutoffs and determines when web searches are needed.

    This class maintains a comprehensive mapping of AI models to their training
    data cutoff dates and provides logic to automatically trigger web searches
    when queries involve information beyond a model's knowledge.
    """

    def __init__(self):
        self.models = self._initialize_model_mappings()
        logger.info(f"Initialized ModelCutoffManager with {len(self.models)} models")

    def _initialize_model_mappings(self) -> Dict[str, ModelInfo]:
        """Initialize the comprehensive model mapping with knowledge cutoffs"""

        # Helper function to create datetime objects
        def cutoff_date(year: int, month: int, day: int = 1) -> datetime:
            return datetime(year, month, day, tzinfo=timezone.utc)

        models = {}

        # OpenAI Models (via OpenRouter)
        openai_models = {
            "openai/gpt-5.2": ModelInfo(
                "gpt-5.2", "openai", cutoff_date(2025, 8, 1), True, True
            ),
        }

        # Anthropic Models (via OpenRouter)
        anthropic_models = {
            "anthropic/claude-sonnet-4-5": ModelInfo(
                "claude-sonnet-4-5", "anthropic", cutoff_date(2025, 9, 1), True, True
            ),
            "anthropic/claude-opus-4-5": ModelInfo(
                "claude-opus-4-5", "anthropic", cutoff_date(2025, 11, 1), True, True
            ),
        }

        # Google Models (via OpenRouter)
        google_models = {
            "google/gemini-3-pro-preview": ModelInfo(
                "gemini-3-pro-preview", "google", cutoff_date(2025, 4, 1), True, True
            ),
        }

        # Combine all models
        models.update(openai_models)
        models.update(anthropic_models)
        models.update(google_models)

        # Add fallback patterns for partial matches
        self._add_fallback_patterns(models)

        return models

    def _add_fallback_patterns(self, models: Dict[str, ModelInfo]) -> None:
        """Add fallback patterns for model name matching"""
        # This allows partial matching for models not explicitly listed
        # These are conservative estimates
        fallback_patterns = {
            "gpt": datetime(2025, 8, 1, tzinfo=timezone.utc),
            "claude": datetime(2025, 11, 1, tzinfo=timezone.utc),
            "gemini": datetime(2025, 11, 1, tzinfo=timezone.utc),
        }

        self.fallback_patterns = fallback_patterns

    def get_model_info(self, model_name: str) -> Optional[ModelInfo]:
        """
        Get model information for a given model name.

        Args:
            model_name: The model name (e.g., "openai/gpt-4o", "gpt-4", etc.)

        Returns:
            ModelInfo object if found, None otherwise
        """
        if not model_name:
            return None

        # Direct lookup first
        if model_name in self.models:
            return self.models[model_name]

        # Try case-insensitive lookup
        model_name_lower = model_name.lower()
        for key, model_info in self.models.items():
            if key.lower() == model_name_lower:
                return model_info

        # Try partial matching for fallback patterns
        for pattern, cutoff in self.fallback_patterns.items():
            if pattern in model_name_lower:
                logger.warning(
                    f"Using fallback cutoff for model {model_name} based on pattern {pattern}"
                )
                return ModelInfo(
                    name=model_name,
                    provider="unknown",
                    knowledge_cutoff=cutoff,
                    supports_vision=False,
                    supports_reasoning=False,
                )

        # If no match found, log warning and return conservative default
        logger.warning(f"Unknown model {model_name}, using conservative cutoff")
        return ModelInfo(
            name=model_name,
            provider="unknown",
            knowledge_cutoff=datetime(
                2023, 1, 1, tzinfo=timezone.utc
            ),  # Conservative default
            supports_vision=False,
            supports_reasoning=False,
        )

    def get_knowledge_cutoff(self, model_name: str) -> datetime:
        """
        Get the knowledge cutoff date for a specific model.

        Args:
            model_name: The model name

        Returns:
            datetime object representing the knowledge cutoff
        """
        model_info = self.get_model_info(model_name)
        return (
            model_info.knowledge_cutoff
            if model_info
            else datetime(2023, 1, 1, tzinfo=timezone.utc)
        )

    def needs_web_search(
        self,
        model_name: str,
        query: str,
        query_date: Optional[datetime] = None,
        time_sensitive_keywords: Optional[list] = None,
    ) -> Tuple[bool, str]:
        """
        Determine if a web search is needed based on the model's knowledge cutoff.

        Args:
            model_name: The model being used
            query: The user's query
            query_date: Specific date the query is about (if determinable)
            time_sensitive_keywords: Additional keywords that suggest current info needed

        Returns:
            Tuple of (needs_search: bool, reason: str)
        """
        model_info = self.get_model_info(model_name)
        if not model_info:
            return True, f"Unknown model {model_name}, searching to be safe"

        cutoff = model_info.knowledge_cutoff
        current_time = datetime.now(timezone.utc)

        # If query_date is provided and it's after the cutoff, search is needed
        if query_date and query_date > cutoff:
            return (
                True,
                f"Query date {query_date.strftime('%Y-%m-%d')} is after model cutoff {cutoff.strftime('%Y-%m-%d')}",
            )

        # Check for time-sensitive keywords in the query
        default_time_sensitive = [
            "latest",
            "recent",
            "current",
            "now",
            "today",
            "this year",
            "2024",
            "2025",
            "newest",
            "updated",
            "breaking",
            "just announced",
            "new release",
            "current version",
            "latest version",
            "as of",
            "recently released",
            "today's",
            "this month",
            "this week",
            "yesterday",
        ]

        keywords_to_check = (time_sensitive_keywords or []) + default_time_sensitive
        query_lower = query.lower()

        for keyword in keywords_to_check:
            if keyword in query_lower:
                return True, f"Query contains time-sensitive keyword: '{keyword}'"

        # Check for year references that are after the cutoff
        year_matches = re.findall(r"\b(20\d{2})\b", query)
        for year_str in year_matches:
            year = int(year_str)
            if year > cutoff.year or (year == cutoff.year and cutoff.month < 12):
                return (
                    True,
                    f"Query references year {year} which may be after model cutoff",
                )

        # Check if the query is about very recent events (within 3 months of cutoff)
        days_since_cutoff = (current_time - cutoff).days
        if days_since_cutoff > 90:  # 3 months
            # Check for indicators that the query might be about recent events
            recent_indicators = [
                "what happened",
                "news about",
                "updates on",
                "changes to",
                "announcement",
                "release",
                "launch",
                "outage",
                "incident",
            ]

            for indicator in recent_indicators:
                if indicator in query_lower:
                    return (
                        True,
                        f"Query may be about recent events and model cutoff is {days_since_cutoff} days old",
                    )

        return (
            False,
            f"Query appears to be within model knowledge (cutoff: {cutoff.strftime('%Y-%m-%d')})",
        )

    def should_auto_search(
        self, model_name: str, query: str, confidence_threshold: float = 0.7
    ) -> Tuple[bool, str, float]:
        """
        Advanced heuristic to determine if automatic web search should be triggered.

        Args:
            model_name: The model being used
            query: The user's query
            confidence_threshold: Confidence threshold for triggering search (0.0-1.0)

        Returns:
            Tuple of (should_search: bool, reason: str, confidence: float)
        """
        needs_search, reason = self.needs_web_search(model_name, query)

        if not needs_search:
            return False, reason, 0.0

        # Calculate confidence based on multiple factors
        confidence = 0.0
        factors = []

        model_info = self.get_model_info(model_name)
        cutoff = (
            model_info.knowledge_cutoff
            if model_info
            else datetime(2023, 1, 1, tzinfo=timezone.utc)
        )
        days_since_cutoff = (datetime.now(timezone.utc) - cutoff).days

        # Factor 1: Age of model cutoff
        if days_since_cutoff > 365:  # Over 1 year old
            confidence += 0.4
            factors.append("model_very_old")
        elif days_since_cutoff > 180:  # Over 6 months old
            confidence += 0.3
            factors.append("model_old")
        elif days_since_cutoff > 90:  # Over 3 months old
            confidence += 0.2
            factors.append("model_somewhat_old")

        # Factor 2: Time-sensitive keywords
        urgent_keywords = ["breaking", "latest", "current", "today", "now", "recent"]
        query_lower = query.lower()

        urgent_count = sum(1 for keyword in urgent_keywords if keyword in query_lower)
        if urgent_count > 0:
            confidence += min(0.3, urgent_count * 0.15)
            factors.append(f"urgent_keywords_{urgent_count}")

        # Factor 3: Technical/API queries (often change frequently)
        tech_keywords = [
            "api",
            "documentation",
            "error",
            "deprecated",
            "version",
            "changelog",
        ]
        tech_count = sum(1 for keyword in tech_keywords if keyword in query_lower)
        if tech_count > 0:
            confidence += min(0.2, tech_count * 0.1)
            factors.append(f"tech_keywords_{tech_count}")

        # Factor 4: Cloud provider specific (these change frequently - be aggressive!)
        cloud_keywords = [
            "aws",
            "azure",
            "gcp",
            "google cloud",
            "kubernetes",
            "terraform",
            "docker",
            "k8s",
            "ec2",
            "s3",
            "lambda",
            "cloudformation",
            "helm",
            "eks",
            "gke",
            "aks",
            "cloud run",
            "compute engine",
            "app service",
        ]
        cloud_count = sum(1 for keyword in cloud_keywords if keyword in query_lower)
        if cloud_count > 0:
            # Much higher confidence for cloud queries - they change rapidly
            confidence += min(0.5, cloud_count * 0.25)
            factors.append(f"cloud_keywords_{cloud_count}")

        # Factor 5: Political/current events queries (highly time-sensitive)
        political_keywords = [
            "president",
            "election",
            "government",
            "congress",
            "senate",
            "prime minister",
            "parliament",
            "politics",
            "vote",
            "campaign",
        ]
        political_count = sum(
            1 for keyword in political_keywords if keyword in query_lower
        )
        if political_count > 0:
            confidence += min(0.3, political_count * 0.2)
            factors.append(f"political_keywords_{political_count}")

        # Factor 6: Current events and news queries
        news_keywords = [
            "news",
            "event",
            "happening",
            "announcement",
            "report",
            "update",
        ]
        news_count = sum(1 for keyword in news_keywords if keyword in query_lower)
        if news_count > 0:
            confidence += min(0.2, news_count * 0.15)
            factors.append(f"news_keywords_{news_count}")

        # Special case: For cloud queries with older models, be very aggressive
        if cloud_count > 0 and days_since_cutoff > 180:  # Model older than 6 months
            confidence = max(
                confidence, 0.8
            )  # Ensure high confidence for cloud queries
            if "cloud_aggressive" not in factors:
                factors.append("cloud_aggressive")

        # Cap confidence at 1.0
        confidence = min(1.0, confidence)

        should_search = confidence >= confidence_threshold
        detailed_reason = f"{reason} (confidence: {confidence:.2f}, factors: {factors})"

        return should_search, detailed_reason, confidence

    def get_model_capabilities(self, model_name: str) -> Dict[str, bool]:
        """
        Get capabilities of a specific model.

        Args:
            model_name: The model name

        Returns:
            Dictionary of capabilities
        """
        model_info = self.get_model_info(model_name)
        if not model_info:
            return {"supports_vision": False, "supports_reasoning": False}

        return {
            "supports_vision": model_info.supports_vision,
            "supports_reasoning": model_info.supports_reasoning,
        }

    def list_models(self, provider: Optional[str] = None) -> Dict[str, ModelInfo]:
        """
        List all available models, optionally filtered by provider.

        Args:
            provider: Optional provider filter

        Returns:
            Dictionary of model name to ModelInfo
        """
        if provider:
            return {k: v for k, v in self.models.items() if v.provider == provider}
        return self.models.copy()


# Create a global instance for easy access
model_cutoff_manager = ModelCutoffManager()


def cutoff_date(year: int, month: int, day: int = 1) -> datetime:
    """Helper function to create UTC datetime objects for cutoff dates"""
    return datetime(year, month, day, tzinfo=timezone.utc)
