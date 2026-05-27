"""GCP service-account management routes (multi-SA per user).

Each row in ``user_connections`` for provider='gcp' represents one connected
service account, keyed on the SA's client_email. Visibility (private | org)
controls whether org-mates can use the same credential.
"""

import json
import logging
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

from flask import Blueprint, jsonify, request
from google.oauth2 import service_account as google_sa
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from utils.auth.rbac_decorators import require_permission
from utils.db.connection_utils import (
    deactivate_all_connections,
    deactivate_connection,
    get_all_user_connections,
    save_connection_metadata,
)
from utils.log_sanitizer import hash_for_log
from utils.secrets.secret_ref_utils import (
    delete_connection_secret,
    store_connection_secret,
)

logger = logging.getLogger(__name__)

gcp_service_accounts_bp = Blueprint("gcp_service_accounts", __name__)

PROVIDER = "gcp"
READ_ONLY_SCOPE = "https://www.googleapis.com/auth/cloud-platform.read-only"
_LOG_PREFIX = "[GCP-SA]"


# ── Helpers ────────────────────────────────────────────────────────────


def _strip_secret_ref(conn_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of the connection dict without ``secret_ref``."""
    out = dict(conn_dict)
    out.pop("secret_ref", None)
    return out


def _build_credentials(sa_info: Dict[str, Any]):
    """Build read-only SA credentials. Caller handles exceptions.

    Routes through the same unwrap helper as the runtime credential loader so
    legacy wrapper-shape blobs (``{service_account_json, client_email, project_id}``)
    also work — necessary for reconnect to succeed on rows written by an
    earlier iteration of this route.
    """
    from connectors.gcp_connector.auth.multi_sa import _unwrap_sa_info
    return google_sa.Credentials.from_service_account_info(
        _unwrap_sa_info(sa_info), scopes=[READ_ONLY_SCOPE]
    )


def _verify_and_enumerate(
    sa_info: Dict[str, Any], project_id: str
) -> Tuple[bool, List[str], Optional[str]]:
    """Verify SA against ``project_id`` and enumerate accessible projects.

    Returns ``(ok, accessible_project_ids, error_message)``. Enumeration falls
    back to ``[project_id]`` if the broader list call fails (common when the SA
    only has project-level perms).
    """
    try:
        creds = _build_credentials(sa_info)
    except Exception as exc:
        return False, [], f"Invalid service account JSON: {type(exc).__name__}"

    try:
        crm = build(
            "cloudresourcemanager",
            "v1",
            credentials=creds,
            cache_discovery=False,
        )
    except Exception as exc:
        return False, [], f"Failed to build GCP client: {type(exc).__name__}"

    # 1. Verify against the home project (cheap, definitive)
    try:
        crm.projects().get(projectId=project_id).execute()
    except HttpError as exc:
        status = getattr(exc, "status_code", None) or getattr(getattr(exc, "resp", None), "status", None)
        return False, [], f"Service account cannot access project (HTTP {status})"
    except Exception as exc:
        return False, [], f"Verification failed: {type(exc).__name__}"

    # 2. Best-effort enumeration of all accessible projects
    accessible: List[str] = []
    try:
        resp = crm.projects().list().execute()
        for proj in resp.get("projects") or []:
            pid = proj.get("projectId")
            if not pid:
                continue
            if (proj.get("lifecycleState") or "ACTIVE") != "ACTIVE":
                continue
            accessible.append(pid)
    except Exception as exc:
        logger.info(
            "%s projects.list failed for project=%s (%s) — falling back to home project",
            _LOG_PREFIX,
            hash_for_log(project_id),
            type(exc).__name__,
        )

    if project_id not in accessible:
        accessible.append(project_id)

    return True, accessible, None


# ── Routes ─────────────────────────────────────────────────────────────


@gcp_service_accounts_bp.route("/api/gcp/service-accounts", methods=["GET"])
@require_permission("connectors", "read")
def list_service_accounts(user_id):
    """Return active GCP service-account connections for the user.

    Inactive rows are intentionally excluded: the disconnect flow deletes the
    Vault secret, so an inactive row cannot be re-activated without re-uploading
    the SA key — surfacing it as "reconnectable" would mislead the user.
    """
    rows = get_all_user_connections(user_id, PROVIDER)
    return jsonify({"service_accounts": [_strip_secret_ref(r) for r in rows]}), 200


@gcp_service_accounts_bp.route("/api/gcp/service-accounts", methods=["POST"])
@require_permission("connectors", "write")
def add_service_account(user_id):
    """Verify and store a new GCP service-account credential."""
    body = request.get_json(silent=True) or {}
    sa_raw = body.get("service_account_json")
    alias = body.get("alias")
    visibility = body.get("visibility") or "private"

    if visibility not in ("private", "org"):
        return jsonify({"error": "visibility must be 'private' or 'org'"}), 400

    if not sa_raw:
        return jsonify({"error": "service_account_json is required"}), 400

    # Accept either a dict or a JSON string
    if isinstance(sa_raw, str):
        try:
            sa_info = json.loads(sa_raw)
        except json.JSONDecodeError:
            return jsonify({"error": "service_account_json is not valid JSON"}), 400
    elif isinstance(sa_raw, dict):
        sa_info = sa_raw
    else:
        return jsonify({"error": "service_account_json must be JSON object or string"}), 400

    client_email = sa_info.get("client_email")
    project_id = sa_info.get("project_id")
    if not client_email or not project_id:
        return jsonify(
            {"error": "service_account_json must contain client_email and project_id"}
        ), 400

    if alias is not None and not isinstance(alias, str):
        return jsonify({"error": "alias must be a string"}), 400
    if alias:
        alias = alias.strip()[:120] or None

    ok, accessible, err = _verify_and_enumerate(sa_info, project_id)
    if not ok:
        logger.warning(
            "%s SA verification failed user=%s project=%s: %s",
            _LOG_PREFIX,
            hash_for_log(user_id),
            hash_for_log(project_id),
            err,
        )
        return jsonify({"error": err or "Service account verification failed"}), 400

    secret_ref = store_connection_secret(user_id, PROVIDER, client_email, sa_info)
    if not secret_ref:
        return jsonify({"error": "Failed to store credentials"}), 500

    saved = save_connection_metadata(
        user_id,
        PROVIDER,
        client_email,
        account_alias=alias,
        project_id=project_id,
        accessible_project_ids=accessible,
        visibility=visibility,
        secret_ref=secret_ref,
        connection_method="service_account",
        status="active",
    )
    if not saved:
        # Best-effort cleanup of the orphaned secret
        delete_connection_secret(secret_ref)
        return jsonify({"error": "Failed to save connection metadata"}), 500

    logger.info(
        "%s SA connected user=%s account=%s project=%s accessible=%d visibility=%s",
        _LOG_PREFIX,
        hash_for_log(user_id),
        hash_for_log(client_email),
        hash_for_log(project_id),
        len(accessible),
        visibility,
    )

    return jsonify(
        _strip_secret_ref(
            {
                "account_id": client_email,
                "account_alias": alias,
                "project_id": project_id,
                "accessible_project_ids": accessible,
                "visibility": visibility,
                "status": "active",
            }
        )
    ), 201


@gcp_service_accounts_bp.route(
    "/api/gcp/service-accounts/<path:sa_email>", methods=["DELETE"]
)
@require_permission("connectors", "write")
def delete_service_account(user_id, sa_email):
    """Deactivate a single SA connection and delete its Vault secret."""
    sa_email = unquote(sa_email or "").strip()
    if not sa_email:
        return jsonify({"error": "service account email required"}), 400

    ok, secret_ref = deactivate_connection(user_id, PROVIDER, sa_email)
    if not ok:
        return jsonify({"error": "Service account not found or already inactive"}), 404

    if secret_ref:
        try:
            delete_connection_secret(secret_ref)
        except Exception as exc:
            logger.warning(
                "%s Secret cleanup failed user=%s account=%s: %s",
                _LOG_PREFIX,
                hash_for_log(user_id),
                hash_for_log(sa_email),
                type(exc).__name__,
            )

    logger.info(
        "%s SA disconnected user=%s account=%s",
        _LOG_PREFIX,
        hash_for_log(user_id),
        hash_for_log(sa_email),
    )
    return jsonify({"success": True}), 200


@gcp_service_accounts_bp.route(
    "/api/gcp/service-accounts/disconnect-all", methods=["POST"]
)
@require_permission("connectors", "write")
def disconnect_all_service_accounts(user_id):
    """Deactivate every GCP SA for this user and delete each Vault secret."""
    ok, refs = deactivate_all_connections(user_id, PROVIDER)
    if not ok:
        return jsonify({"error": "Failed to disconnect service accounts"}), 500

    for ref in refs:
        if not ref:
            continue
        try:
            delete_connection_secret(ref)
        except Exception as exc:
            logger.warning(
                "%s Bulk secret cleanup failed user=%s: %s",
                _LOG_PREFIX,
                hash_for_log(user_id),
                type(exc).__name__,
            )

    logger.info(
        "%s All SAs disconnected user=%s count=%d",
        _LOG_PREFIX,
        hash_for_log(user_id),
        len(refs),
    )
    return jsonify({"success": True, "disconnected": len(refs)}), 200
