"""Shared Flask-Limiter utilities."""

import logging
import os
from typing import Optional

from flask import current_app, jsonify, request
from flask_limiter import Limiter

from config.rate_limiting import (
    DEFAULT_RATE_LIMITS,
    RATE_LIMIT_ERROR_RESPONSE,
    RATE_LIMIT_HEADERS_ENABLED,
    RATE_LIMIT_STORAGE_URL,
    RATE_LIMIT_STRATEGY,
    RATE_LIMITING_ENABLED,
)
from utils.auth.stateless_auth import get_user_id_from_request


def _get_remote_ip() -> str:
    """Return best-effort client IP, accounting for reverse proxies."""
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip
    return request.remote_addr or "unknown"


def get_rate_limit_key() -> str:
    """Use the user id when available, otherwise fall back to the caller IP."""
    user_id = get_user_id_from_request()
    if user_id and user_id != "anonymous":
        return f"user:{user_id}"
    return f"ip:{_get_remote_ip()}"


def is_rate_limit_exempt() -> bool:
    """Allow health checks, metrics, and trusted internal traffic to skip limits."""
    if _get_remote_ip() in {"127.0.0.1", "localhost", "::1"}:
        return True

    # Exempt Kubernetes health probe endpoints regardless of blueprint/function naming
    endpoint = request.endpoint or ""
    if endpoint in {"health", "healthz", "ready", "liveness", "readiness_check"}:
        return True

    # Handle fully-qualified blueprint endpoints like "health.readiness_check" or similar
    if any(keyword in endpoint for keyword in {"readiness", "liveness", "health", "ready"}):
        return True

    # Exempt incidents read endpoints (frequent polling for real-time updates)
    if request.path.startswith("/api/incidents") and request.method in ('GET', 'HEAD', 'OPTIONS'):
        return True
    
    # Exempt connected accounts endpoint (frequently polled by connector status hooks)
    if request.path.startswith("/api/connected-accounts") and request.method in ('GET', 'HEAD', 'OPTIONS'):
        return True

    if request.path.startswith("/metrics") or request.path.startswith("/status"):
        return True

    admin_token = (
        current_app.config.get("RATE_LIMIT_BYPASS_TOKEN")
        or os.getenv("RATE_LIMIT_BYPASS_TOKEN")
    )
    provided_token = request.headers.get("X-Rate-Limit-Bypass")
    return bool(admin_token and provided_token == admin_token)


def get_rate_limit_error_response(retry_after: Optional[int] = None) -> dict:
    """Return a JSON-friendly error payload used by the 429 handler."""
    response = RATE_LIMIT_ERROR_RESPONSE.copy()
    if retry_after:
        response["retry_after_seconds"] = retry_after
        response["message"] = f"Too many requests. Please try again in {retry_after} seconds."
    return response


def log_rate_limit_hit(user_id: Optional[str] = None, endpoint: Optional[str] = None) -> None:
    """Centralised logging helper so we stay consistent across services."""
    logger = logging.getLogger(__name__)
    logger.warning(
        "Rate limit hit: user=%s, ip=%s, endpoint=%s",
        user_id or "anonymous",
        _get_remote_ip(),
        endpoint or request.endpoint,
    )


limiter = Limiter(
    key_func=get_rate_limit_key,
    default_limits=DEFAULT_RATE_LIMITS,
    strategy=RATE_LIMIT_STRATEGY,
    storage_uri=RATE_LIMIT_STORAGE_URL,
    enabled=RATE_LIMITING_ENABLED,
    headers_enabled=RATE_LIMIT_HEADERS_ENABLED,
)


@limiter.request_filter
def _exempt_rate_limited_requests() -> bool:
    return is_rate_limit_exempt()


def register_rate_limit_handlers(app) -> None:
    """Attach a JSON 429 handler to the Flask app."""

    @app.errorhandler(429)
    def _ratelimit_handler(exc):  # type: ignore[unused-variable]
        retry_after = getattr(exc, "retry_after", None)
        log_rate_limit_hit(get_user_id_from_request(), request.endpoint)
        return jsonify(get_rate_limit_error_response(retry_after)), 429


__all__ = [
    "get_rate_limit_key",
    "get_rate_limit_error_response",
    "is_rate_limit_exempt",
    "limiter",
    "log_rate_limit_hit",
    "register_rate_limit_handlers",
]
