import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint, jsonify, request

from connectors.rabbitmq_connector.client import RabbitMQAPIError, RabbitMQClient
from chat.backend.agent.tools.mcp_tools import clear_credentials_cache
from utils.auth.rbac_decorators import require_permission
from utils.log_sanitizer import hash_for_log, sanitize
from utils.rabbitmq_accounts import (
    account_allowed,
    account_id_for_url,
    account_summary,
    clean_name,
    find_account,
    list_rabbitmq_accounts,
    name_conflict,
    retire_rabbitmq_connections,
    store_rabbitmq_accounts,
    sync_rabbitmq_connections,
    upsert_account,
)
from utils.secrets.secret_ref_utils import delete_user_secret

logger = logging.getLogger(__name__)

rabbitmq_bp = Blueprint("rabbitmq", __name__)

_VALIDATION_FAILED = "Failed to validate RabbitMQ connection"


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




def _status_summary(user_id: str, account: Dict[str, Any]) -> Dict[str, Any]:
    """Live check of one broker. A failure stays on that row so the others remain usable."""
    summary = account_summary(account)
    summary["allowed"] = account_allowed(user_id, account["account_id"])
    vhost = account.get("vhost") or "/"
    try:
        client = RabbitMQClient(url=account["url"], username=account["username"], password=account["password"])
    except ValueError:
        logger.warning("[RABBITMQ] Stored management URL is not usable")
        summary["valid"] = False
        summary["error"] = _VALIDATION_FAILED
        return summary
    try:
        # whoami only proves the password. The vhost list proves this user can see queues.
        who = client.whoami()
        try:
            client.queues(vhost=vhost, page=1, page_size=1)
        except RabbitMQAPIError as exc:
            # Keep the saved connection so the page can show the permission gap.
            if exc.kind not in ("vhost", "not_found"):
                raise
            summary["error"] = _public_connect_error(exc.kind, vhost=vhost, username=account["username"])
    except (RabbitMQAPIError, ValueError):
        # The account dict carries the password, so nothing from it is logged.
        logger.warning("[RABBITMQ] Status validation failed")
        summary["valid"] = False
        summary["error"] = _VALIDATION_FAILED
        return summary
    finally:
        client.close()
    summary["valid"] = True
    summary["username"] = who.get("name") or account["username"]
    summary["tags"] = who.get("tags") or summary.get("tags") or []
    return summary


def _payload(summaries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Every broker, plus the first one's fields at the top level for older readers."""
    if not summaries:
        return {"connected": False, "accounts": []}
    primary = summaries[0]
    return {
        "connected": True,
        "url": primary.get("url"),
        "username": primary.get("username"),
        "vhost": primary.get("vhost"),
        "clusterName": primary.get("clusterName"),
        "version": primary.get("version"),
        "tags": primary.get("tags") or [],
        "validatedAt": primary.get("validatedAt"),
        "accounts": summaries,
    }


def _stored_payload(user_id: str, accounts: list, validated_id: Optional[str] = None) -> Dict[str, Any]:
    """Response from stored rows. Skips the live check so connect stays within one validation."""
    summaries = []
    for account in accounts:
        summary = account_summary(account)
        summary["allowed"] = account_allowed(user_id, account["account_id"])
        # Other brokers were not rechecked, so leave their validity unknown.
        if validated_id is not None and account["account_id"] == validated_id:
            summary["valid"] = True
        summaries.append(summary)
    return _payload(summaries)


@rabbitmq_bp.route("/connect", methods=["POST"])
@require_permission("connectors", "write")
def connect(user_id):
    """Store a broker. A new management host is added; the same host replaces its credentials."""
    payload = request.get_json(force=True, silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Expected a JSON object"}), 400

    try:
        vhost = _clean_vhost(payload.get("vhost"))
        name = clean_name(payload.get("name"))
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

    account_id = account_id_for_url(client.url)
    if not account_id:
        return jsonify({"error": "Invalid RabbitMQ connection details"}), 400

    incoming = {
        "account_id": account_id,
        "name": name,
        "url": client.url,
        "username": client.username,
        "password": payload.get("password"),
        "vhost": vhost,
        "cluster_name": overview.get("cluster_name"),
        "rabbitmq_version": overview.get("rabbitmq_version"),
        "tags": who.get("tags") or [],
        "validated_at": datetime.now(timezone.utc).isoformat(),
    }

    # A Vault error looks like "nothing stored". Saving then would drop the other brokers.
    try:
        existing = list_rabbitmq_accounts(user_id, raise_on_error=True)
    except Exception:
        logger.exception("[RABBITMQ] Failed to load existing brokers for user %s", hash_for_log(user_id))
        return jsonify({"error": "Failed to load existing RabbitMQ connections"}), 500

    if name_conflict(existing, name, account_id):
        return jsonify({"error": "Another RabbitMQ connection already uses that name"}), 409

    accounts, replaced = upsert_account(existing, incoming)

    try:
        store_rabbitmq_accounts(user_id, accounts, schedule_prediscovery=True)
    except Exception:
        logger.exception("[RABBITMQ] Failed to store credentials")
        return jsonify({"error": "Failed to store RabbitMQ credentials"}), 500

    logger.info(
        "[RABBITMQ] Stored brokers for user %s (count=%d, replaced=%s)",
        hash_for_log(user_id),
        len(accounts),
        replaced,
    )
    clear_credentials_cache(user_id)
    body = _stored_payload(user_id, accounts, validated_id=account_id)
    body["success"] = True
    body["replaced"] = replaced
    body["accountId"] = account_id
    # The toast names the broker just connected, not the primary.
    body["connectedAccount"] = next(
        (s for s in body["accounts"] if s.get("accountId") == account_id), None
    )
    return jsonify(body)


@rabbitmq_bp.route("/status", methods=["GET"])
@require_permission("connectors", "read")
def status(user_id):
    """Check every connected broker. One dead password does not hide the others."""
    accounts = list_rabbitmq_accounts(user_id)
    if not accounts:
        return jsonify({"connected": False, "accounts": []})

    # Backfill connection rows for a broker saved before brokers were tracked.
    try:
        sync_rabbitmq_connections(user_id, accounts)
    except Exception:
        logger.warning("[RABBITMQ] Could not sync connection rows for user %s", hash_for_log(user_id))

    # One slow broker must not stack timeouts in front of the others.
    workers = min(4, len(accounts))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        summaries = list(pool.map(lambda account: _status_summary(user_id, account), accounts))

    body = _payload(summaries)
    if not any(item.get("valid") for item in summaries):
        # Every stored password failed — still list them so they can be removed.
        body["error"] = _VALIDATION_FAILED
    elif len(summaries) == 1 and summaries[0].get("error"):
        # One broker keeps the top-level permission hint the page already shows.
        body["error"] = summaries[0]["error"]
    return jsonify(body)


_BAD_SELECTOR = "Invalid RabbitMQ broker selector. Omit it to disconnect every broker."


def _selector_from_body() -> Tuple[Optional[str], bool]:
    """(selector, valid). A selector that is present must be a non-blank string."""
    data = request.get_json(silent=True)
    if data is None:
        # An empty body means "no selector". A body that isn't JSON is a mistake.
        return None, not request.get_data()
    if not isinstance(data, dict):
        return None, False
    for key in ("accountId", "account"):
        if key in data:
            selector = data[key]
            if not isinstance(selector, str) or not selector.strip():
                return None, False
            return selector, True
    return None, True


@rabbitmq_bp.route("/disconnect", methods=["POST"])
@require_permission("connectors", "write")
def disconnect_post(user_id):
    # The broker name belongs in the body, not the query string.
    selector, valid = _selector_from_body()
    if not valid:
        return jsonify({"error": _BAD_SELECTOR}), 400
    return _disconnect(user_id, selector)


@rabbitmq_bp.route("/disconnect", methods=["DELETE"])
@require_permission("connectors", "write")
def disconnect(user_id):
    selector = request.args.get("account")
    # "?account=" is a mistake, not a request to remove every broker.
    if selector is not None and not selector.strip():
        return jsonify({"error": _BAD_SELECTOR}), 400
    return _disconnect(user_id, selector)


def _disconnect(user_id, selector: Optional[str]):
    """Remove one broker, or every broker when none is named."""
    try:
        # A failed read is not "no brokers" — that path deletes every broker.
        accounts = list_rabbitmq_accounts(user_id, raise_on_error=True)
        if selector and str(selector).strip():
            match = find_account(accounts, str(selector))
            if match is None:
                known = ", ".join(a["account_id"] for a in accounts) or "none"
                return jsonify({"error": f"Unknown RabbitMQ broker. Connected: {known}"}), 404
            remaining = [a for a in accounts if a["account_id"] != match["account_id"]]
            # Other brokers stay stored. Only this one's connection row is retired.
            if remaining:
                # Removing a broker is not a new connection: never schedule prediscovery.
                store_rabbitmq_accounts(user_id, remaining, schedule_prediscovery=False)
                clear_credentials_cache(user_id)
                logger.info(
                    "[RABBITMQ] Removed one broker for user %s (%d remaining)",
                    hash_for_log(user_id),
                    len(remaining),
                )
                body = _stored_payload(user_id, remaining)
                body["success"] = True
                body["message"] = "RabbitMQ connection removed"
                return jsonify(body)

        retire_rabbitmq_connections(user_id)
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
            "connected": False,
            "accounts": [],
        })
    except Exception:
        logger.exception("[RABBITMQ] Failed to disconnect")
        return jsonify({"error": "Failed to disconnect RabbitMQ"}), 500
