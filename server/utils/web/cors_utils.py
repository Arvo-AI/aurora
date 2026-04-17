from flask import jsonify, request
import os

FRONTEND_URL = os.getenv("FRONTEND_URL")

_ALLOWED_ORIGINS = set()
if FRONTEND_URL:
    _ALLOWED_ORIGINS.add(FRONTEND_URL.rstrip("/"))

def create_cors_response(success=True):
    """Create a Flask response with proper CORS headers for preflight requests"""
    resp = jsonify(success=success)
    origin = request.headers.get('Origin', '')
    if origin.rstrip("/") in _ALLOWED_ORIGINS:
        resp.headers.add('Access-Control-Allow-Origin', origin)
    elif FRONTEND_URL:
        resp.headers.add('Access-Control-Allow-Origin', FRONTEND_URL)
    resp.headers.add('Access-Control-Allow-Headers', 'Content-Type, X-Provider, X-Requested-With, X-User-ID, X-Org-ID, Authorization')
    resp.headers.add('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
    resp.headers.add('Access-Control-Allow-Credentials', 'true')
    resp.headers.add('Content-Type', 'application/json')
    return resp 