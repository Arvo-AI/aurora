"""Tailscale API routes."""

from functools import wraps
from flask import Blueprint, request, jsonify, g
from utils.web.cors_utils import create_cors_response

tailscale_bp = Blueprint('tailscale', __name__)


@tailscale_bp.before_request
def handle_options_request():
    """Handle CORS preflight OPTIONS requests for all Tailscale routes."""
    if request.method == 'OPTIONS':
        return create_cors_response()


def require_tailscale(f):
    """
    Decorator that handles Tailscale client setup.

    Must be used AFTER @require_permission (which injects user_id as the
    first positional arg).  Sets g.user_id, g.tailscale_client, and
    g.tailnet for use in the route.  Returns 401 if Tailscale is not
    connected.

    Usage:
        @tailscale_bp.route('/tailscale/example', methods=['GET'])
        @require_permission("connectors", "read")
        @require_tailscale
        def example_route(user_id):
            client = g.tailscale_client
            tailnet = g.tailnet
            # ... route logic
    """
    @wraps(f)
    def decorated_function(user_id, *args, **kwargs):
        client, tailnet, error_response = get_tailscale_client(user_id)
        if error_response:
            return error_response

        g.user_id = user_id
        g.tailscale_client = client
        g.tailnet = tailnet

        return f(user_id, *args, **kwargs)
    return decorated_function


def get_tailscale_client(user_id: str):
    """
    Get an authenticated Tailscale client for the user.

    This is a shared helper used by multiple route modules.

    Returns:
        Tuple of (client, tailnet, error_response)
        If error_response is not None, return it immediately
    """
    from utils.auth.token_management import get_token_data
    from connectors.tailscale_connector.auth import get_valid_access_token
    from connectors.tailscale_connector.api_client import TailscaleClient

    token_data = get_token_data(user_id, "tailscale")
    if not token_data:
        return None, None, (jsonify({
            "error": "Tailscale not connected",
            "action": "CONNECT_REQUIRED"
        }), 401)

    client_id = token_data.get("client_id")
    client_secret = token_data.get("client_secret")
    tailnet = token_data.get("tailnet", "-")

    if not client_id or not client_secret:
        return None, None, (jsonify({"error": "Invalid stored credentials"}), 401)

    success, access_token, error = get_valid_access_token(
        client_id, client_secret, token_data.get("token_data")
    )

    if not success:
        return None, None, (jsonify({"error": error or "Failed to authenticate"}), 401)

    client = TailscaleClient(access_token)
    return client, tailnet, None


from . import tailscale_routes  # noqa: E402, F401
from . import tailscale_devices  # noqa: E402, F401
from . import tailscale_acl  # noqa: E402, F401
from . import tailscale_network  # noqa: E402, F401
