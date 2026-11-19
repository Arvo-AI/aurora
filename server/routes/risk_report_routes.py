"""Risk Report API: start a backtest from a repo or PR link, poll it, track the funnel.

POST /risk-report/runs          {"mode": "repo"|"pr", "target": "<link>"}  -> {"id"}
GET  /risk-report/runs/<id>     -> status, progress, report
POST /risk-report/events        {"event": "<funnel event>", "mode"?: ...}
GET  /risk-report/funnel        -> counters for each funnel step
"""

import logging

from flask import Blueprint, jsonify, request

from services.risk_report import store
from services.risk_report.public_github import parse_pr, parse_repo
from utils.auth.rbac_decorators import require_auth_only

logger = logging.getLogger(__name__)

risk_report_bp = Blueprint("risk_report", __name__)


@risk_report_bp.route("/runs", methods=["POST"])
@require_auth_only
def start_run(user_id):
    data = request.get_json(silent=True) or {}
    mode = data.get("mode")
    target = (data.get("target") or "").strip()[:300]
    try:
        if mode == "pr":
            parse_pr(target)
        elif mode == "repo":
            parse_repo(target)
        else:
            return jsonify({"error": "mode must be 'repo' or 'pr'"}), 400
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    from services.risk_report.tasks import run_risk_report

    run = store.new_run(user_id, mode, target)
    run_risk_report.delay(run["id"])
    store.track("report_requested", user_id, mode=mode)
    return jsonify({"id": run["id"]}), 202


@risk_report_bp.route("/runs/<run_id>", methods=["GET"])
@require_auth_only
def get_run(user_id, run_id):
    run = store.get_run(run_id)
    if not run or run.get("user_id") != user_id:
        return jsonify({"error": "Not found"}), 404
    return jsonify({k: v for k, v in run.items() if k != "user_id"})


@risk_report_bp.route("/events", methods=["POST"])
@require_auth_only
def track_event(user_id):
    data = request.get_json(silent=True) or {}
    mode = data.get("mode") if data.get("mode") in ("repo", "pr") else None
    if not store.track(data.get("event", ""), user_id, mode=mode, source=str(data.get("source", ""))[:40]):
        return jsonify({"error": "unknown event"}), 400
    return jsonify({"ok": True})


@risk_report_bp.route("/funnel", methods=["GET"])
@require_auth_only
def get_funnel(user_id):
    return jsonify(store.funnel())
