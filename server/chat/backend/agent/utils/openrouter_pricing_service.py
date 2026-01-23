"""
OpenRouter Dynamic Pricing Service

Fetches and caches model pricing from OpenRouter's API to ensure
accurate, up-to-date cost tracking without manual maintenance.
"""

import os
import json
import time
import logging
import requests
from typing import Dict, Optional, Any
from datetime import datetime, timedelta
from threading import Lock

logger = logging.getLogger(__name__)


class OpenRouterPricingService:
    """Service to fetch and cache model pricing from OpenRouter API"""

    def __init__(self, cache_duration_hours: int = 6):
        self.api_url = "https://openrouter.ai/api/v1/models"
        self.cache_duration = timedelta(hours=cache_duration_hours)
        self.pricing_cache: Dict[str, Dict[str, float]] = {}
        self.last_fetch_time: Optional[datetime] = None
        self.lock = Lock()

        # Get API key from environment variable
        self.api_key = os.getenv("OPENROUTER_API_KEY")
        if not self.api_key:
            logger.warning(
                "OPENROUTER_API_KEY environment variable not set - will use fallback pricing"
            )

        self.fallback_pricing = {
            "openai/gpt-5.2": {"input": 0.00175, "output": 0.014},
            "anthropic/claude-sonnet-4-5": {"input": 0.003, "output": 0.015},
            "anthropic/claude-opus-4-5": {"input": 0.005, "output": 0.025},
            "google/gemini-3-pro-preview": {"input": 0.00125, "output": 0.01},
            "default": {"input": 0.001, "output": 0.002},
        }

    def _needs_refresh(self) -> bool:
        """Check if pricing cache needs to be refreshed"""
        if not self.last_fetch_time:
            return True
        return datetime.now() - self.last_fetch_time > self.cache_duration

    def _fetch_pricing_from_api(self) -> Dict[str, Dict[str, float]]:
        """Fetch pricing data from OpenRouter API"""
        if not self.api_key:
            logger.critical("CRITICAL: No OpenRouter API key available - Aurora is using FALLBACK PRICING! This may result in incorrect billing!")
            return self.fallback_pricing.copy()

        try:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }

            logger.info("Fetching model pricing from OpenRouter API...")
            response = requests.get(self.api_url, headers=headers, timeout=10)
            response.raise_for_status()

            data = response.json()
            models = data.get("data", [])

            pricing_data = {}
            for model in models:
                model_id = model.get("id")
                pricing = model.get("pricing", {})

                if model_id and pricing:
                    # OpenRouter pricing is usually per token, convert to per 1K tokens
                    prompt_cost = pricing.get("prompt")
                    completion_cost = pricing.get("completion")

                    if prompt_cost is not None and completion_cost is not None:
                        # Convert from per-token to per-1K-tokens
                        pricing_data[model_id] = {
                            "input": float(prompt_cost) * 1000,
                            "output": float(completion_cost) * 1000,
                        }

            logger.info(
                f"Successfully fetched pricing for {len(pricing_data)} models from OpenRouter API"
            )

            # Merge with fallback pricing for any missing models
            final_pricing = self.fallback_pricing.copy()
            final_pricing.update(pricing_data)

            return final_pricing

        except requests.RequestException as e:
            logger.critical(f"CRITICAL: OpenRouter API failed - Aurora is using FALLBACK PRICING! Error: {e}")
            return self.fallback_pricing.copy()
        except Exception as e:
            logger.critical(f"CRITICAL: Unexpected OpenRouter pricing error - Aurora is using FALLBACK PRICING! Error: {e}")
            return self.fallback_pricing.copy()

    def get_pricing(self, force_refresh: bool = False) -> Dict[str, Dict[str, float]]:
        """
        Get current pricing data, refreshing from API if needed

        Args:
            force_refresh: Force refresh even if cache is still valid

        Returns:
            Dictionary mapping model IDs to pricing info
        """
        with self.lock:
            if force_refresh or self._needs_refresh():
                try:
                    self.pricing_cache = self._fetch_pricing_from_api()
                    self.last_fetch_time = datetime.now()
                except Exception as e:
                    logger.critical(f"CRITICAL: Error refreshing pricing cache - Aurora may be using FALLBACK PRICING! Error: {e}")
                    # If we have no cache, use fallback
                    if not self.pricing_cache:
                        logger.critical("CRITICAL: No pricing cache available - Aurora is using FALLBACK PRICING!")
                        self.pricing_cache = self.fallback_pricing.copy()

            return self.pricing_cache.copy()

    def get_model_pricing(self, model_name: str) -> Dict[str, float]:
        """
        Get pricing for a specific model with Aurora markup

        Args:
            model_name: The model identifier (e.g., "anthropic/claude-opus-4")

        Returns:
            Dictionary with 'input' and 'output' pricing per 1K tokens (includes 1.4x markup)
        """
        pricing_data = self.get_pricing()

        # Try exact match first
        if model_name in pricing_data:
            base_pricing = pricing_data[model_name]
        # Try without version suffix
        elif model_name.split(".")[0].split("-v")[0] in pricing_data:
            base_model = model_name.split(".")[0].split("-v")[0]
            base_pricing = pricing_data[base_model]
        else:
            # Return default pricing
            logger.warning(f"No pricing found for model {model_name}, using default")
            base_pricing = pricing_data.get(
                "default", {"input": 0.001, "output": 0.002}
            )

        # Apply Aurora's 1.4x markup for profit
        return {
            "input": base_pricing["input"] * 1.4,
            "output": base_pricing["output"] * 1.4,
        }

    def refresh_pricing(self) -> bool:
        """
        Manually refresh pricing cache

        Returns:
            True if refresh was successful, False otherwise
        """
        try:
            self.get_pricing(force_refresh=True)
            return True
        except Exception as e:
            logger.error(f"Failed to refresh pricing: {e}")
            return False

    def get_cache_info(self) -> Dict[str, Any]:
        """Get information about the current pricing cache"""
        return {
            "last_fetch_time": self.last_fetch_time.isoformat()
            if self.last_fetch_time
            else None,
            "cache_age_minutes": (datetime.now() - self.last_fetch_time).total_seconds()
            / 60
            if self.last_fetch_time
            else None,
            "cache_duration_hours": self.cache_duration.total_seconds() / 3600,
            "needs_refresh": self._needs_refresh(),
            "models_cached": len(self.pricing_cache),
            "has_api_key": bool(self.api_key),
        }


# Global instance for the service
_pricing_service = None
_service_lock = Lock()


def get_pricing_service() -> OpenRouterPricingService:
    """Get the global OpenRouter pricing service instance"""
    global _pricing_service

    with _service_lock:
        if _pricing_service is None:
            _pricing_service = OpenRouterPricingService()
        return _pricing_service
