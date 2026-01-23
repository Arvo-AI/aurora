from flask import jsonify, request
import os

FRONTEND_URL = os.getenv("FRONTEND_URL")

def create_cors_response(success=True):
    """Create a Flask response with proper CORS headers for preflight requests"""
    resp = jsonify(success=success)
    # Dynamically reflect the origin for development/local usage while keeping a default
    origin = request.headers.get('Origin', FRONTEND_URL)
    resp.headers.add('Access-Control-Allow-Origin', origin)
    # If you need to restrict in production, consider checking against a whitelist here.
    resp.headers.add('Access-Control-Allow-Headers', 'Content-Type, X-Provider, X-Requested-With, X-User-ID, Authorization')
    resp.headers.add('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
    resp.headers.add('Access-Control-Allow-Credentials', 'true')
    resp.headers.add('Content-Type', 'application/json')
    return resp 