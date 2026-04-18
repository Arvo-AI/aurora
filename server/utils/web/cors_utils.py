from flask import jsonify, request
import logging
import os

logger = logging.getLogger(__name__)

FRONTEND_URL = os.getenv("FRONTEND_URL")

_ALLOWED_ORIGINS = set()
if FRONTEND_URL:
    _ALLOWED_ORIGINS.add(FRONTEND_URL.rstrip("/"))
if os.getenv("AURORA_ENV", "dev").lower() == "dev":
    _ALLOWED_ORIGINS.add("http://localhost:3000")

def create_cors_response(success=True):
    """Create a Flask response with proper CORS headers for preflight requests"""
    resp = jsonify(success=success)
    origin = request.headers.get('Origin', '')
    normalised = origin.rstrip("/").lower()
    allowed_lower = {o.lower() for o in _ALLOWED_ORIGINS}
    if normalised in allowed_lower:
        resp.headers.add('Access-Control-Allow-Origin', origin)
    elif FRONTEND_URL:
        logger.info("CORS: rejected origin %s, falling back to FRONTEND_URL", origin or "(empty)")
        resp.headers.add('Access-Control-Allow-Origin', FRONTEND_URL)
    resp.headers.add('Vary', 'Origin')
    resp.headers.add('Access-Control-Allow-Headers', 'Content-Type, X-Provider, X-Requested-With, X-User-ID, X-Org-ID, Authorization, X-Internal-Secret')
    resp.headers.add('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
    resp.headers.add('Access-Control-Allow-Credentials', 'true')
    resp.headers.add('Content-Type', 'application/json')
    return resp
