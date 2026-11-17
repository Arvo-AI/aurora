"""Routes for the zero-connection "try a review" preview.

POST /change-gating/try        start a preview (public PR URL or pasted diff)
GET  /change-gating/try/<id>   poll its state

Auth is the normal session + RBAC (``chat`` read/write, which every role has —
the preview only creates a read-only chat session). The signed-in user is the
Aurora account on the instance; no *connector* is involved.
"""

from __future__ import annotations

import logging
import uuid

from flask import Blueprint, jsonify, request

from services.change_gating.try_review import (
    TryReviewError,
    parse_public_pr_url,
    validate_pasted_diff,
)
from utils.auth.rbac_decorators import require_permission
from utils.validation import is_valid_uuid
from utils.web.limiter_ext import limiter

logger = logging.getLogger(__name__)

change_gating_try_bp = Blueprint("change_gating_try", __name__)

# Each run spends LLM tokens on the operator's key, so cap per client even though
# the webhook-driven reviews are deliberately unthrottled.
_START_LIMITS = "5 per minute;30 per hour"


@change_gating_try_bp.route("/change-gating/try", methods=["POST", "OPTIONS"])
@limiter.limit(_START_LIMITS)
@require_permission("chat", "write")
def start_try_review(user_id):
    body = request.get_json(silent=True) or {}
    pr_url = body.get("pr_url")
    diff = body.get("diff")

    if bool(pr_url) == bool(diff):
        return jsonify({"error": {"code": "invalid_input", "message": "Send either pr_url or diff."}}), 400

    try:
        if pr_url:
            if not isinstance(pr_url, str):
                raise TryReviewError("invalid_url", "pr_url must be a string.")
            parse_public_pr_url(pr_url)  # fail fast; the task re-parses
            mode, payload = "url", pr_url
        else:
            if not isinstance(diff, str):
                raise TryReviewError("not_a_diff", "diff must be a string.")
            mode, payload = "diff", validate_pasted_diff(diff)
    except TryReviewError as exc:
        return jsonify({"error": {"code": exc.code, "message": exc.message}}), 400

    from tasks.change_gating_try import run_try_review, write_job

    job_id = str(uuid.uuid4())
    write_job(user_id, job_id, {"status": "queued"})
    run_try_review.delay(user_id, job_id, mode, payload)
    logger.info("change_gating_try=queued job_id=%s mode=%s", job_id, mode)
    return jsonify({"job_id": job_id, "status": "queued"}), 202


@change_gating_try_bp.route("/change-gating/try/<job_id>", methods=["GET", "OPTIONS"])
@require_permission("chat", "read")
def get_try_review(user_id, job_id):
    if not is_valid_uuid(job_id):
        return jsonify({"error": {"code": "invalid_input", "message": "Invalid job id."}}), 400

    from tasks.change_gating_try import read_job

    state = read_job(user_id, job_id)  # key includes user_id: no cross-user reads
    if state is None:
        return jsonify({"error": {"code": "not_found", "message": "No such review (results are kept for 1 hour)."}}), 404
    return jsonify(state)
