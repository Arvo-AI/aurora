"""Teams inside an org, and which accounts each team may use.

Blueprint: org_teams_bp
Prefix: /api/org
"""

from __future__ import annotations

import logging
import re

from flask import Blueprint, jsonify, request

from routes.connector_access import _catalog
from utils.auth.connector_access import load_grants
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request, set_rls_context
from utils.db.connection_pool import db_pool
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

org_teams_bp = Blueprint("org_teams", __name__, url_prefix="/api/org")

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,79}$")


def _known(user_id: str) -> dict:
    catalog = _catalog(user_id, load_grants(user_id))
    return {entry["id"]: {scope["id"] for scope in entry["scopes"]} for entry in catalog}


def _load(cur, org_id: str) -> dict:
    cur.execute(
        "SELECT id, email, name FROM users WHERE org_id = %s ORDER BY name, email",
        (org_id,),
    )
    members = [{"id": r[0], "email": r[1], "name": r[2] or ""} for r in cur.fetchall()]
    cur.execute(
        "SELECT id, name FROM org_teams WHERE org_id = %s ORDER BY name",
        (org_id,),
    )
    teams = [{"id": r[0], "name": r[1], "memberIds": [], "grants": []} for r in cur.fetchall()]
    by_id = {team["id"]: team for team in teams}
    if by_id:
        cur.execute(
            "SELECT team_id, user_id FROM org_team_members WHERE org_id = %s",
            (org_id,),
        )
        for team_id, user_id in cur.fetchall():
            if team_id in by_id:
                by_id[team_id]["memberIds"].append(user_id)
        cur.execute(
            """SELECT team_id, connector, scope_id FROM org_team_grants
                WHERE org_id = %s ORDER BY connector, scope_id""",
            (org_id,),
        )
        for team_id, connector, scope_id in cur.fetchall():
            if team_id in by_id:
                by_id[team_id]["grants"].append({"connector": connector, "scopeId": scope_id or ""})
    return {"members": members, "teams": teams}


def _replace(cur, org_id: str, team_id: str, member_ids: list, grants: list) -> None:
    cur.execute("DELETE FROM org_team_members WHERE org_id = %s AND team_id = %s", (org_id, team_id))
    cur.execute("DELETE FROM org_team_grants WHERE org_id = %s AND team_id = %s", (org_id, team_id))
    # One team per person: moving someone here removes them from any other team.
    if member_ids:
        cur.execute(
            "DELETE FROM org_team_members WHERE org_id = %s AND user_id = ANY(%s)",
            (org_id, member_ids),
        )
    for member_id in member_ids:
        cur.execute(
            """INSERT INTO org_team_members (org_id, team_id, user_id)
               VALUES (%s, %s, %s)""",
            (org_id, team_id, member_id),
        )
    for grant in grants:
        cur.execute(
            """INSERT INTO org_team_grants (org_id, team_id, connector, scope_id)
               VALUES (%s, %s, %s, %s)""",
            (org_id, team_id, grant["connector"], grant["scopeId"]),
        )


@org_teams_bp.route("/teams", methods=["GET"])
@require_permission("connectors", "read")
def list_teams(user_id: str):
    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No org context"}), 400
    try:
        with db_pool.get_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[OrgTeams]")
                return jsonify(_load(cur, org_id))
    except Exception:
        logger.exception("Failed to list teams")
        return jsonify({"error": "Could not load teams"}), 500


@org_teams_bp.route("/teams", methods=["POST"])
@require_permission("admin", "access")
def create_team(user_id: str):
    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No org context"}), 400
    name = str((request.get_json(silent=True) or {}).get("name") or "").strip()
    if not _NAME.match(name):
        return jsonify({"error": "Team name must start with a letter or number"}), 400
    try:
        with db_pool.get_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[OrgTeams]")
                cur.execute(
                    "SELECT 1 FROM org_teams WHERE org_id = %s AND lower(name) = lower(%s)",
                    (org_id, name),
                )
                # Same name would make the two teams impossible to tell apart
                if cur.fetchone():
                    return jsonify({"error": "A team with that name already exists"}), 400
                cur.execute(
                    "INSERT INTO org_teams (org_id, name) VALUES (%s, %s) RETURNING id",
                    (org_id, name),
                )
                team_id = cur.fetchone()[0]
            conn.commit()
        logger.info("Created team %s", sanitize(name))
        return jsonify({"id": team_id, "name": name, "memberIds": [], "grants": []}), 201
    except Exception:
        logger.exception("Failed to create team")
        return jsonify({"error": "Could not create team"}), 500


@org_teams_bp.route("/teams/membership", methods=["PUT"])
@require_permission("admin", "access")
def set_membership(user_id: str):
    """One team per person. A null teamId clears membership."""
    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No org context"}), 400
    body = request.get_json(silent=True) or {}
    target = body.get("userId")
    team_id = body.get("teamId")
    if not isinstance(target, str) or not target:
        return jsonify({"error": "Invalid member"}), 400
    # Empty clears the assignment. Anything else must name a team.
    if team_id is not None and (not isinstance(team_id, str) or not team_id):
        return jsonify({"error": "Invalid team"}), 400
    try:
        with db_pool.get_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[OrgTeams]")
                cur.execute(
                    "SELECT 1 FROM users WHERE org_id = %s AND id = %s",
                    (org_id, target),
                )
                if not cur.fetchone():
                    return jsonify({"error": "Unknown member"}), 400
                # None clears membership, so there is no team row to look up
                if team_id:
                    cur.execute(
                        "SELECT 1 FROM org_teams WHERE org_id = %s AND id = %s",
                        (org_id, team_id),
                    )
                    if not cur.fetchone():
                        return jsonify({"error": "Unknown team"}), 404
                cur.execute(
                    "DELETE FROM org_team_members WHERE org_id = %s AND user_id = %s",
                    (org_id, target),
                )
                if team_id:
                    cur.execute(
                        """INSERT INTO org_team_members (org_id, team_id, user_id)
                           VALUES (%s, %s, %s)""",
                        (org_id, team_id, target),
                    )
            conn.commit()
        return jsonify({"userId": target, "teamId": team_id})
    except Exception:
        logger.exception("Failed to set team membership")
        return jsonify({"error": "Could not update team"}), 500


@org_teams_bp.route("/teams/<team_id>", methods=["PUT"])
@require_permission("admin", "access")
def update_team(user_id: str, team_id: str):
    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No org context"}), 400
    body = request.get_json(silent=True) or {}
    member_ids = body.get("memberIds")
    raw_grants = body.get("grants")
    if not isinstance(member_ids, list) or not isinstance(raw_grants, list):
        return jsonify({"error": "`memberIds` and `grants` must be lists"}), 400
    if not all(isinstance(item, str) and item for item in member_ids):
        return jsonify({"error": "Invalid member"}), 400

    known = _known(user_id)
    grants = []
    seen = set()
    for item in raw_grants:
        if not isinstance(item, dict):
            return jsonify({"error": "Invalid grant"}), 400
        connector = str(item.get("connector") or "")
        scope_id = str(item.get("scopeId") or "")
        if connector not in known or scope_id not in known[connector]:
            return jsonify({"error": "Unknown account"}), 400
        key = (connector, scope_id)
        if key in seen:
            continue
        seen.add(key)
        grants.append({"connector": connector, "scopeId": scope_id})

    try:
        with db_pool.get_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[OrgTeams]")
                cur.execute(
                    "SELECT name FROM org_teams WHERE org_id = %s AND id = %s",
                    (org_id, team_id),
                )
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "Unknown team"}), 404
                if member_ids:
                    cur.execute(
                        "SELECT id FROM users WHERE org_id = %s AND id = ANY(%s)",
                        (org_id, member_ids),
                    )
                    found = {r[0] for r in cur.fetchall()}
                    # A member id from another org must not be attached
                    if found != set(member_ids):
                        return jsonify({"error": "Unknown member"}), 400
                _replace(cur, org_id, team_id, member_ids, grants)
            conn.commit()
        logger.info("Updated team %s members=%d grants=%d", sanitize(row[0]), len(member_ids), len(grants))
        return jsonify({"id": team_id, "name": row[0], "memberIds": member_ids, "grants": grants})
    except Exception:
        logger.exception("Failed to update team")
        return jsonify({"error": "Could not update team"}), 500


@org_teams_bp.route("/teams/<team_id>", methods=["DELETE"])
@require_permission("admin", "access")
def delete_team(user_id: str, team_id: str):
    org_id = get_org_id_from_request()
    if not org_id:
        return jsonify({"error": "No org context"}), 400
    try:
        with db_pool.get_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[OrgTeams]")
                cur.execute(
                    "DELETE FROM org_teams WHERE org_id = %s AND id = %s RETURNING name",
                    (org_id, team_id),
                )
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "Unknown team"}), 404
                # No foreign key, so a deleted team would keep its overrides.
                cur.execute(
                    "DELETE FROM org_connector_access WHERE org_id = %s AND team_id = %s",
                    (org_id, team_id),
                )
            conn.commit()
        logger.info("Deleted team %s", sanitize(row[0]))
        return jsonify({"ok": True})
    except Exception:
        logger.exception("Failed to delete team")
        return jsonify({"error": "Could not delete team"}), 500
