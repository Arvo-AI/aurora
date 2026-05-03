"""WIF connection routes for GCP Workload Identity Federation."""

import logging

from flask import Blueprint, jsonify, request
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import store_user_preference
from utils.auth.token_management import store_tokens_in_db
from connectors.gcp_connector.auth.wif import (
    GCP_AUTH_TYPE_WIF,
    get_aurora_sa_email,
    verify_wif_access,
)

logger = logging.getLogger(__name__)

gcp_wif_bp = Blueprint("gcp_wif_bp", __name__)


@gcp_wif_bp.route("/api/gcp/wif/connect", methods=["POST"])
@require_permission("connectors", "write")
def connect_wif(user_id):
    """Connect a GCP project via Workload Identity Federation.

    The customer runs the Aurora Terraform module / gcloud script first, then
    provides the WIF config values here. Aurora verifies access synchronously
    and stores the connection -- no Celery tasks, no polling.
    """
    payload = request.get_json(force=True, silent=True) or {}

    required = ("project_id", "project_number", "pool_id", "provider_id", "sa_email")
    missing = [f for f in required if not payload.get(f)]
    if missing:
        return jsonify({"error": f"Missing required fields: {', '.join(missing)}"}), 400

    wif_config = {
        "project_id": payload["project_id"].strip(),
        "project_number": payload["project_number"].strip(),
        "pool_id": payload["pool_id"].strip(),
        "provider_id": payload["provider_id"].strip(),
        "sa_email": payload["sa_email"].strip(),
    }
    if payload.get("viewer_sa_email"):
        wif_config["viewer_sa_email"] = payload["viewer_sa_email"].strip()
    if payload.get("org_id"):
        wif_config["org_id"] = payload["org_id"].strip()
    if payload.get("additional_project_ids"):
        wif_config["additional_project_ids"] = [
            p.strip() for p in payload["additional_project_ids"] if isinstance(p, str) and p.strip()
        ]

    result = verify_wif_access(wif_config)
    if not result.get("ok"):
        return jsonify({
            "error": f"WIF verification failed: {result.get('error', 'unknown')}",
        }), 400

    verified_projects = result.get("projects", [])

    token_payload = {
        "auth_type": GCP_AUTH_TYPE_WIF,
        "wif_config": wif_config,
        "email": wif_config["sa_email"],
        "default_project_id": wif_config["project_id"],
        "accessible_projects": verified_projects,
    }

    try:
        store_tokens_in_db(user_id, token_payload, "gcp")
    except Exception as e:
        logger.error("WIF connect: failed to store credentials (error_type=%s)", type(e).__name__)
        return jsonify({"error": "Failed to store WIF credentials"}), 500

    try:
        store_user_preference(user_id, "gcp_root_project", wif_config["project_id"])
    except Exception:
        logger.warning("WIF connect: could not persist root project preference")

    logger.info(
        "WIF connect: stored credentials for user %s (projects=%d)",
        user_id,
        len(verified_projects),
    )

    return jsonify({
        "success": True,
        "email": wif_config["sa_email"],
        "default_project_id": wif_config["project_id"],
        "accessible_projects": verified_projects,
    })


@gcp_wif_bp.route("/api/gcp/wif/setup-info", methods=["GET"])
@require_permission("connectors", "read")
def wif_setup_info(user_id):
    """Return Aurora's SA email so the UI can embed it in setup instructions."""
    sa_email = get_aurora_sa_email()
    if not sa_email:
        return jsonify({"error": "WIF not configured on this Aurora instance"}), 404
    return jsonify({"aurora_sa_email": sa_email})


@gcp_wif_bp.route("/api/gcp/wif/verify", methods=["POST"])
@require_permission("connectors", "read")
def verify_wif_endpoint(user_id):
    """Health-check: attempt a WIF token exchange and return ok/error."""
    from utils.auth.token_management import get_token_data
    from connectors.gcp_connector.auth.service_accounts import get_gcp_auth_type

    token_data = get_token_data(user_id, "gcp")
    if not token_data or get_gcp_auth_type(token_data) != GCP_AUTH_TYPE_WIF:
        return jsonify({"ok": False, "error": "No WIF connection found"}), 404

    result = verify_wif_access(token_data.get("wif_config", {}))
    status = 200 if result.get("ok") else 502
    return jsonify(result), status
