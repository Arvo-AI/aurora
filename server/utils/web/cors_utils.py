import logging
from flask import jsonify, request
import os
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

FRONTEND_URL = os.getenv("FRONTEND_URL", "")
if not FRONTEND_URL:
    logger.warning("FRONTEND_URL not set - CORS will reject all cross-origin requests")

# Build allowed origins set from FRONTEND_URL
_allowed_origins = set()
if FRONTEND_URL:
    _allowed_origins.add(FRONTEND_URL.rstrip("/"))
    # Also allow localhost variants for development
    parsed = urlparse(FRONTEND_URL)
    if parsed.hostname in ("localhost", "127.0.0.1"):
        for host in ("localhost", "127.0.0.1"):
            _allowed_origins.add(f"{parsed.scheme}://{host}:{parsed.port}" if parsed.port else f"{parsed.scheme}://{host}")


def create_cors_response(success=True):
    """Create a Flask response with proper CORS headers for preflight requests"""
    resp = jsonify(success=success)
    origin = request.headers.get('Origin', '')
    # Only allow the origin if it matches the configured FRONTEND_URL
    if origin.rstrip("/") in _allowed_origins:
        allowed_origin = origin
    else:
        allowed_origin = FRONTEND_URL
    resp.headers.add('Access-Control-Allow-Origin', allowed_origin)
    resp.headers.add('Access-Control-Allow-Headers', 'Content-Type, X-Provider, X-Requested-With, X-User-ID, Authorization')
    resp.headers.add('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
    resp.headers.add('Access-Control-Allow-Credentials', 'true')
    resp.headers.add('Content-Type', 'application/json')
    return resp