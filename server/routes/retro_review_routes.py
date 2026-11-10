"""Retro review API: replay Aurora's pre-merge review on a public repo's past PRs.

POST /api/retro-reviews          start a job  {"repo": "owner/repo"}
GET  /api/retro-reviews          the org's recent jobs
GET  /api/retro-reviews/<id>     one job with its per-PR results

No GitHub install or credential is involved: the job reads public data only.
"""

import logging
import uuid

from flask import Blueprint, jsonify, request

from services.change_gating import retro
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request, set_rls_context
from utils.db.connection_pool import db_pool
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

retro_review_bp = Blueprint("retro_review", __name__)
_LOG = "[RetroReview]"

# A job that has not finished within this window is treated as dead, so a
# crashed worker cannot block the org from starting a new one forever.
_ACTIVE_WINDOW = "2 hours"


def _job_row_to_dict(row, include_items: bool) -> dict:
    job = {
        "id": str(row[0]),
        "repo": row[1],
        "status": row[2],
        "summary": row[3],
        "error": row[4],
        "created_at": row[5].isoformat() if row[5] else None,
        "started_at": row[6].isoformat() if row[6] else None,
        "completed_at": row[7].isoformat() if row[7] else None,
    }
    if include_items:
        job["items"] = row[8] or []
    return job


_SELECT = (
    "SELECT id, repo, status, summary, error, created_at, started_at, completed_at, items "
    "FROM retro_reviews"
)


def _bounded_int(value, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(number, retro.MAX_PER_SET))


@retro_review_bp.route("/api/retro-reviews", methods=["POST"])
@require_permission("incidents", "write")
def start_retro_review(user_id):
    data = request.get_json(silent=True) or {}
    try:
        repo = retro.normalize_repo(data.get("repo", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    max_reverted = _bounded_int(data.get("max_reverted"), retro.DEFAULT_MAX_REVERTED)
    max_controls = _bounded_int(data.get("max_controls"), retro.DEFAULT_MAX_CONTROLS)

    org_id = get_org_id_from_request()
    with db_pool.get_user_connection() as conn:
        cursor = conn.cursor()
        if not set_rls_context(cursor, conn, user_id, org_id=org_id, log_prefix=_LOG):
            return jsonify({"error": "Organization not found"}), 403

        # Each job is up to 30 LLM calls: one at a time per org.
        cursor.execute(
            "SELECT id FROM retro_reviews WHERE org_id = %s AND status IN ('pending', 'running') "
            f"AND created_at > NOW() - INTERVAL '{_ACTIVE_WINDOW}' LIMIT 1",
            (org_id,),
        )
        running = cursor.fetchone()
        if running:
            return jsonify({
                "error": "A retro review is already running for your organization",
                "id": str(running[0]),
            }), 409

        cursor.execute(
            """INSERT INTO retro_reviews (org_id, user_id, repo, max_reverted, max_controls)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (org_id, user_id, repo, max_reverted, max_controls),
        )
        job_id = str(cursor.fetchone()[0])
        conn.commit()

    from tasks.retro_review import run_retro_review
    run_retro_review.delay(job_id, user_id)

    logger.info("%s started job %s repo=%s user=%s", _LOG, job_id, sanitize(repo), sanitize(user_id))
    return jsonify({"id": job_id, "repo": repo, "status": "pending"}), 202


@retro_review_bp.route("/api/retro-reviews", methods=["GET"])
@require_permission("incidents", "read")
def list_retro_reviews(user_id):
    org_id = get_org_id_from_request()
    with db_pool.get_user_connection() as conn:
        cursor = conn.cursor()
        if not set_rls_context(cursor, conn, user_id, org_id=org_id, log_prefix=_LOG):
            return jsonify({"error": "Organization not found"}), 403
        cursor.execute(f"{_SELECT} WHERE org_id = %s ORDER BY created_at DESC LIMIT 20", (org_id,))
        rows = cursor.fetchall()
    return jsonify({"retro_reviews": [_job_row_to_dict(r, include_items=False) for r in rows]})


@retro_review_bp.route("/api/retro-reviews/<job_id>", methods=["GET"])
@require_permission("incidents", "read")
def get_retro_review(user_id, job_id):
    try:
        uuid.UUID(job_id)
    except ValueError:
        return jsonify({"error": "Not found"}), 404
    org_id = get_org_id_from_request()
    with db_pool.get_user_connection() as conn:
        cursor = conn.cursor()
        if not set_rls_context(cursor, conn, user_id, org_id=org_id, log_prefix=_LOG):
            return jsonify({"error": "Organization not found"}), 403
        cursor.execute(f"{_SELECT} WHERE id = %s AND org_id = %s", (job_id, org_id))
        row = cursor.fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify(_job_row_to_dict(row, include_items=True))
