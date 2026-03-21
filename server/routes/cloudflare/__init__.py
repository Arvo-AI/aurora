"""Cloudflare API routes."""

from flask import Blueprint, request
from utils.web.cors_utils import create_cors_response

cloudflare_bp = Blueprint('cloudflare', __name__)


@cloudflare_bp.before_request
def handle_options_request():
    """Handle CORS preflight OPTIONS requests for all Cloudflare routes."""
    if request.method == 'OPTIONS':
        return create_cors_response()


from . import cloudflare_routes  # noqa: E402, F401
