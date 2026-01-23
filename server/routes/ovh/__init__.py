"""OVH Cloud API routes - Infrastructure + OAuth2 Quick Connect."""

from flask import Blueprint, request
from utils.web.cors_utils import create_cors_response

ovh_bp = Blueprint('ovh', __name__)


@ovh_bp.before_request
def handle_options_request():
    """Handle CORS preflight OPTIONS requests for all OVH routes."""
    if request.method == 'OPTIONS':
        return create_cors_response()


# Import OVH API routes (projects, validation)
from . import ovh_api_routes  # noqa: E402,F401

# Import OAuth2 Quick Connect flow
from . import oauth2_auth_code_flow  # noqa: E402,F401

