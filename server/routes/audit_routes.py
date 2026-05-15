"""Audit log routes -- compliance-grade event tracking for user actions."""
import json
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
            # X-Forwarded-For can be a comma-separated chain; the first token
            # is the original client IP, the rest are intermediate proxies.
            forwarded = req.headers.get("X-Forwarded-For")
            if forwarded:
                ip_address = forwarded.split(",", 1)[0].strip() or req.remote_addr
            else:
                ip_address = req.remote_addr
            user_agent = req.headers.get("User-Agent")

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                # No RLS needed — audit_log not RLS-protected
                cur.execute("""
                    INSERT INTO audit_log (org_id, user_id, action, resource_type, resource_id, detail, ip_address, user_agent)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                """, (
                    org_id, user_id, action, resource_type,
                    resource_id,
                    json.dumps(detail or {}),
                    ip_address, user_agent,
                ))
                conn.commit()
    except Exception:
        logger.exception("[AUDIT] Failed to record audit event: %s/%s", action, resource_type)


def _iso(val):
    return val.isoformat() if hasattr(val, "isoformat") else val


def _synthetic_events(cur, org_id, pg_interval):
    """Pull activity from core tables to fill gaps in the audit_log."""
    events = []

    cur.execute("""
        SELECT id, email, name, role, created_at
        FROM users WHERE org_id = %s AND created_at >= NOW() - %s::interval
        ORDER BY created_at DESC LIMIT 200
    """, (org_id, pg_interval))
    for row in cur.fetchall():
        events.append({
            "id": None, "org_id": org_id, "user_id": row[0],
            "action": "member_joined", "resource_type": "user",
            "resource_id": row[0],
            "detail": {"email": row[1], "name": row[2], "role": row[3] or "viewer"},
            "ip_address": None, "created_at": _iso(row[4]),
        })

    cur.execute("""
        SELECT id, source_type, alert_title, severity, status, created_at, user_id
        FROM incidents
        WHERE org_id = %s AND created_at >= NOW() - %s::interval
        ORDER BY created_at DESC LIMIT 200
    """, (org_id, pg_interval))
    for row in cur.fetchall():
        events.append({
            "id": None, "org_id": org_id, "user_id": row[6] or "",
            "action": "incident_created", "resource_type": "incident",
            "resource_id": str(row[0]),
            "detail": {"source": row[1], "title": row[2], "severity": row[3], "status": row[4]},
            "ip_address": None, "created_at": _iso(row[5]),
        })

    cur.execute("""
        SELECT ut.provider, ut.timestamp, u.id, u.name, u.email
        FROM user_tokens ut
        JOIN users u ON ut.user_id = u.id
        WHERE (ut.org_id = %s OR u.org_id = %s)
          AND ut.secret_ref IS NOT NULL AND ut.is_active = TRUE
          AND ut.timestamp >= NOW() - %s::interval
        ORDER BY ut.timestamp DESC LIMIT 100
    """, (org_id, org_id, pg_interval))
    for row in cur.fetchall():
        events.append({
            "id": None, "org_id": org_id, "user_id": row[2],
            "action": "connector_added", "resource_type": "connector",
            "resource_id": row[0],
            "detail": {"provider": row[0], "user_name": row[3] or row[4]},
            "ip_address": None, "created_at": _iso(row[1]),
        })

    cur.execute("""
        SELECT uc.provider, uc.last_verified_at, u.id, u.name, u.email
        FROM user_connections uc
        JOIN users u ON uc.user_id = u.id
        WHERE (uc.org_id = %s OR u.org_id = %s)
          AND uc.status = 'active'
          AND uc.last_verified_at >= NOW() - %s::interval
        ORDER BY uc.last_verified_at DESC LIMIT 100
    """, (org_id, org_id, pg_interval))
    seen_providers = {e["resource_id"] for e in events if e["action"] == "connector_added"}
    for row in cur.fetchall():
        if row[0] not in seen_providers:
            events.append({
                "id": None, "org_id": org_id, "user_id": row[2],
                "action": "connector_added", "resource_type": "connector",
                "resource_id": row[0],
                "detail": {"provider": row[0], "user_name": row[3] or row[4]},
                "ip_address": None, "created_at": _iso(row[1]),
            })

    return events


@audit_bp.route("/api/audit-log", methods=["GET"])
@require_permission("incidents", "read")
def get_audit_log(user_id):
    """Paginated, filterable audit log — merges audit_log table with synthetic activity events."""
    org_id = get_org_id_from_request()

    try:
        page = max(int(request.args.get("page", 1)), 1)
    except (TypeError, ValueError):
        return jsonify({"error": "page must be a positive integer"}), 400
    try:
        per_page = min(max(int(request.args.get("per_page", 50)), 1), 200)
    except (TypeError, ValueError):
        return jsonify({"error": "per_page must be a positive integer"}), 400

    action_filter = request.args.get("action")
    resource_filter = request.args.get("resource_type")
    user_filter = request.args.get("user_id")
    period = request.args.get("period", "30d")

    interval_map = {"1d": "1 day", "7d": "7 days", "30d": "30 days", "90d": "90 days", "180d": "180 days", "365d": "365 days"}
    pg_interval = interval_map.get(period, "30 days")

    conditions = ["org_id = %s", "created_at >= NOW() - %s::interval"]
    params: list = [org_id, pg_interval]

    if action_filter:
        conditions.append("action ILIKE %s")
        params.append(f"%{action_filter}%")
    if resource_filter:
        conditions.append("resource_type ILIKE %s")
        params.append(f"%{resource_filter}%")
    if user_filter:
        conditions.append("user_id = %s")
        params.append(user_filter)

    where = " AND ".join(conditions)

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                # No RLS needed — audit_log not RLS-protected
                cur.execute(f"""
                    SELECT id, org_id, user_id, action, resource_type, resource_id, detail, ip_address, created_at
                    FROM audit_log
                    WHERE {where}
                    ORDER BY created_at DESC
                """, params)
                cols = [d[0] for d in cur.description]
                audit_rows = [dict(zip(cols, row)) for row in cur.fetchall()]

                synthetic = _synthetic_events(cur, org_id, pg_interval)

        for row in audit_rows:
            for k, v in row.items():
                if hasattr(v, "isoformat"):
                    row[k] = v.isoformat()

        all_events = audit_rows + synthetic

        if action_filter:
            filt = action_filter.lower()
            all_events = [e for e in all_events if filt in (e.get("action") or "").lower()]
        if resource_filter:
            filt = resource_filter.lower()
            all_events = [e for e in all_events if filt in (e.get("resource_type") or "").lower()]

        all_events.sort(key=lambda e: e.get("created_at") or "", reverse=True)

        total = len(all_events)
        offset = (page - 1) * per_page
        page_events = all_events[offset:offset + per_page]

        return jsonify({
            "events": page_events,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": max((total + per_page - 1) // per_page, 1),
        }), 200
    except Exception:
        logger.exception("[AUDIT] Failed to fetch audit log")
        return jsonify({"error": "Failed to fetch audit log"}), 500
