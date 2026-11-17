"""Aurora Watch: turn the always-on cluster watcher on or off and see what it is watching."""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request

from routes.audit_routes import record_audit_event
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request

logger = logging.getLogger(__name__)

watch_bp = Blueprint("aurora_watch", __name__)


def _view(user_id: str, org_id: str) -> dict:
    from services.watch.detector import DEFAULTS
    from services.watch.store import get_settings, list_clusters, status
    from services.watch.tasks import POLL_SECONDS
    from utils.flags.feature_flags import is_aurora_watch_enabled

    settings = get_settings(org_id)
    by_cluster = {s["cluster_id"]: s for s in status(user_id)}
    clusters = []
    for c in list_clusters(user_id):
        s = by_cluster.get(c["cluster_id"]) or {}
        clusters.append({
            "cluster_id": c["cluster_id"],
            "name": c.get("name"),
            "source": c.get("source"),
            "polls": s.get("polls", 0),
            "learning": s.get("learning", True),
            "last_poll_at": s.get("last_poll_at"),
            "last_error": s.get("last_error"),
            "blind_spots": s.get("blind_spots", []),
            "open_findings": s.get("open_findings", []),
        })
    return {
        "available": is_aurora_watch_enabled(),
        "enabled": settings["enabled"],
        "namespaces": settings["namespaces"],
        "updated_at": settings["updated_at"],
        "acting_as_you": settings.get("owner_user_id") == user_id,
        "poll_seconds": POLL_SECONDS,
        "learning_polls": DEFAULTS["learning_polls"],
        "clusters": clusters,
    }


@watch_bp.route("/api/aurora-watch", methods=["GET"])
@require_permission("incidents", "read")
def get_aurora_watch(user_id):
    org_id = get_org_id_from_request()
    try:
        return jsonify(_view(user_id, org_id))
    except Exception:
        logger.exception("[AuroraWatch] status failed")
        return jsonify({"error": "Could not load Aurora Watch status"}), 500


@watch_bp.route("/api/aurora-watch", methods=["PUT"])
@require_permission("connectors", "write")
def put_aurora_watch(user_id):
    """Turn Aurora Watch on or off, or change what it watches.

    Whoever turns it on (from off) is the user it acts as; changing the
    namespaces while it is on keeps that user.
    """
    from services.watch.store import PREF_KEY, get_settings, normalize_namespaces, save_settings
    from utils.auth.stateless_auth import get_org_preference
    from utils.flags.feature_flags import is_aurora_watch_enabled

    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("enabled"), bool):
        return jsonify({"error": "Body must be JSON with a boolean 'enabled'"}), 400
    try:
        namespaces = normalize_namespaces(data.get("namespaces"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if data["enabled"] and not is_aurora_watch_enabled():
        return jsonify({"error": "Aurora Watch is turned off for this Aurora installation"}), 409

    org_id = get_org_id_from_request()
    try:
        current = get_settings(org_id)
        keeps_owner = current["enabled"] and current.get("owner_user_id")
        owner = current["owner_user_id"] if keeps_owner else user_id
        save_settings(org_id, enabled=data["enabled"], owner_user_id=owner, namespaces=namespaces)
        # store_org_preference logs and swallows DB errors: confirm the write landed.
        saved = get_org_preference(org_id, PREF_KEY, strict=True) or {}
        if bool(saved.get("enabled")) != data["enabled"] or saved.get("namespaces") != namespaces:
            raise RuntimeError("Aurora Watch settings were not saved")
    except Exception:
        logger.exception("[AuroraWatch] settings update failed")
        return jsonify({"error": "Could not update Aurora Watch"}), 500

    record_audit_event(org_id, user_id, "aurora_watch_enabled" if data["enabled"] else "aurora_watch_disabled",
                       "aurora_watch", detail={"namespaces": namespaces}, req=request)
    try:
        return jsonify(_view(user_id, org_id))
    except Exception:
        logger.exception("[AuroraWatch] settings saved; status view failed")
        return jsonify({"enabled": data["enabled"], "namespaces": namespaces})
