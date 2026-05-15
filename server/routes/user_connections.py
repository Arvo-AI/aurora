from flask import Blueprint, jsonify, request
import logging
from utils.auth.rbac_decorators import require_permission
from utils.db.connection_utils import list_active_connections, set_connection_status

user_connections_bp = Blueprint('user_connections_bp', __name__)

logger = logging.getLogger(__name__)

def get_user_connections_from_db(user_id: str):
    """Return active connections using user_connections table."""
    return list_active_connections(user_id)

@user_connections_bp.route('/api/user_connections', methods=['GET'])
@require_permission("connectors", "read")
def get_user_connections(user_id):
    try:
        connections = get_user_connections_from_db(user_id)
        logger.info(f"Found {len(connections)} active connections for user {user_id}")
        return jsonify(connections), 200

    except Exception as e:
        logger.error(f"Error in /api/user_connections: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500

@user_connections_bp.route('/api/user_connections', methods=['DELETE'])
@require_permission("connectors", "write")
def disconnect_connection(user_id):
    """Mark a connection as not_connected."""
    try:
        data = request.get_json(force=True, silent=True) or {}
        provider = data.get("provider")
        account_id = data.get("account_id")
        if not provider or not account_id:
            return jsonify({"error": "provider and account_id required"}), 400

        success = set_connection_status(user_id, provider, account_id, "not_connected")
        if not success:
            return jsonify({"error": "Failed to disconnect"}), 500

        return jsonify({"status": "disconnected"}), 200
    except Exception as e:
        logger.error(f"Error in DELETE /api/user_connections: {e}")
        return jsonify({"error": "An unexpected error occurred"}), 500
