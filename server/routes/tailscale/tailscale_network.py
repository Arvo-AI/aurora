"""
Tailscale Network Configuration Routes

Provides endpoints for:
1. DNS configuration (nameservers, search paths, MagicDNS)
2. Route management (subnet routes)
3. Auth key management
"""

import logging
from flask import request, jsonify, g
from routes.tailscale import tailscale_bp, require_tailscale
from utils.web.limiter_ext import limiter

logger = logging.getLogger(__name__)


# ============================================================================
# DNS Configuration
# ============================================================================

@tailscale_bp.route('/tailscale/dns', methods=['GET'])
@limiter.limit("30 per minute")
@require_tailscale
def get_dns_config():
    """
    Get full DNS configuration (nameservers, preferences, search paths).

    Returns:
    {
        "dns": {
            "nameservers": ["8.8.8.8"],
            "preferences": { "magicDNS": true },
            "searchPaths": ["example.com"]
        }
    }
    """
    try:
        target_tailnet = request.args.get('tailnet', g.tailnet)

        # Fetch all DNS configs
        dns_config = {}
        errors = []

        success, nameservers, error = g.tailscale_client.get_dns_nameservers(target_tailnet)
        if success:
            dns_config["nameservers"] = nameservers
        elif error:
            errors.append(f"nameservers: {error}")

        success, preferences, error = g.tailscale_client.get_dns_preferences(target_tailnet)
        if success:
            dns_config["preferences"] = preferences
        elif error:
            errors.append(f"preferences: {error}")

        success, searchpaths, error = g.tailscale_client.get_dns_searchpaths(target_tailnet)
        if success:
            dns_config["searchPaths"] = searchpaths
        elif error:
            errors.append(f"searchPaths: {error}")

        response = {"dns": dns_config}
        if errors:
            response["partialErrors"] = errors
            logger.warning(f"Partial DNS fetch errors for user {g.user_id}: {errors}")

        return jsonify(response)

    except Exception as e:
        logger.error(f"Error getting Tailscale DNS config: {e}", exc_info=True)
        return jsonify({"error": "Failed to get DNS configuration"}), 500


@tailscale_bp.route('/tailscale/dns/nameservers', methods=['GET', 'POST'])
@limiter.limit("30 per minute")
@require_tailscale
def dns_nameservers():
    """
    Get or set DNS nameservers.

    POST Request body:
    {
        "nameservers": ["8.8.8.8", "8.8.4.4"]
    }
    """
    try:
        target_tailnet = request.args.get('tailnet', g.tailnet)

        if request.method == 'POST':
            data = request.get_json() or {}
            nameservers = data.get("nameservers", [])

            if not isinstance(nameservers, list):
                return jsonify({"error": "nameservers must be an array"}), 400

            success, error = g.tailscale_client.set_dns_nameservers(target_tailnet, nameservers)

            if not success:
                return jsonify({"error": error}), 400

            logger.info(f"DNS nameservers updated by user {g.user_id}")

            return jsonify({
                "success": True,
                "message": "Nameservers updated"
            })

        # GET
        success, data, error = g.tailscale_client.get_dns_nameservers(target_tailnet)

        if not success:
            return jsonify({"error": error}), 400

        return jsonify({"nameservers": data})

    except Exception as e:
        logger.error(f"Error with Tailscale DNS nameservers: {e}", exc_info=True)
        return jsonify({"error": "Failed to process nameservers request"}), 500


@tailscale_bp.route('/tailscale/dns/preferences', methods=['GET', 'POST'])
@limiter.limit("30 per minute")
@require_tailscale
def dns_preferences():
    """
    Get or set DNS preferences (MagicDNS).

    POST Request body:
    {
        "magicDNS": true
    }
    """
    try:
        target_tailnet = request.args.get('tailnet', g.tailnet)

        if request.method == 'POST':
            data = request.get_json() or {}
            magic_dns = data.get("magicDNS", True)

            success, error = g.tailscale_client.set_dns_preferences(target_tailnet, magic_dns)

            if not success:
                return jsonify({"error": error}), 400

            logger.info(f"DNS preferences updated by user {g.user_id}")

            return jsonify({
                "success": True,
                "message": "DNS preferences updated"
            })

        # GET
        success, data, error = g.tailscale_client.get_dns_preferences(target_tailnet)

        if not success:
            return jsonify({"error": error}), 400

        return jsonify({"preferences": data})

    except Exception as e:
        logger.error(f"Error with Tailscale DNS preferences: {e}", exc_info=True)
        return jsonify({"error": "Failed to process DNS preferences request"}), 500


@tailscale_bp.route('/tailscale/dns/searchpaths', methods=['GET', 'POST'])
@limiter.limit("30 per minute")
@require_tailscale
def dns_searchpaths():
    """
    Get or set DNS search paths.

    POST Request body:
    {
        "searchPaths": ["example.com", "internal.local"]
    }
    """
    try:
        target_tailnet = request.args.get('tailnet', g.tailnet)

        if request.method == 'POST':
            data = request.get_json() or {}
            searchpaths = data.get("searchPaths", [])

            if not isinstance(searchpaths, list):
                return jsonify({"error": "searchPaths must be an array"}), 400

            success, error = g.tailscale_client.set_dns_searchpaths(target_tailnet, searchpaths)

            if not success:
                return jsonify({"error": error}), 400

            logger.info(f"DNS search paths updated by user {g.user_id}")

            return jsonify({
                "success": True,
                "message": "Search paths updated"
            })

        # GET
        success, data, error = g.tailscale_client.get_dns_searchpaths(target_tailnet)

        if not success:
            return jsonify({"error": error}), 400

        return jsonify({"searchPaths": data})

    except Exception as e:
        logger.error(f"Error with Tailscale DNS search paths: {e}", exc_info=True)
        return jsonify({"error": "Failed to process search paths request"}), 500


# ============================================================================
# Routes (Subnet Routes)
# ============================================================================

@tailscale_bp.route('/tailscale/routes', methods=['GET'])
@limiter.limit("30 per minute")
@require_tailscale
def get_routes():
    """
    Get all subnet routes in the tailnet.

    Returns:
    {
        "routes": [
            {
                "route": "10.0.0.0/24",
                "deviceId": "...",
                "deviceName": "...",
                "advertised": true,
                "enabled": true
            }
        ]
    }
    """
    try:
        target_tailnet = request.args.get('tailnet', g.tailnet)

        success, routes, error = g.tailscale_client.get_routes(target_tailnet)

        if not success:
            return jsonify({"error": error}), 400

        return jsonify({"routes": routes})

    except Exception as e:
        logger.error(f"Error getting Tailscale routes: {e}", exc_info=True)
        return jsonify({"error": "Failed to get routes"}), 500


# ============================================================================
# Auth Keys
# ============================================================================

@tailscale_bp.route('/tailscale/auth-keys', methods=['GET', 'POST'])
@limiter.limit("30 per minute")
@require_tailscale
def auth_keys():
    """
    List or create auth keys.

    POST Request body:
    {
        "reusable": false,
        "ephemeral": false,
        "preauthorized": true,
        "tags": ["tag:server"],
        "expirySeconds": 86400,
        "description": "Server auth key"
    }

    Returns (POST):
    {
        "key": "tskey-auth-xxx",  // Only shown once!
        "id": "...",
        "created": "...",
        "expires": "..."
    }
    """
    try:
        target_tailnet = request.args.get('tailnet', g.tailnet)

        if request.method == 'POST':
            data = request.get_json() or {}

            success, key_data, error = g.tailscale_client.create_auth_key(
                tailnet=target_tailnet,
                reusable=data.get("reusable", False),
                ephemeral=data.get("ephemeral", False),
                preauthorized=data.get("preauthorized", True),
                tags=data.get("tags"),
                expiry_seconds=data.get("expirySeconds"),
                description=data.get("description")
            )

            if not success:
                return jsonify({"error": error}), 400

            logger.info(f"Auth key created by user {g.user_id}")

            return jsonify({
                "success": True,
                "key": key_data
            })

        # GET - List keys
        success, keys, error = g.tailscale_client.list_auth_keys(target_tailnet)

        if not success:
            return jsonify({"error": error}), 400

        return jsonify({"keys": keys})

    except Exception as e:
        logger.error(f"Error with Tailscale auth keys: {e}", exc_info=True)
        return jsonify({"error": "Failed to process auth keys request"}), 500


@tailscale_bp.route('/tailscale/auth-keys/<key_id>', methods=['GET', 'DELETE'])
@limiter.limit("30 per minute")
@require_tailscale
def auth_key_detail(key_id: str):
    """
    Get or delete a specific auth key.

    GET: Returns key details (not the actual key value)
    DELETE: Revokes the key
    """
    try:
        target_tailnet = request.args.get('tailnet', g.tailnet)

        if request.method == 'DELETE':
            success, error = g.tailscale_client.delete_auth_key(target_tailnet, key_id)

            if not success:
                return jsonify({"error": error}), 400

            logger.info(f"Auth key {key_id} deleted by user {g.user_id}")

            return jsonify({
                "success": True,
                "message": "Auth key revoked"
            })

        # GET
        success, key_data, error = g.tailscale_client.get_auth_key(target_tailnet, key_id)

        if not success:
            return jsonify({"error": error}), 400

        return jsonify({"key": key_data})

    except Exception as e:
        logger.error(f"Error with Tailscale auth key {key_id}: {e}", exc_info=True)
        return jsonify({"error": "Failed to process auth key request"}), 500


# ============================================================================
# Tailnet Settings
# ============================================================================

@tailscale_bp.route('/tailscale/settings', methods=['GET'])
@limiter.limit("30 per minute")
@require_tailscale
def tailnet_settings():
    """
    Get tailnet-wide settings.

    Returns:
    {
        "settings": { ... tailnet settings ... }
    }
    """
    try:
        target_tailnet = request.args.get('tailnet', g.tailnet)

        success, settings, error = g.tailscale_client.get_tailnet_settings(target_tailnet)

        if not success:
            return jsonify({"error": error}), 400

        return jsonify({"settings": settings})

    except Exception as e:
        logger.error(f"Error getting Tailscale settings: {e}", exc_info=True)
        return jsonify({"error": "Failed to get tailnet settings"}), 500
