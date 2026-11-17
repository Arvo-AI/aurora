"""Authenticated quick CI-log triage endpoint."""

import logging

from flask import Blueprint, jsonify, request
from services.quick_triage import analyze_log

logger = logging.getLogger(__name__)

quick_triage_bp = Blueprint(
    "quick_triage",
    __name__,
    url_prefix="/api/quick-triage"
)


@quick_triage_bp.route("", methods=["POST"])
def quick_triage():
    if request.content_length and request.content_length > 25_000:
        return jsonify({"error": "Request too large"}), 413

    body = request.get_json(silent=True)

    if not isinstance(body, dict):
        return jsonify({"error": "Invalid JSON body"}), 400

    try:
        result = analyze_log(body.get("log"))
        return jsonify(result), 200

    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    except Exception:
        logger.exception("Quick triage analysis failed")
        return jsonify({"error": "Analysis unavailable"}), 503
