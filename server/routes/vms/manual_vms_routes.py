import logging
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, request
from psycopg2 import sql

from utils.db.connection_pool import db_pool
from utils.web.limiter_ext import limiter
from utils.auth.stateless_auth import get_user_id_from_request
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


def _serialize_vm_row(row: tuple) -> Dict[str, Any]:
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
    }


@manual_vms_bp.route("/api/vms/manual", methods=["GET"])
@limiter.limit("30 per minute;200 per hour")
def list_manual_vms():
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User not authenticated"}), 401

    with db_pool.get_user_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SET myapp.current_user_id = %s;", (user_id,))
            conn.commit()
            cur.execute(
                """
                SELECT id, name, ip_address, port, ssh_jump_command, ssh_key_id, ssh_username, connection_verified, created_at, updated_at
                FROM user_manual_vms
                WHERE user_id = %s
                ORDER BY created_at DESC
                """,
                (user_id,),
            )
            rows = cur.fetchall()
    return jsonify({"vms": [_serialize_vm_row(r) for r in rows]})


@manual_vms_bp.route("/api/vms/manual", methods=["POST"])
@limiter.limit("20 per minute;100 per hour")
def create_manual_vm():
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User not authenticated"}), 401

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
            cur.execute("SET myapp.current_user_id = %s;", (user_id,))
            conn.commit()
            # Unique constraint is on (user_id, ip_address, port)
            # If same IP+port already exists, update the name and config
            # Reset connection_verified on upsert since credentials may have changed
            cur.execute(
                """
                INSERT INTO user_manual_vms (user_id, name, ip_address, port, ssh_jump_command, ssh_key_id, ssh_username, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (user_id, ip_address, port) DO UPDATE
                    SET name = EXCLUDED.name,
                        ssh_jump_command = EXCLUDED.ssh_jump_command,
                        ssh_key_id = EXCLUDED.ssh_key_id,
                        ssh_username = EXCLUDED.ssh_username,
                        connection_verified = FALSE,
                        updated_at = NOW()
                RETURNING id, name, ip_address, port, ssh_jump_command, ssh_key_id, ssh_username, connection_verified, created_at, updated_at;
                """,
                (
                    user_id,
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
def update_manual_vm(vm_id: int):
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User not authenticated"}), 401

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
    reset_connection_verified = False

    if name is not None:
        updates.append(sql.SQL("name = %s"))
        params.append(name)
    if ip_address is not None:
        updates.append(sql.SQL("ip_address = %s"))
        params.append(ip_address)
        reset_connection_verified = True
    if port_val is not None:
        updates.append(sql.SQL("port = %s"))
        params.append(port_val)
        reset_connection_verified = True
    if (
        ssh_jump_command is not None
        or "sshJumpCommand" in data
        or "ssh_jump_command" in data
    ):
        updates.append(sql.SQL("ssh_jump_command = %s"))
        params.append(ssh_jump_command)
    if ssh_key_id is not None or "sshKeyId" in data or "ssh_key_id" in data:
        updates.append(sql.SQL("ssh_key_id = %s"))
        params.append(ssh_key_id)
        reset_connection_verified = True
    if ssh_username is not None or "sshUsername" in data or "ssh_username" in data:
        updates.append(sql.SQL("ssh_username = %s"))
        params.append(ssh_username)
        reset_connection_verified = True

    if not updates:
        return jsonify({"error": "No fields to update"}), 400

    if reset_connection_verified:
        updates.append(sql.SQL("connection_verified = FALSE"))
    updates.append(sql.SQL("updated_at = NOW()"))

    with db_pool.get_user_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SET myapp.current_user_id = %s;", (user_id,))
            conn.commit()
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
def delete_manual_vm(vm_id: int):
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User not authenticated"}), 401

    with db_pool.get_user_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SET myapp.current_user_id = %s;", (user_id,))
            conn.commit()
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
def check_manual_vm_connection():
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User not authenticated"}), 401

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
    if vm_id is not None:
        try:
            vm_id = int(vm_id)
        except (TypeError, ValueError):
            return jsonify({"error": "vmId must be an integer"}), 400
        with db_pool.get_user_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SET myapp.current_user_id = %s;", (user_id,))
                conn.commit()
                cur.execute(
                    """
                    SELECT ip_address, port, ssh_jump_command, ssh_key_id, ssh_username
                    FROM user_manual_vms
                    WHERE id = %s AND user_id = %s
                    """,
                    (vm_id, user_id),
                )
                vm_row = cur.fetchone()
        if not vm_row:
            return jsonify({"error": "Manual VM not found"}), 404

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
        if vm_id is not None:
            try:
                with db_pool.get_user_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("SET myapp.current_user_id = %s;", (user_id,))
                        cur.execute(
                            "UPDATE user_manual_vms SET connection_verified = TRUE, updated_at = NOW() WHERE id = %s AND user_id = %s",
                            (vm_id, user_id),
                        )
                        conn.commit()
                logger.info(f"Connection verified for VM {vm_id}, user {user_id}")
            except Exception as db_exc:
                logger.error(
                    f"Failed to update connection_verified for VM {vm_id}: {db_exc}"
                )
        return jsonify({"success": True, "connectedAs": connected_as})
    except Exception as exc:
        logger.error("SSH validation failed unexpectedly: %s", exc, exc_info=True)
        return jsonify(
            {"success": False, "error": "SSH validation failed unexpectedly"}
        ), 500
