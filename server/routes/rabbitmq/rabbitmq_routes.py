import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, request

from connectors.rabbitmq_connector.client import RabbitMQAPIError, RabbitMQClient
from chat.backend.agent.tools.mcp_tools import clear_credentials_cache
from utils.auth.rbac_decorators import require_permission
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.log_sanitizer import hash_for_log, sanitize
from utils.secrets.secret_ref_utils import delete_user_secret

logger = logging.getLogger(__name__)

rabbitmq_bp = Blueprint("rabbitmq", __name__)


def _stored_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    try:
        data = get_token_data(user_id, "rabbitmq")
        return data or None
    except Exception:
        logger.exception("[RABBITMQ] Failed to retrieve credentials")
        return None


def _clean_username(raw: Any) -> str:
    username = raw.strip() if isinstance(raw, str) else ""
    if not username or len(username) > 200 or any(ord(ch) < 32 for ch in username):
        raise ValueError("Username is required")
    return username


def _clean_password(raw: Any) -> str:
    # Do not strip: leading or trailing spaces can be part of the secret.
    if not isinstance(raw, str) or not raw or len(raw) > 500:
        raise ValueError("Password is required")
    return raw


def _clean_vhost(raw: Any) -> str:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return "/"
    if not isinstance(raw, str):
        raise ValueError("Vhost must be a string")
    vhost = raw.strip()
    if len(vhost) > 255 or any(ord(ch) < 32 for ch in vhost):
        raise ValueError("Vhost is invalid")
    return vhost


def _command_token(value: str) -> bool:
    return bool(value) and len(value) <= 200 and all(ch.isalnum() or ch in "/._-@" for ch in value)


def _read_permission_command(vhost: str, username: str) -> str:
    # A quote or space in the name would make the copied command wrong.
    target = vhost if _command_token(vhost) else "<vhost>"
    user = username if _command_token(username) else "<username>"
    return f'rabbitmqctl set_permissions -p {target} {user} "^$" "^$" ".*"'


def _public_connect_error(kind: str, vhost: str = "/", username: str = "") -> str:
    """User-facing connect failure. The vhost command is filled in when it is safe to copy."""
    if kind == "auth":
        return "RabbitMQ rejected the username or password"
    target = vhost if _command_token(vhost) else "<vhost>"
    grant = (
        f"In the management UI, set permission on vhost {target}: configure ^$, write ^$, read .*. "
        f"Or on the broker host run {_read_permission_command(vhost, username)}"
    )
    if kind == "vhost":
        return (
            "This user can sign in, but cannot read that vhost. The monitoring tag does not grant access. "
            + grant
        )
    if kind == "not_found":
        # RabbitMQ 4.x answers 404 for a vhost the user has no permission on, same as a missing one.
        return f"RabbitMQ vhost {target} was not found, or this user has no read permission on it. " + grant
    if kind == "forbidden":
        return "This RabbitMQ user cannot read the management API. Grant the monitoring tag."
    if kind == "tls":
        return "TLS verification failed. Check RABBITMQ_SSL_VERIFY."
    if kind == "timeout":
        return "Timed out reaching the RabbitMQ management API"
    return "Unable to reach the RabbitMQ management API"


def _client_from_payload(payload: Dict[str, Any]) -> RabbitMQClient:
    url = payload.get("url")
    # A number or object would raise AttributeError inside the URL parser and become a 500.
    if url is not None and not isinstance(url, str):
        raise ValueError("Management URL must be a string")
    return RabbitMQClient(
        url=url or "",
        username=_clean_username(payload.get("username")),
        password=_clean_password(payload.get("password")),
    )


@rabbitmq_bp.route("/connect", methods=["POST"])
@require_permission("connectors", "write")
def connect(user_id):
    payload = request.get_json(force=True, silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Expected a JSON object"}), 400

    try:
        vhost = _clean_vhost(payload.get("vhost"))
        client = _client_from_payload(payload)
    except ValueError:
        logger.warning("[RABBITMQ] Rejected connection details")
        return jsonify({"error": "Invalid RabbitMQ connection details"}), 400

    logger.info("[RABBITMQ] Connecting user %s to %s", hash_for_log(user_id), sanitize(client.url))

    try:
        who = client.whoami()
        overview = client.overview()
        # whoami succeeds for a monitoring user with no permissions. Listing the vhost does not.
        client.queues(vhost=vhost, page=1, page_size=1)
    except RabbitMQAPIError as exc:
        logger.warning("[RABBITMQ] Connect failed")
        return jsonify({"error": _public_connect_error(exc.kind, vhost=vhost, username=client.username)}), 400
    finally:
        client.close()

    token_payload = {
        "url": client.url,
        "username": client.username,
        "password": payload.get("password"),
        "vhost": vhost,
        "cluster_name": overview.get("cluster_name"),
        "rabbitmq_version": overview.get("rabbitmq_version"),
        "tags": who.get("tags") or [],
        "validated_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        store_tokens_in_db(user_id, token_payload, "rabbitmq")
    except Exception:
        logger.exception("[RABBITMQ] Failed to store credentials")
        return jsonify({"error": "Failed to store RabbitMQ credentials"}), 500

    clear_credentials_cache(user_id)
    return jsonify({
        "success": True,
        "url": client.url,
        "username": who.get("name") or client.username,
        "vhost": vhost,
        "clusterName": overview.get("cluster_name"),
        "version": overview.get("rabbitmq_version"),
        "tags": who.get("tags") or [],
    })


@rabbitmq_bp.route("/status", methods=["GET"])
@require_permission("connectors", "read")
def status(user_id):
    creds = _stored_credentials(user_id)
    if not creds or not creds.get("url") or not creds.get("username") or not creds.get("password"):
        return jsonify({"connected": False})

    try:
        client = RabbitMQClient(
            url=creds["url"],
            username=creds["username"],
            password=creds["password"],
        )
    except ValueError:
        logger.warning("[RABBITMQ] Stored management URL is not usable")
        return jsonify({"connected": False, "error": "Failed to validate RabbitMQ connection"})

    vhost = creds.get("vhost") if isinstance(creds.get("vhost"), str) and creds.get("vhost") else "/"
    access_error = None
    try:
        # whoami only proves the password. The vhost list proves this user can see queues.
        who = client.whoami()
        try:
            client.queues(vhost=vhost, page=1, page_size=1)
        except RabbitMQAPIError as exc:
            # Keep the saved connection so the page can show the permission gap.
            if exc.kind not in ("vhost", "not_found"):
                raise
            access_error = _public_connect_error(
                exc.kind,
                vhost=vhost,
                username=creds.get("username") if isinstance(creds.get("username"), str) else "",
            )
    except (RabbitMQAPIError, ValueError):
        logger.warning("[RABBITMQ] Status validation failed")
        return jsonify({"connected": False, "error": "Failed to validate RabbitMQ connection"})
    finally:
        client.close()

    body = {
        "connected": True,
        "url": creds.get("url"),
        "username": who.get("name") or creds.get("username"),
        "vhost": vhost,
        "clusterName": creds.get("cluster_name"),
        "version": creds.get("rabbitmq_version"),
        "tags": who.get("tags") or [],
        "validatedAt": creds.get("validated_at"),
    }
    if access_error:
        body["error"] = access_error
    return jsonify(body)


@rabbitmq_bp.route("/disconnect", methods=["DELETE", "POST"])
@require_permission("connectors", "write")
def disconnect(user_id):
    try:
        vault_ok, rows = delete_user_secret(user_id, "rabbitmq")
        # (False, 0) means cleanup failed before the row was removed, so the credential is still live.
        if not vault_ok and rows == 0:
            logger.error("[RABBITMQ] Disconnect failed for user %s", hash_for_log(user_id))
            return jsonify({"error": "Failed to disconnect RabbitMQ"}), 500
        # The row is gone. A leftover Vault object is no longer reachable from the app.
        if not vault_ok:
            logger.warning("[RABBITMQ] Disconnected user %s but Vault delete failed", hash_for_log(user_id))
        clear_credentials_cache(user_id)
        return jsonify({
            "success": True,
            "message": "RabbitMQ disconnected successfully",
            "tokensDeleted": rows,
        })
    except Exception:
        logger.exception("[RABBITMQ] Failed to disconnect")
        return jsonify({"error": "Failed to disconnect RabbitMQ"}), 500
