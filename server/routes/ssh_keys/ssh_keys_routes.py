import logging
from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, request
from psycopg2.extras import Json

from utils.db.connection_pool import db_pool
from utils.web.limiter_ext import limiter
from utils.secrets.secret_ref_utils import delete_user_secret
from utils.ssh.ssh_key_utils import (
    _get_single_key,
    _parse_token_data,
    _serialize_key_row,
    build_ssh_provider_name,
    generate_ssh_key_pair
)
from utils.auth.stateless_auth import get_user_id_from_request
from utils.auth.token_management import store_tokens_in_db

ssh_keys_bp = Blueprint("ssh_keys_bp", __name__)
logger = logging.getLogger(__name__)






def _list_user_keys(user_id: str) -> List[Dict[str, Any]]:
    """Fetch keys for a user using RLS-aware connection."""
    with db_pool.get_user_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SET myapp.current_user_id = %s;", (user_id,))
            conn.commit()
            cur.execute(
                """
                SELECT id, provider, token_data, secret_ref, timestamp
                FROM user_tokens
                WHERE user_id = %s
                  AND provider LIKE 'aurora_ssh%%'
                  AND is_active = TRUE
                ORDER BY timestamp DESC;
                """,
                (user_id,),
            )
            rows = cur.fetchall()
    return [_serialize_key_row(user_id, row) for row in rows]




def _load_raw_key_row(user_id: str, key_id: int) -> Optional[tuple]:
    with db_pool.get_user_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SET myapp.current_user_id = %s;", (user_id,))
            conn.commit()
            cur.execute(
                """
                SELECT id, provider, token_data, secret_ref, timestamp
                FROM user_tokens
                WHERE user_id = %s
                  AND id = %s
                  AND provider LIKE 'aurora_ssh%%'
                  AND is_active = TRUE
                LIMIT 1;
                """,
                (user_id, key_id),
            )
            return cur.fetchone()


@ssh_keys_bp.route("/api/ssh-keys", methods=["POST"])
@limiter.limit("10 per minute;40 per hour")
def create_ssh_key():
    """Generate and store a new Aurora-managed SSH keypair."""
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User not authenticated"}), 401

    try:
        private_key, public_key = generate_ssh_key_pair()
        provider = build_ssh_provider_name()

        # Store private key + metadata in Vault via existing token storage helper
        secret_payload = {"private_key": private_key, "public_key": public_key}
        store_tokens_in_db(user_id, secret_payload, provider)

        # Store public key in DB token_data for quick listing (safe to keep outside Vault)
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE user_tokens
                    SET token_data = %s,
                        timestamp = CURRENT_TIMESTAMP,
                        last_activity = CURRENT_TIMESTAMP
                    WHERE user_id = %s AND provider = %s
                    RETURNING id, timestamp;
                    """,
                    (Json({"public_key": public_key, "label": "Aurora SSH Key"}), user_id, provider),
                )
                db_row = cur.fetchone()
                conn.commit()

        key_id, created_at = db_row if db_row else (None, None)
        if key_id is None:
            # Fallback lookup in unlikely event the UPDATE didn't return a row
            with db_pool.get_user_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SET myapp.current_user_id = %s;", (user_id,))
                    conn.commit()
                    cur.execute(
                        """
                        SELECT id, timestamp
                        FROM user_tokens
                        WHERE user_id = %s AND provider = %s
                        LIMIT 1;
                        """,
                        (user_id, provider),
                    )
                    fallback_row = cur.fetchone()
                    if fallback_row:
                        key_id, created_at = fallback_row

        return (
            jsonify(
                {
                    "id": key_id,
                    "provider": provider,
                    "label": provider,
                    "publicKey": public_key,
                    "createdAt": created_at.isoformat() if created_at else None,
                }
            ),
            201,
        )
    except Exception as exc:
        logger.error("Failed to create SSH key for user %s: %s", user_id, exc, exc_info=True)
        return jsonify({"error": "Failed to create SSH key"}), 500


@ssh_keys_bp.route("/api/ssh-keys", methods=["GET"])
@limiter.limit("30 per minute;200 per hour")
def list_ssh_keys():
    """Return public metadata for all Aurora-managed SSH keys for the user."""
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User not authenticated"}), 401

    try:
        return jsonify({"keys": _list_user_keys(user_id)})
    except Exception as exc:
        logger.error("Failed to list SSH keys for user %s: %s", user_id, exc, exc_info=True)
        return jsonify({"error": "Failed to list SSH keys"}), 500


@ssh_keys_bp.route("/api/ssh-keys/<int:key_id>", methods=["GET"])
@limiter.limit("30 per minute;200 per hour")
def get_ssh_key(key_id: int):
    """Fetch a single SSH key's public material."""
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User not authenticated"}), 401

    try:
        key_record = _get_single_key(user_id, key_id)
        if not key_record:
            return jsonify({"error": "SSH key not found"}), 404
        return jsonify(key_record)
    except Exception as exc:
        logger.error("Failed to load SSH key %s for user %s: %s", key_id, user_id, exc, exc_info=True)
        return jsonify({"error": "Failed to load SSH key"}), 500


@ssh_keys_bp.route("/api/ssh-keys/<int:key_id>", methods=["PATCH"])
@limiter.limit("10 per minute;40 per hour")
def rename_ssh_key(key_id: int):
    """Rename a managed SSH key label."""
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User not authenticated"}), 401

    data = request.get_json() or {}
    new_label = (data.get("label") or "").strip()
    if not new_label:
        return jsonify({"error": "Label is required"}), 400

    row = _load_raw_key_row(user_id, key_id)
    if not row:
        return jsonify({"error": "SSH key not found"}), 404

    token_payload = _parse_token_data(row[2])
    token_payload["label"] = new_label

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE user_tokens
                    SET token_data = %s,
                        timestamp = CURRENT_TIMESTAMP,
                        last_activity = CURRENT_TIMESTAMP
                    WHERE id = %s
                    RETURNING id, provider, token_data, secret_ref, timestamp;
                    """,
                    (Json(token_payload), key_id),
                )
                updated_row = cur.fetchone()
                conn.commit()

        if not updated_row:
            return jsonify({"error": "Failed to rename SSH key"}), 500

        return jsonify(_serialize_key_row(user_id, updated_row))
    except Exception as exc:
        logger.error("Failed to rename SSH key %s for user %s: %s", key_id, user_id, exc, exc_info=True)
        return jsonify({"error": "Failed to rename SSH key"}), 500


@ssh_keys_bp.route("/api/ssh-keys/<int:key_id>", methods=["DELETE"])
@limiter.limit("10 per minute;40 per hour")
def delete_ssh_key(key_id: int):
    """Delete a managed SSH keypair (DB row + Vault entry)."""
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User not authenticated"}), 401

    try:
        key_record = _get_single_key(user_id, key_id)
        if not key_record:
            return jsonify({"error": "SSH key not found"}), 404

        # provider == SSH key identifier (for example: "aurora_ssh_<uuid>")
        provider = key_record["provider"]
        secret_deleted, rows_deleted = delete_user_secret(user_id, provider)
        if rows_deleted == 0:
            return jsonify({"error": "Failed to delete SSH key"}), 500

        return jsonify(
            {
                "deleted": True,
                "secretDeleted": secret_deleted,
            }
        )
    except Exception as exc:
        logger.error("Failed to delete SSH key %s for user %s: %s", key_id, user_id, exc, exc_info=True)
        return jsonify({"error": "Failed to delete SSH key"}), 500
