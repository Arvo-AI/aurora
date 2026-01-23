"""
Tailscale Device Management Routes

Provides endpoints for:
1. Listing devices in a tailnet
2. Getting device details
3. Authorizing/removing devices
4. Managing device tags and routes
"""

import logging
from flask import request, jsonify, g
from routes.tailscale import tailscale_bp, require_tailscale
from utils.web.limiter_ext import limiter

logger = logging.getLogger(__name__)


@tailscale_bp.route('/tailscale/devices', methods=['GET'])
@limiter.limit("30 per minute")
@require_tailscale
def list_devices():
    """
    List all devices in the user's tailnet.

    Query params:
    - tailnet: Optional tailnet name (uses stored default if not provided)

    Returns:
    {
        "devices": [
            {
                "id": "device-id",
                "hostname": "my-laptop",
                "name": "my-laptop.tailnet.ts.net",
                "addresses": ["100.x.x.x"],
                "authorized": true,
                "tags": ["tag:server"],
                "lastSeen": "2024-01-01T00:00:00Z",
                "os": "linux",
                ...
            }
        ]
    }
    """
    try:
        # Use query param or default tailnet
        tailnet = request.args.get('tailnet', g.tailnet)

        success, devices, error = g.tailscale_client.list_devices(tailnet)

        if not success:
            return jsonify({"error": error}), 400

        return jsonify({"devices": devices})

    except Exception as e:
        logger.error(f"Error listing Tailscale devices: {e}", exc_info=True)
        return jsonify({"error": "Failed to list devices"}), 500


@tailscale_bp.route('/tailscale/devices/<device_id>', methods=['GET', 'DELETE'])
@limiter.limit("30 per minute")
@require_tailscale
def device_detail(device_id: str):
    """
    Get or delete a specific device.

    GET: Returns device details
    DELETE: Removes device from tailnet
    """
    try:
        if request.method == 'DELETE':
            success, error = g.tailscale_client.delete_device(device_id)
            if not success:
                return jsonify({"error": error}), 400
            return jsonify({"success": True, "message": "Device removed"})

        # GET - Fetch device details
        success, device, error = g.tailscale_client.get_device(device_id)

        if not success:
            return jsonify({"error": error}), 400

        return jsonify({"device": device})

    except Exception as e:
        logger.error(f"Error with Tailscale device {device_id}: {e}", exc_info=True)
        return jsonify({"error": "Failed to process device request"}), 500


@tailscale_bp.route('/tailscale/devices/<device_id>/authorize', methods=['POST'])
@limiter.limit("15 per minute")
@require_tailscale
def authorize_device(device_id: str):
    """Authorize a device to join the tailnet."""
    try:
        success, error = g.tailscale_client.authorize_device(device_id)

        if not success:
            return jsonify({"error": error}), 400

        logger.info(f"Device {device_id} authorized by user {g.user_id}")

        return jsonify({
            "success": True,
            "message": "Device authorized successfully"
        })

    except Exception as e:
        logger.error(f"Error authorizing Tailscale device {device_id}: {e}", exc_info=True)
        return jsonify({"error": "Failed to authorize device"}), 500


@tailscale_bp.route('/tailscale/devices/<device_id>/tags', methods=['POST'])
@limiter.limit("15 per minute")
@require_tailscale
def set_device_tags(device_id: str):
    """
    Set tags on a device.

    Request body:
    {
        "tags": ["tag:server", "tag:production"]
    }
    """
    try:
        data = request.get_json() or {}
        tags = data.get("tags", [])

        if not isinstance(tags, list):
            return jsonify({"error": "tags must be an array"}), 400

        success, error = g.tailscale_client.set_device_tags(device_id, tags)

        if not success:
            return jsonify({"error": error}), 400

        logger.info(f"Tags set on device {device_id} by user {g.user_id}")

        return jsonify({
            "success": True,
            "message": "Device tags updated"
        })

    except Exception as e:
        logger.error(f"Error setting Tailscale device tags: {e}", exc_info=True)
        return jsonify({"error": "Failed to set device tags"}), 500


@tailscale_bp.route('/tailscale/devices/<device_id>/routes', methods=['POST'])
@limiter.limit("15 per minute")
@require_tailscale
def set_device_routes(device_id: str):
    """
    Enable routes for a device (subnet router).

    Request body:
    {
        "routes": ["10.0.0.0/24", "192.168.1.0/24"]
    }
    """
    try:
        data = request.get_json() or {}
        routes = data.get("routes", [])

        if not isinstance(routes, list):
            return jsonify({"error": "routes must be an array"}), 400

        success, error = g.tailscale_client.set_device_routes(device_id, routes)

        if not success:
            return jsonify({"error": error}), 400

        logger.info(f"Routes set on device {device_id} by user {g.user_id}")

        return jsonify({
            "success": True,
            "message": "Device routes updated"
        })

    except Exception as e:
        logger.error(f"Error setting Tailscale device routes: {e}", exc_info=True)
        return jsonify({"error": "Failed to set device routes"}), 500


@tailscale_bp.route('/tailscale/devices/<device_id>/key-expiry', methods=['POST'])
@limiter.limit("15 per minute")
@require_tailscale
def set_device_key_expiry(device_id: str):
    """
    Enable or disable key expiry for a device.

    Request body:
    {
        "keyExpiryDisabled": true
    }
    """
    try:
        data = request.get_json() or {}
        key_expiry_disabled = data.get("keyExpiryDisabled", False)

        success, error = g.tailscale_client.set_device_key_expiry(device_id, key_expiry_disabled)

        if not success:
            return jsonify({"error": error}), 400

        status = "disabled" if key_expiry_disabled else "enabled"
        logger.info(f"Key expiry {status} for device {device_id} by user {g.user_id}")

        return jsonify({
            "success": True,
            "message": f"Key expiry {status}"
        })

    except Exception as e:
        logger.error(f"Error setting Tailscale device key expiry: {e}", exc_info=True)
        return jsonify({"error": "Failed to set key expiry"}), 500
