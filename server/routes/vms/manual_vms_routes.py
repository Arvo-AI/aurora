import logging
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, request
from psycopg2 import sql

from utils.db.connection_pool import db_pool
from utils.log_sanitizer import sanitize
from utils.web.limiter_ext import limiter
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request, set_rls_context
from utils.ssh.ssh_utils import (
    load_user_private_key_safe,
    parse_ssh_key_id,
    validate_and_test_ssh,
)

manual_vms_bp = Blueprint("manual_vms_bp", __name__)
logger = logging.getLogger(__name__)


def _parse_port(raw_port: Any, default: int = 22) -> int:
    if raw_port is None:
        return default
    try:
        port_int = int(raw_port)
    except (TypeError, ValueError):
        raise ValueError("Port must be an integer")
    if not (1 <= port_int <= 65535):
        raise ValueError("Port must be between 1 and 65535")
    return port_int


def _validate_required(fields: Dict[str, Any]):
    missing = [k for k, v in fields.items() if not v]
    if missing:
        raise ValueError(f"Missing required field(s): {', '.join(missing)}")


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return value


def _as_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    return int(value)


def connection_fields_changed(existing: Dict[str, Any], proposed: Dict[str, Any]) -> bool:
    """True when this write points SSH at a different host, key, or user."""
    casters = {
        "ip_address": _blank_to_none,
        "port": _as_int,
        "ssh_jump_command": _blank_to_none,
        "ssh_key_id": _as_int,
        "ssh_username": _blank_to_none,
    }
    for field, caster in casters.items():
        # Omitted fields are left as stored, so they cannot invalidate a check
        if field not in proposed:
            continue
        if caster(existing.get(field)) != caster(proposed[field]):
            return True
    return False


def _org_for_write(cur, conn, user_id: str, log_prefix: str) -> Optional[str]:
    """Return the org to stamp on this write, or None when RLS cannot be set."""
    return set_rls_context(
        cur,
        conn,
        user_id,
        org_id=get_org_id_from_request(),
        log_prefix=log_prefix,
    )


def _serialize_vm_row(row: tuple, is_shared: bool = False) -> Dict[str, Any]:
    (
        vm_id,
        name,
        ip_address,
        port,
        ssh_jump_command,
        ssh_key_id,
        ssh_username,
        connection_verified,
        created_at,
        updated_at,
    ) = row
    return {
        "id": vm_id,
        "name": name,
        "ipAddress": ip_address,
        "port": port,
        "sshJumpCommand": ssh_jump_command,
        "sshKeyId": ssh_key_id,
        "sshUsername": ssh_username,
        "connectionVerified": connection_verified,
        "createdAt": created_at.isoformat() if created_at else None,
        "updatedAt": updated_at.isoformat() if updated_at else None,
        "source": "manual",
        "isShared": is_shared,
    }


@manual_vms_bp.route("/api/vms/manual", methods=["GET"])
@limiter.limit("30 per minute;200 per hour")
@require_permission("vms", "read")
def list_manual_vms(user_id):
    from utils.db.org_scope import resolve_org, org_read_predicate
    org_id = resolve_org(user_id)
    predicate, pred_params = org_read_predicate(user_id, org_id)
    with db_pool.get_user_connection() as conn:
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[VMs:list]")
            cur.execute(
                f"""
                SELECT id, name, ip_address, port, ssh_jump_command, ssh_key_id, ssh_username, connection_verified, created_at, updated_at, user_id
                FROM user_manual_vms
                WHERE {predicate}
                ORDER BY created_at DESC
                """,
                pred_params,
            )
            rows = cur.fetchall()
    vms = []
    for r in rows:
        row_data = r[:10]
        row_owner_id = r[10]
        vms.append(_serialize_vm_row(row_data, is_shared=(row_owner_id != user_id)))
    return jsonify({"vms": vms})


@manual_vms_bp.route("/api/vms/manual", methods=["POST"])
@limiter.limit("20 per minute;100 per hour")
@require_permission("vms", "write")
def create_manual_vm(user_id):
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    ip_address = (data.get("ipAddress") or data.get("ip_address") or "").strip()
    ssh_jump_command = (
        data.get("sshJumpCommand") or data.get("ssh_jump_command") or ""
    ).strip() or None
    ssh_username = (
        data.get("sshUsername") or data.get("ssh_username") or ""
    ).strip() or None
    ssh_key_id = data.get("sshKeyId") or data.get("ssh_key_id")

    try:
        _validate_required({"name": name, "ipAddress": ip_address})
        port = _parse_port(data.get("port"), default=22)
        if ssh_key_id is not None:
            ssh_key_id, error_msg = parse_ssh_key_id(ssh_key_id)
            if error_msg:
                return jsonify({"error": error_msg}), 400
            _, error_msg = load_user_private_key_safe(user_id, ssh_key_id)
            if error_msg:
                return jsonify({"error": error_msg}), 400
    except ValueError as exc:
        return jsonify({"error": "Invalid input parameters"}), 400

    with db_pool.get_user_connection() as conn:
        with conn.cursor() as cur:
            # Stamp org_id on insert; boot backfill only catches rows that missed it
            org_id = _org_for_write(cur, conn, user_id, "[VMs:create]")
            if not org_id:
                return jsonify({"error": "Organization context required"}), 400
            # Same SSH target keeps connection_verified; a new key, user, or jump host does not
            cur.execute(
                """
                INSERT INTO user_manual_vms (user_id, org_id, name, ip_address, port, ssh_jump_command, ssh_key_id, ssh_username, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (user_id, ip_address, port) DO UPDATE
                    SET name = EXCLUDED.name,
                        org_id = EXCLUDED.org_id,
                        ssh_jump_command = EXCLUDED.ssh_jump_command,
                        ssh_key_id = EXCLUDED.ssh_key_id,
                        ssh_username = EXCLUDED.ssh_username,
                        connection_verified = CASE
                            WHEN NULLIF(BTRIM(user_manual_vms.ssh_jump_command), '')
                                 IS DISTINCT FROM NULLIF(BTRIM(EXCLUDED.ssh_jump_command), '')
                              OR user_manual_vms.ssh_key_id IS DISTINCT FROM EXCLUDED.ssh_key_id
                              OR NULLIF(BTRIM(user_manual_vms.ssh_username), '')
                                 IS DISTINCT FROM NULLIF(BTRIM(EXCLUDED.ssh_username), '')
                            THEN FALSE
                            ELSE user_manual_vms.connection_verified
                        END,
                        updated_at = NOW()
                RETURNING id, name, ip_address, port, ssh_jump_command, ssh_key_id, ssh_username, connection_verified, created_at, updated_at;
                """,
                (
                    user_id,
                    org_id,
                    name,
                    ip_address,
                    port,
                    ssh_jump_command,
                    ssh_key_id,
                    ssh_username,
                ),
            )
            row = cur.fetchone()
            conn.commit()

    if not row:
        return jsonify({"error": "Failed to create VM"}), 500
    return jsonify(_serialize_vm_row(row)), 201


@manual_vms_bp.route("/api/vms/manual/<int:vm_id>", methods=["PUT"])
@limiter.limit("20 per minute;100 per hour")
@require_permission("vms", "write")
def update_manual_vm(user_id, vm_id: int):
    data = request.get_json() or {}
    name = (data.get("name") or "").strip() or None
    ip_address = (data.get("ipAddress") or data.get("ip_address") or "").strip() or None
    ssh_jump_command = (
        data.get("sshJumpCommand") or data.get("ssh_jump_command") or ""
    ).strip() or None
    ssh_key_id = data.get("sshKeyId") or data.get("ssh_key_id")
    ssh_username = (
        data.get("sshUsername") or data.get("ssh_username") or ""
    ).strip() or None
    port = data.get("port")

    try:
        port_val = _parse_port(port, default=None) if port is not None else None
        if ssh_key_id is not None:
            ssh_key_id, error_msg = parse_ssh_key_id(ssh_key_id)
            if error_msg:
                return jsonify({"error": error_msg}), 400
            _, error_msg = load_user_private_key_safe(user_id, ssh_key_id)
            if error_msg:
                return jsonify({"error": error_msg}), 400
    except ValueError as exc:
        return jsonify({"error": "Invalid input parameters"}), 400

    updates = []
    params = []
    proposed_connection: Dict[str, Any] = {}

    if name is not None:
        updates.append(sql.SQL("name = %s"))
        params.append(name)
    if ip_address is not None:
        updates.append(sql.SQL("ip_address = %s"))
        params.append(ip_address)
        proposed_connection["ip_address"] = ip_address
    if port_val is not None:
        updates.append(sql.SQL("port = %s"))
        params.append(port_val)
        proposed_connection["port"] = port_val
    if (
        ssh_jump_command is not None
        or "sshJumpCommand" in data
        or "ssh_jump_command" in data
    ):
        updates.append(sql.SQL("ssh_jump_command = %s"))
        params.append(ssh_jump_command)
        proposed_connection["ssh_jump_command"] = ssh_jump_command
    if ssh_key_id is not None or "sshKeyId" in data or "ssh_key_id" in data:
        updates.append(sql.SQL("ssh_key_id = %s"))
        params.append(ssh_key_id)
        proposed_connection["ssh_key_id"] = ssh_key_id
    if ssh_username is not None or "sshUsername" in data or "ssh_username" in data:
        updates.append(sql.SQL("ssh_username = %s"))
        params.append(ssh_username)
        proposed_connection["ssh_username"] = ssh_username

    if not updates:
        return jsonify({"error": "No fields to update"}), 400

    with db_pool.get_user_connection() as conn:
        with conn.cursor() as cur:
            org_id = _org_for_write(cur, conn, user_id, "[VMs:update]")
            if not org_id:
                return jsonify({"error": "Organization context required"}), 400
            cur.execute(
                """
                SELECT user_id, ip_address, port, ssh_jump_command, ssh_key_id, ssh_username
                FROM user_manual_vms WHERE id = %s
                """,
                (vm_id,),
            )
            existing = cur.fetchone()
            if not existing:
                return jsonify({"error": "Manual VM not found"}), 404
            if existing[0] != user_id:
                return jsonify({"error": "Cannot modify a shared VM"}), 403

            stored_connection = {
                "ip_address": existing[1],
                "port": existing[2],
                "ssh_jump_command": existing[3],
                "ssh_key_id": existing[4],
                "ssh_username": existing[5],
            }
            # Resending the same SSH target must not clear a connection that already worked
            if connection_fields_changed(stored_connection, proposed_connection):
                updates.append(sql.SQL("connection_verified = FALSE"))
            # Stamp org_id on the write so the row is visible without a server restart
            updates.append(sql.SQL("org_id = %s"))
            params.append(org_id)
            updates.append(sql.SQL("updated_at = NOW()"))

            query = sql.SQL(
                """
                UPDATE user_manual_vms
                SET {updates}
                WHERE id = %s AND user_id = %s
                RETURNING id, name, ip_address, port, ssh_jump_command, ssh_key_id, ssh_username, connection_verified, created_at, updated_at;
                """
            ).format(updates=sql.SQL(", ").join(updates))
            cur.execute(query, params + [vm_id, user_id])
            row = cur.fetchone()
            conn.commit()

    if not row:
        return jsonify({"error": "Manual VM not found"}), 404

    return jsonify(_serialize_vm_row(row))


@manual_vms_bp.route("/api/vms/manual/<int:vm_id>", methods=["DELETE"])
@limiter.limit("20 per minute;100 per hour")
@require_permission("vms", "write")
def delete_manual_vm(user_id, vm_id: int):
    with db_pool.get_user_connection() as conn:
        with conn.cursor() as cur:
            _org_for_write(cur, conn, user_id, "[VMs:delete]")
            cur.execute(
                "SELECT user_id FROM user_manual_vms WHERE id = %s",
                (vm_id,),
            )
            existing = cur.fetchone()
            if not existing:
                return jsonify({"error": "Manual VM not found"}), 404
            if existing[0] != user_id:
                return jsonify({"error": "Cannot delete a shared VM"}), 403
            cur.execute(
                "DELETE FROM user_manual_vms WHERE id = %s AND user_id = %s RETURNING id;",
                (vm_id, user_id),
            )
            row = cur.fetchone()
            conn.commit()
    if not row:
        return jsonify({"error": "Manual VM not found"}), 404
    return jsonify({"deleted": True})


@manual_vms_bp.route("/api/vms/check-connection", methods=["POST"])
@limiter.limit("30 per minute;120 per hour")
@require_permission("vms", "write")
def check_manual_vm_connection(user_id):
    data = request.get_json() or {}
    vm_id = data.get("vmId")
    ip_address = (data.get("ipAddress") or data.get("ip_address") or "").strip()
    port = data.get("port")
    username = (data.get("username") or "").strip()
    ssh_key_id = data.get("sshKeyId") or data.get("ssh_key_id")
    ssh_jump_command = (
        data.get("sshJumpCommand") or data.get("ssh_jump_command") or ""
    ).strip() or None
    ssh_username = (
        data.get("sshUsername") or data.get("ssh_username") or ""
    ).strip() or None

    # If vmId provided, load defaults from DB
    stored_target = None
    if vm_id is not None:
        try:
            vm_id = int(vm_id)
        except (TypeError, ValueError):
            return jsonify({"error": "vmId must be an integer"}), 400
        with db_pool.get_user_connection() as conn:
            with conn.cursor() as cur:
                _org_for_write(cur, conn, user_id, "[VMs:check-load]")
                cur.execute(
                    """
                    SELECT ip_address, port, ssh_jump_command, ssh_key_id, ssh_username, user_id
                    FROM user_manual_vms
                    WHERE id = %s
                    """,
                    (vm_id,),
                )
                vm_row = cur.fetchone()
        if not vm_row:
            return jsonify({"error": "Manual VM not found"}), 404
        if vm_row[5] != user_id:
            return jsonify({"error": "Cannot run connection check on a shared VM"}), 403

        stored_target = {
            "ip_address": vm_row[0],
            "port": vm_row[1],
            "ssh_jump_command": vm_row[2],
            "ssh_key_id": vm_row[3],
            "ssh_username": vm_row[4],
        }
        ip_address = ip_address or vm_row[0]
        port = port or vm_row[1]
        ssh_jump_command = ssh_jump_command or vm_row[2]
        ssh_key_id = ssh_key_id or vm_row[3]
        ssh_username = ssh_username or vm_row[4]

    if not username and ssh_username:
        username = ssh_username

    try:
        _validate_required(
            {"ipAddress": ip_address, "username": username, "sshKeyId": ssh_key_id}
        )
        port_val = _parse_port(port, default=22)
        ssh_key_id, error_msg = parse_ssh_key_id(ssh_key_id)
        if error_msg:
            return jsonify({"error": error_msg}), 400
        private_key, error_msg = load_user_private_key_safe(user_id, ssh_key_id)
        if error_msg:
            return jsonify({"error": error_msg}), 400
    except ValueError as exc:
        return jsonify({"error": "Invalid input parameters"}), 400

    try:
        success, error_msg, connected_as = validate_and_test_ssh(
            ip_address,
            username,
            private_key,
            timeout=30,
            port=port_val,
            jump_command=ssh_jump_command,
        )
        if not success:
            return jsonify({"success": False, "error": error_msg}), 400
        tested_target = {
            "ip_address": ip_address,
            "port": port_val,
            "ssh_jump_command": ssh_jump_command,
            "ssh_key_id": ssh_key_id,
            "ssh_username": username,
        }
        # A one-off user, key, or host must not mark the saved VM Ready
        matches_stored = stored_target is not None and not connection_fields_changed(
            stored_target, tested_target
        )
        verified = False
        if vm_id is not None and matches_stored:
            try:
                with db_pool.get_user_connection() as conn:
                    with conn.cursor() as cur:
                        org_id = _org_for_write(cur, conn, user_id, "[VMs:check-verify]")
                        # A verified row with a null org_id stays hidden until the next boot backfill
                        if org_id:
                            cur.execute(
                                """
                                UPDATE user_manual_vms
                                SET connection_verified = TRUE, org_id = %s, updated_at = NOW()
                                WHERE id = %s AND user_id = %s
                                """,
                                (org_id, vm_id, user_id),
                            )
                        # No org to stamp — still record the check for the owning user
                        else:
                            cur.execute(
                                """
                                UPDATE user_manual_vms
                                SET connection_verified = TRUE, updated_at = NOW()
                                WHERE id = %s AND user_id = %s
                                """,
                                (vm_id, user_id),
                            )
                        conn.commit()
                verified = True
                logger.info(f"Connection verified for VM {sanitize(vm_id)}, user {sanitize(user_id)}")
            except Exception as db_exc:
                logger.error(
                    f"Failed to update connection_verified for VM {sanitize(vm_id)}: {db_exc}"
                )
        return jsonify({
            "success": True,
            "connectedAs": connected_as,
            "connectionVerified": verified,
        })
    except Exception as exc:
        logger.error("SSH validation failed unexpectedly: %s", exc, exc_info=True)
        return jsonify(
            {"success": False, "error": "SSH validation failed unexpectedly"}
        ), 500
