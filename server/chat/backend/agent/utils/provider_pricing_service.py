"""
Provider-specific Dynamic Pricing Service.

Fetches actual pricing from provider billing APIs when available.
Currently supports:
  - Google (Gemini API): via Google Cloud Billing Catalog API (public, needs OAuth)
  - Anthropic / OpenAI: uses static fallback (no public pricing API)

Pricing is cached for 24 hours since provider rates rarely change more frequently.
"""

import logging
import requests
from typing import Dict, Optional
from datetime import datetime, timedelta
from threading import Lock

logger = logging.getLogger(__name__)

# Google Cloud Billing Catalog API service IDs
GEMINI_API_SERVICE_ID = "AEFD-7695-64FA"

# Maps SKU description keywords to our internal model identifiers.
# Each entry: (model_key, token_direction)
# We match on 'short' context since that's the standard tier (<=200K tokens).
_GEMINI_SKU_PATTERNS: list[tuple[str, str, list[str], list[str] | None]] = [
    ("gemini-2.5-flash-lite", "input", ["2.5 flash lite", "input", "short", "text"], None),
    ("gemini-2.5-flash-lite", "output", ["2.5 flash lite", "output", "short", "text"], None),
    ("gemini-2.5-flash", "input", ["2.5 flash", "input token count", "short", "text"], ["lite"]),
    ("gemini-2.5-flash", "output", ["2.5 flash", "output token count", "short", "text"], ["lite"]),
    ("gemini-2.5-pro", "input", ["2.5 pro", "input", "short", "text"], ["experimental", "preview"]),
    ("gemini-2.5-pro", "output", ["2.5 pro", "output", "short", "text"], ["experimental", "preview"]),
    ("gemini-3-flash", "input", ["3 flash", "input", "text"], ["lite", "3.1"]),
    ("gemini-3-flash", "output", ["3 flash", "output", "text"], ["lite", "3.1"]),
    ("gemini-3-pro", "input", ["3 pro", "input", "short", "text"], ["3.1"]),
    ("gemini-3-pro", "output", ["3 pro", "output", "short", "text"], ["3.1"]),
    ("gemini-3.1-pro-preview", "input", ["3.1 pro", "input", "text"], None),
    ("gemini-3.1-pro-preview", "output", ["3.1 pro", "output", "text"], None),
    ("gemini-3.1-flash-lite-preview", "input", ["3.1 flash lite", "input", "text"], None),
    ("gemini-3.1-flash-lite-preview", "output", ["3.1 flash lite", "output", "text"], None),
]


def _sku_matches(description: str, keywords: list[str], exclude: list[str] | None = None) -> bool:
    """Check if a SKU description matches all required keywords and no excluded ones."""
    desc = description.lower()
    if "batch" in desc or "cache" in desc or "bidi" in desc or "live" in desc:
        return False
    if "video" in desc or "audio" in desc or "image" in desc:
        return False
    if "thinking" in desc or "priority" in desc or "flex" in desc or "tts" in desc:
        return False
    if exclude:
        if any(ex in desc for ex in exclude):
            return False
    return all(kw in desc for kw in keywords)


def _extract_price_per_1k(sku: dict) -> float:
    """Extract price per 1K tokens from a billing SKU.

    The Billing API reports price per single token as (units + nanos/1e9).
    We convert to per-1K-tokens to match our internal MODEL_PRICING format.
    """
    try:
        rate = sku["pricingInfo"][0]["pricingExpression"]["tieredRates"][0]["unitPrice"]
        units = int(rate.get("units", "0"))
        nanos = rate.get("nanos", 0)
        price_per_token = units + nanos / 1_000_000_000
        return price_per_token * 1000
    except (KeyError, IndexError, ValueError):
        return 0.0


class ProviderPricingService:
    """Fetches and caches pricing from provider billing APIs.

    Cache strategy: fetch once on first access, then refresh every 24h.
    On fetch failure, returns stale cache (if available) or None.
    """

    def __init__(self, cache_ttl_hours: int = 24):
        self._cache: Dict[str, Dict[str, float]] = {}
        self._last_fetch: Optional[datetime] = None
        self._cache_ttl = timedelta(hours=cache_ttl_hours)
        self._lock = Lock()
        self._fetch_in_progress = False

    def _needs_refresh(self) -> bool:
        if not self._last_fetch:
            return True
        return datetime.now() - self._last_fetch > self._cache_ttl

    def _get_gcp_access_token(self) -> Optional[str]:
        """Get a GCP access token from available credentials."""
        try:
            import google.auth
            import google.auth.transport.requests

            credentials, _ = google.auth.default()
            credentials.refresh(google.auth.transport.requests.Request())
            return credentials.token
        except Exception as e:
            logger.debug("GCP ADC credentials unavailable: %s", e)

        try:
            import subprocess
            result = subprocess.run(
                ["gcloud", "auth", "application-default", "print-access-token"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception as e:
            logger.debug("gcloud CLI token retrieval failed: %s", e)

        return None

    def _fetch_gemini_pricing(self) -> Dict[str, Dict[str, float]]:
        """Fetch Gemini model pricing from Google Cloud Billing Catalog API."""
        token = self._get_gcp_access_token()
        if not token:
            logger.warning(
                "No GCP credentials available for billing API - "
                "using static pricing for Google models"
            )
            return {}

        headers = {"Authorization": f"Bearer {token}"}
        all_skus = []
        page_token = None

        try:
            while True:
                params = {"pageSize": 500}
                if page_token:
                    params["pageToken"] = page_token
                resp = requests.get(
                    f"https://cloudbilling.googleapis.com/v1/services/{GEMINI_API_SERVICE_ID}/skus",
                    params=params,
                    headers=headers,
                    timeout=15,
                )
                resp.raise_for_status()
                data = resp.json()
                all_skus.extend(data.get("skus", []))
                page_token = data.get("nextPageToken")
                if not page_token:
                    break

            logger.info(f"Fetched {len(all_skus)} Gemini API SKUs from billing catalog")

        except Exception as e:
            logger.warning(f"Failed to fetch Gemini billing SKUs: {e}")
            return {}

        pricing: Dict[str, Dict[str, float]] = {}

        for sku in all_skus:
            desc = sku.get("description", "")
            if "generate" not in desc.lower():
                continue

            for model_key, direction, keywords, exclude in _GEMINI_SKU_PATTERNS:
                if _sku_matches(desc, keywords, exclude):
                    price_per_1k = _extract_price_per_1k(sku)
                    if price_per_1k <= 0:
                        continue

                    for prefix in ("google", "vertex"):
                        full_key = f"{prefix}/{model_key}"
                        if full_key not in pricing:
                            pricing[full_key] = {}
                        if direction not in pricing[full_key]:
                            pricing[full_key][direction] = price_per_1k
                            logger.debug(
                                f"  {full_key} {direction}: ${price_per_1k:.4f}/1K "
                                f"(from: {desc})"
                            )
                    break

        # Only return models where we have both input and output
        complete = {}
        for model, rates in pricing.items():
            if "input" in rates and "output" in rates:
                complete[model] = rates

        logger.info(
            f"Resolved pricing for {len(complete)} Google/Vertex models from billing API"
        )
        return complete

    def get_pricing(self, force_refresh: bool = False) -> Dict[str, Dict[str, float]]:
        """Get cached provider pricing, refreshing if stale.

        Returns a dict of model_name -> {"input": float, "output": float}
        where prices are per 1K tokens (matching MODEL_PRICING format).
        """
        with self._lock:
            if not force_refresh and not self._needs_refresh():
                return self._cache.copy()

            if self._fetch_in_progress:
                return self._cache.copy()

            self._fetch_in_progress = True

        try:
            new_pricing = self._fetch_gemini_pricing()

            with self._lock:
                if new_pricing:
                    self._cache = new_pricing
                    self._last_fetch = datetime.now()
                    logger.info(
                        f"Provider pricing cache updated: {len(new_pricing)} models"
                    )
                elif not self._cache:
                    self._last_fetch = datetime.now()
                    logger.warning("No provider pricing fetched and no cached data")
        finally:
            with self._lock:
                self._fetch_in_progress = False

        return self._cache.copy()

    def get_model_pricing(self, model_name: str) -> Optional[Dict[str, float]]:
        """Get pricing for a specific model, or None if unavailable."""
        pricing = self.get_pricing()
        return pricing.get(model_name)

    def get_cache_info(self) -> dict:
        return {
            "last_fetch": self._last_fetch.isoformat() if self._last_fetch else None,
            "cache_age_hours": (
                (datetime.now() - self._last_fetch).total_seconds() / 3600
                if self._last_fetch
                else None
            ),
            "cache_ttl_hours": self._cache_ttl.total_seconds() / 3600,
            "models_cached": len(self._cache),
            "needs_refresh": self._needs_refresh(),
        }


_instance: Optional[ProviderPricingService] = None
_instance_lock = Lock()


def get_provider_pricing_service() -> ProviderPricingService:
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = ProviderPricingService()
        return _instance
