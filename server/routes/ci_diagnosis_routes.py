"""Authenticated, tool-free first value from a pasted CI log."""

import logging

from flask import Blueprint, jsonify, request

from services.ci_diagnosis import (
    InvalidDiagnosis, InvalidLog, ProviderUnavailable, diagnose, prepare_log,
)
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request
from utils.hooks import get_hook
from utils.web.limiter_ext import limiter

logger = logging.getLogger(__name__)
ci_diagnosis_bp = Blueprint("ci_diagnosis", __name__)


@ci_diagnosis_bp.after_request
def prevent_caching(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@ci_diagnosis_bp.route("/api/ci-diagnosis", methods=["POST"])
@require_permission("chat", "write")
@limiter.limit("5 per minute;30 per hour")
def analyze_ci_log(user_id):
    # Bound the JSON envelope before parsing (including escaped Unicode).
    request.max_content_length = 150_000
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="Send a JSON object containing a log."), 400
    if data.get("consent") is not True:
        return jsonify(error="Confirm that this log may be sent to your configured AI provider."), 400
    try:
        lines = prepare_log(data.get("log"))
    except InvalidLog as exc:
        return jsonify(error=str(exc)), 400

    org_id = get_org_id_from_request()
    for name in ("verify_entitlement", "before_llm_call"):
        args = (org_id,) if name == "verify_entitlement" else (org_id, user_id)
        allowed, _ = get_hook(name)(*args)
        if not allowed:
            return jsonify(error="AI analysis is not available for your organization. Contact your administrator."), 403
    logger.info("ci_diagnosis_started lines=%d", len(lines))
    try:
        return jsonify(diagnose(lines, user_id, org_id))
    except ProviderUnavailable:
        logger.info("ci_diagnosis_failed reason=provider_unavailable")
        return jsonify(error="The AI provider is unavailable. Ask your administrator to check Aurora's model configuration, then retry."), 503
    except InvalidDiagnosis:
        logger.info("ci_diagnosis_failed reason=invalid_evidence")
        return jsonify(error="The analysis could not be supported with valid log references. Try a clearer excerpt around the failure."), 502
    except Exception:
        logger.warning("CI diagnosis failed")
        return jsonify(error="Analysis could not be completed. Please retry."), 500
