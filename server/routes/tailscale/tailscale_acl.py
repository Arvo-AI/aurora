"""
Tailscale ACL Policy Routes

Provides endpoints for:
1. Getting the current ACL policy
2. Updating the ACL policy
3. Previewing ACL changes
4. Validating ACL syntax
"""

import logging
from flask import request, jsonify, g
from routes.tailscale import tailscale_bp, require_tailscale
from utils.web.limiter_ext import limiter

logger = logging.getLogger(__name__)


@tailscale_bp.route('/tailscale/acl', methods=['GET', 'PUT'])
@limiter.limit("30 per minute")
@require_tailscale
def acl_policy():
    """
    Get or update the ACL policy.

    GET: Returns current ACL policy
    PUT: Updates ACL policy (requires full policy document)

    PUT Request body:
    {
        "acl": { ... ACL policy document ... },
        "ifMatch": "optional-etag-for-optimistic-locking"
    }
    """
    try:
        # Use query param or default tailnet
        target_tailnet = request.args.get('tailnet', g.tailnet)

        if request.method == 'PUT':
            data = request.get_json() or {}
            acl = data.get("acl")
            if_match = data.get("ifMatch")

            if not acl:
                return jsonify({"error": "ACL policy is required"}), 400

            success, updated_acl, error = g.tailscale_client.update_acl(target_tailnet, acl, if_match)

            if not success:
                return jsonify({"error": error}), 400

            logger.info(f"ACL policy updated by user {g.user_id}")

            return jsonify({
                "success": True,
                "message": "ACL policy updated",
                "acl": updated_acl
            })

        # GET - Fetch current ACL
        success, acl_data, error = g.tailscale_client.get_acl(target_tailnet)

        if not success:
            return jsonify({"error": error}), 400

        return jsonify({"acl": acl_data})

    except Exception as e:
        logger.error(f"Error with Tailscale ACL: {e}", exc_info=True)
        return jsonify({"error": "Failed to process ACL request"}), 500


@tailscale_bp.route('/tailscale/acl/preview', methods=['POST'])
@limiter.limit("15 per minute")
@require_tailscale
def preview_acl():
    """
    Preview ACL changes without applying.

    Request body:
    {
        "acl": { ... ACL policy document ... }
    }

    Returns:
    {
        "preview": { ... preview results ... }
    }
    """
    try:
        data = request.get_json() or {}
        acl = data.get("acl")
        target_tailnet = data.get("tailnet", g.tailnet)

        if not acl:
            return jsonify({"error": "ACL policy is required"}), 400

        success, preview_result, error = g.tailscale_client.preview_acl(target_tailnet, acl)

        if not success:
            return jsonify({"error": error}), 400

        return jsonify({"preview": preview_result})

    except Exception as e:
        logger.error(f"Error previewing Tailscale ACL: {e}", exc_info=True)
        return jsonify({"error": "Failed to preview ACL"}), 500


@tailscale_bp.route('/tailscale/acl/validate', methods=['POST'])
@limiter.limit("30 per minute")
@require_tailscale
def validate_acl():
    """
    Validate ACL syntax without applying.

    Request body:
    {
        "acl": { ... ACL policy document ... }
    }

    Returns:
    {
        "valid": true/false,
        "errors": [ ... validation errors if any ... ]
    }
    """
    try:
        data = request.get_json() or {}
        acl = data.get("acl")
        target_tailnet = data.get("tailnet", g.tailnet)

        if not acl:
            return jsonify({"error": "ACL policy is required"}), 400

        success, validation_result, error = g.tailscale_client.validate_acl(target_tailnet, acl)

        if not success:
            return jsonify({
                "valid": False,
                "error": error
            }), 400

        return jsonify({
            "valid": True,
            "result": validation_result
        })

    except Exception as e:
        logger.error(f"Error validating Tailscale ACL: {e}", exc_info=True)
        return jsonify({"error": "Failed to validate ACL"}), 500
