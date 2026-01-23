"""Scaleway Cloud API routes."""

from flask import Blueprint, request
from utils.web.cors_utils import create_cors_response

scaleway_bp = Blueprint('scaleway', __name__)


@scaleway_bp.before_request
def handle_options_request():
    """Handle CORS preflight OPTIONS requests for all Scaleway routes."""
    if request.method == 'OPTIONS':
        return create_cors_response()


from . import scaleway_routes  # noqa: E402, F401
