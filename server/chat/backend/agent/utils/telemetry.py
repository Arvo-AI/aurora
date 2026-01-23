import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def emit_cache_event(event: str, data: Optional[Dict[str, Any]] = None) -> None:
    payload = {"type": "CACHE_TELEMETRY", "event": event, "data": data or {}}
    try:
        logger.info(payload)
    except Exception:
        # Avoid throwing from telemetry
        pass


def emit_vendor_cache_event(provider: str, event: str, data: Optional[Dict[str, Any]] = None) -> None:
    payload = {
        "type": "VENDOR_CACHE_TELEMETRY",
        "provider": provider,
        "event": event,
        "data": data or {},
    }
    try:
        logger.info(payload)
    except Exception:
        pass 