"""Audit log routes -- compliance-grade event tracking for user actions."""
import logging
from flask import Blueprint, request, jsonify
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

audit_bp = Blueprint("audit_log", __name__)


def record_audit_event(org_id, user_id, action, resource_type,
                       resource_id=None, detail=None, req=None):
    """Insert an audit log entry. Safe to call from any route -- failures are logged, never raised."""
    try:
        ip_address = None
        user_agent = None
        if req:
            ip_address = req.headers.get("X-Forwarded-For", req.remote_addr)
            user_agent = req.headers.get("User-Agent")

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO audit_log (org_id, user_id, action, resource_type, resource_id, detail, ip_address, user_agent)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                """, (
                    org_id, user_id, action, resource_type,
                    resource_id,
                    __import__("json").dumps(detail or {}),
                    ip_address, user_agent,
                ))
                conn.commit()
    except Exception:
        logger.exception("[AUDIT] Failed to record audit event: %s/%s", action, resource_type)


@audit_bp.route("/api/audit-log", methods=["GET"])
@require_permission("admin", "read")
def get_audit_log(user_id):
    """Paginated, filterable audit log."""
    org_id = get_org_id_from_request()
    page = max(int(request.args.get("page", 1)), 1)
    per_page = min(int(request.args.get("per_page", 50)), 200)
    offset = (page - 1) * per_page

    action_filter = request.args.get("action")
    resource_filter = request.args.get("resource_type")
    user_filter = request.args.get("user_id")
    period = request.args.get("period", "30d")

    interval_map = {"1d": "1 day", "7d": "7 days", "30d": "30 days", "90d": "90 days", "180d": "180 days", "365d": "365 days"}
    pg_interval = interval_map.get(period, "30 days")

    conditions = ["org_id = %s", "created_at >= NOW() - %s::interval"]
    params: list = [org_id, pg_interval]

    if action_filter:
        conditions.append("action = %s")
        params.append(action_filter)
    if resource_filter:
        conditions.append("resource_type = %s")
        params.append(resource_filter)
    if user_filter:
        conditions.append("user_id = %s")
        params.append(user_filter)

    where = " AND ".join(conditions)

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM audit_log WHERE {where}", params)
                total = cur.fetchone()[0]

                cur.execute(f"""
                    SELECT id, org_id, user_id, action, resource_type, resource_id, detail, ip_address, created_at
                    FROM audit_log
                    WHERE {where}
                    ORDER BY created_at DESC
                    LIMIT %s OFFSET %s
                """, params + [per_page, offset])
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]

        for row in rows:
            for k, v in row.items():
                if hasattr(v, "isoformat"):
                    row[k] = v.isoformat()

        return jsonify({
            "events": rows,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": max((total + per_page - 1) // per_page, 1),
        }), 200
    except Exception:
        logger.exception("[AUDIT] Failed to fetch audit log")
        return jsonify({"error": "Failed to fetch audit log"}), 500
