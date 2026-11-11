"""Revert backtest API: find reverted PRs in a public repo and review them pre-merge."""

import logging
import uuid

from flask import Blueprint, jsonify, request

from utils.auth.rbac_decorators import require_permission

logger = logging.getLogger(__name__)

backtest_bp = Blueprint("change_gating_backtest", __name__)

# Each review is one LLM call; cap per user so the endpoint can't be used to burn tokens.
_MAX_REVIEWS_PER_HOUR = 20


def _error(exc):
    return jsonify({"error": str(exc)}), getattr(exc, "status_code", 400)


@backtest_bp.route("/api/backtest/candidates", methods=["POST"])
@require_permission("chat", "write")
def find_backtest_candidates(user_id):
    from services.change_gating.backtest import BacktestError, PublicGitHubClient, find_candidates, parse_repo_url

    data = request.get_json(silent=True) or {}
    client = PublicGitHubClient()
    try:
        owner, repo = parse_repo_url(data.get("repo_url", ""))
        result = find_candidates(client, owner, repo)
    except BacktestError as exc:
        return _error(exc)
    finally:
        client.close()
    logger.info(
        "backtest_event=candidates_found user=%s repo=%s reverted=%d controls=%d",
        user_id, result["repo"], len(result["reverted"]), len(result["controls"]),
    )
    return jsonify(result)


@backtest_bp.route("/api/backtest/reviews", methods=["POST"])
@require_permission("chat", "write")
def start_backtest_review(user_id):
    from services.change_gating.backtest import BacktestError, parse_repo_url
    from tasks.change_gating_backtest import backtest_pr, save_job
    from utils.cache.redis_client import get_redis_client

    data = request.get_json(silent=True) or {}
    try:
        owner, repo = parse_repo_url(data.get("repo_url", ""))
    except BacktestError as exc:
        return _error(exc)
    pr_number = data.get("pr_number")
    if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number <= 0:
        return jsonify({"error": "pr_number must be a positive integer"}), 400
    kind = data.get("kind") if data.get("kind") in ("reverted", "control") else "unknown"

    redis_client = get_redis_client()
    if redis_client is None:
        return jsonify({"error": "Backtest is unavailable (Redis down)"}), 503
    quota_key = f"change_gating:backtest:quota:{user_id}"
    used = redis_client.incr(quota_key)
    if used == 1:
        redis_client.expire(quota_key, 3600)
    if used > _MAX_REVIEWS_PER_HOUR:
        return jsonify({"error": "Backtest limit reached, try again in an hour"}), 429

    job_id = str(uuid.uuid4())
    repo_full_name = f"{owner}/{repo}"
    save_job(user_id, job_id, {"repo": repo_full_name, "pr_number": pr_number, "kind": kind, "status": "running"})
    backtest_pr.delay(user_id, job_id, repo_full_name, pr_number, kind)
    logger.info("backtest_event=review_started user=%s repo=%s pr=%s kind=%s", user_id, repo_full_name, pr_number, kind)
    return jsonify({"job_id": job_id, "status": "running"}), 202


@backtest_bp.route("/api/backtest/reviews/<job_id>", methods=["GET"])
@require_permission("chat", "read")
def get_backtest_review(user_id, job_id):
    from tasks.change_gating_backtest import load_job

    try:
        uuid.UUID(job_id)
    except ValueError:
        return jsonify({"error": "Invalid job id"}), 400
    state = load_job(user_id, job_id)
    if state is None:
        return jsonify({"error": "Job not found or expired"}), 404
    return jsonify(state)
