"""Fly.io API routes."""

from flask import Blueprint, request
from utils.web.cors_utils import create_cors_response

flyio_bp = Blueprint('flyio', __name__)


@flyio_bp.before_request
def handle_options_request():
    """Handle CORS preflight OPTIONS requests for all Fly.io routes."""
    if request.method == 'OPTIONS':
        return create_cors_response()
    return None


from . import flyio_routes  # noqa: E402, F401
