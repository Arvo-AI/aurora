"""SharePoint connector routes for auth, status, search, and content operations."""

import logging
import secrets
import time
from html import escape as html_escape
from typing import Any, Dict, Optional

import requests
from flask import Blueprint, Response, jsonify, request

from connectors.sharepoint_connector.auth import (
    exchange_code_for_token,
    get_auth_url,
    refresh_access_token,
)
from connectors.sharepoint_connector.client import SharePointClient
from connectors.sharepoint_connector.search_service import SharePointSearchService
from utils.db.connection_pool import db_pool
from utils.auth.oauth2_state_cache import retrieve_oauth2_state, store_oauth2_state
from utils.auth.stateless_auth import get_user_id_from_request, set_rls_context
from utils.auth.rbac_decorators import require_permission
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

sharepoint_bp = Blueprint("sharepoint", __name__)
_AUTH_REQUIRED_MSG = "User authentication required"


@sharepoint_bp.after_request
def _set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _get_request_body() -> Dict[str, Any]:
    """Safely parse the JSON request body."""
    try:
        return request.get_json(force=True, silent=True) or {}
    except Exception:
        return {}


def _safe_json_response(data: Any, status: int = 200) -> Response:
    """Return a JSON response with explicit content-type to prevent MIME sniffing."""
    import json as _json
    body = _json.dumps(data)
    return Response(body, status=status, content_type="application/json")


def _require_user_id():
    """Return (user_id, None) or (None, error_response)."""
    uid = get_user_id_from_request()
    if not uid:
        return None, (jsonify({"error": _AUTH_REQUIRED_MSG}), 401)
    return uid, None


def _require_access_token(user_id: str):
    """Return (access_token, creds, None) or (None, None, error_response)."""
    creds = _get_stored_sharepoint_credentials(user_id)
    if not creds:
        return None, None, (jsonify({"error": "SharePoint not connected"}), 404)
    token = creds.get("access_token")
    if not token:
        return None, None, (jsonify({"error": "SharePoint credentials missing"}), 400)
    return token, creds, None


def _handle_http_error(exc: requests.HTTPError, user_id: str, label: str):
    """Return an error response for a SharePoint HTTPError (no retry)."""
    status_code = exc.response.status_code if exc.response is not None else None
    if status_code == 401:
        return jsonify({"error": "SharePoint credentials expired"}), 401
    logger.exception("[SHAREPOINT] %s failed for user %s", label, sanitize(user_id))
    return jsonify({"error": f"Failed to {label.lower()}"}), 502


def _get_stored_sharepoint_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve stored SharePoint credentials for user."""
    try:
        return get_token_data(user_id, "sharepoint")
    except Exception as exc:
        logger.error("Failed to retrieve SharePoint credentials for user %s: %s", sanitize(user_id), exc)
        return None


def _refresh_sharepoint_credentials(user_id: str, creds: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Attempt to refresh SharePoint OAuth credentials."""
    refresh_token = creds.get("refresh_token")
    if not refresh_token:
        return None

    try:
        token_data = refresh_access_token(refresh_token)
    except Exception as exc:
        logger.warning("[SHAREPOINT] OAuth refresh failed for user %s: %s", sanitize(user_id), exc)
        return None

    access_token = token_data.get("access_token")
    if not access_token:
        return None

    updated_creds = dict(creds)
    updated_creds["access_token"] = access_token
    updated_refresh = token_data.get("refresh_token")
    if updated_refresh:
        updated_creds["refresh_token"] = updated_refresh

    expires_in = token_data.get("expires_in")
    if expires_in:
        updated_creds["expires_in"] = expires_in
        updated_creds["expires_at"] = int(time.time()) + int(expires_in)

    store_tokens_in_db(user_id, updated_creds, "sharepoint")
    return updated_creds


@sharepoint_bp.route("/connect", methods=["POST"])
@require_permission("connectors", "write")
def connect(user_id):
    """Connect SharePoint via Microsoft OAuth2."""
    data = _get_request_body()

    code = data.get("code")
    if not code:
        state = secrets.token_urlsafe(32)
        store_oauth2_state(state, user_id, "sharepoint")
        auth_url = get_auth_url(state=state)
        return jsonify({"authUrl": auth_url})

    return _exchange_oauth_code(user_id, data, code)


def _exchange_oauth_code(user_id: str, data: dict, code: str):
    """Validate OAuth state, exchange code, store token, and return profile."""
    state = data.get("state")
    if not state:
        return jsonify({"error": "Missing OAuth state parameter"}), 400

    state_data = retrieve_oauth2_state(state)
    if not state_data:
        return jsonify({"error": "Invalid or expired OAuth state"}), 400
    if state_data.get("user_id") != user_id or state_data.get("endpoint") != "sharepoint":
        logger.warning("[SHAREPOINT] OAuth state mismatch for user %s", sanitize(user_id))
        return jsonify({"error": "OAuth state mismatch"}), 400

    try:
        token_data = exchange_code_for_token(code)
    except Exception as exc:
        logger.error("[SHAREPOINT] OAuth token exchange failed for user %s: %s", sanitize(user_id), exc)
        return jsonify({"error": "SharePoint OAuth token exchange failed"}), 502

    access_token = token_data.get("access_token")
    if not access_token:
        return jsonify({"error": "SharePoint OAuth token exchange returned no access_token"}), 502

    try:
        client = SharePointClient(access_token)
        user_profile = client.get_current_user()
    except Exception as exc:
        logger.warning("[SHAREPOINT] OAuth validation failed for user %s: %s", sanitize(user_id), exc)
        return jsonify({"error": "Failed to validate SharePoint OAuth token"}), 401

    display_name = (user_profile or {}).get("displayName")
    email = (user_profile or {}).get("mail") or (user_profile or {}).get("userPrincipalName")

    token_payload = {
        "access_token": access_token,
        "user_display_name": display_name,
        "user_email": email,
    }
    refresh_token = token_data.get("refresh_token")
    if refresh_token:
        token_payload["refresh_token"] = refresh_token

    expires_in = token_data.get("expires_in")
    if expires_in:
        token_payload["expires_in"] = expires_in
        token_payload["expires_at"] = int(time.time()) + int(expires_in)

    store_tokens_in_db(user_id, token_payload, "sharepoint")
    return _safe_json_response({
        "success": True,
        "connected": True,
        "userDisplayName": html_escape(display_name) if display_name else None,
        "userEmail": html_escape(email) if email else None,
    })


@sharepoint_bp.route("/status", methods=["GET"])
@require_permission("connectors", "read")
def status(user_id):
    """Check SharePoint connection status."""
    creds = _get_stored_sharepoint_credentials(user_id)
    if not creds or not creds.get("access_token"):
        return jsonify({"connected": False})

    user_profile = _validate_sharepoint_token(user_id, creds)
    if user_profile is None:
        return jsonify({"connected": False})

    display_name = (user_profile or {}).get("displayName") or creds.get("user_display_name")
    email = (
        (user_profile or {}).get("mail")
        or (user_profile or {}).get("userPrincipalName")
        or creds.get("user_email")
    )

    return _safe_json_response({
        "connected": True,
        "userDisplayName": html_escape(display_name) if display_name else None,
        "userEmail": html_escape(email) if email else None,
    })


def _validate_sharepoint_token(user_id: str, creds: Dict[str, Any]) -> Optional[Dict]:
    """Validate the SharePoint token, refreshing if expired. Returns profile or None."""
    try:
        client = SharePointClient(creds.get("access_token"))
        return client.get_current_user()
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        if status_code != 401:
            logger.warning("[SHAREPOINT] Status validation failed for user %s: %s", sanitize(user_id), exc)
            return None
    except Exception as exc:
        logger.warning("[SHAREPOINT] Status validation failed for user %s: %s", sanitize(user_id), exc)
        return None

    refreshed = _refresh_sharepoint_credentials(user_id, creds)
    if not refreshed:
        return None
    try:
        client = SharePointClient(refreshed.get("access_token"))
        return client.get_current_user()
    except Exception as retry_exc:
        logger.warning("[SHAREPOINT] Status validation retry failed for user %s: %s", sanitize(user_id), retry_exc)
        return None


@sharepoint_bp.route("/disconnect", methods=["POST", "DELETE"])
@require_permission("connectors", "write")
def disconnect(user_id):
    """Disconnect SharePoint by removing stored credentials."""
    try:
        secret_ref = None
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            set_rls_context(cursor, conn, user_id, log_prefix="[SHAREPOINT:disconnect]")
            cursor.execute(
                "SELECT secret_ref FROM user_tokens WHERE user_id = %s AND provider = %s",
                (user_id, "sharepoint"),
            )
            row = cursor.fetchone()
            if row:
                secret_ref = row[0]

            cursor.execute(
                "DELETE FROM user_tokens WHERE user_id = %s AND provider = %s",
                (user_id, "sharepoint"),
            )
            deleted_count = cursor.rowcount
            conn.commit()

        if secret_ref:
            try:
                from utils.secrets.secret_ref_utils import secret_manager
                secret_manager.delete_secret(secret_ref)
            except Exception as vault_exc:
                logger.warning("[SHAREPOINT] Failed to delete Vault secret for user %s: %s", sanitize(user_id), vault_exc)

        logger.info("[SHAREPOINT] Disconnected user %s (deleted %s token rows)", sanitize(user_id), deleted_count)
        return jsonify({"success": True, "message": "SharePoint disconnected successfully"})
    except Exception:
        logger.exception("[SHAREPOINT] Failed to disconnect provider")
        return jsonify({"error": "Failed to disconnect SharePoint"}), 500


@sharepoint_bp.route("/search", methods=["POST"])
@require_permission("connectors", "read")
def search(user_id):
    """Search SharePoint for content matching query."""
    data = _get_request_body()
    query = data.get("query")
    if not query:
        return jsonify({"error": "Search query is required"}), 400

    _, _, token_err = _require_access_token(user_id)
    if token_err:
        return token_err

    site_id = data.get("siteId") or data.get("site_id")
    max_results = data.get("maxResults") or data.get("max_results") or 10

    try:
        svc = SharePointSearchService(user_id)
        results = svc.search(query=query, site_id=site_id, max_results=max_results)
    except requests.HTTPError as exc:
        return _handle_http_error(exc, user_id, "Search SharePoint")
    except Exception:
        logger.exception("[SHAREPOINT] Search failed for user %s", sanitize(user_id))
        return jsonify({"error": "Failed to search SharePoint"}), 502

    return _safe_json_response({"results": results, "count": len(results)})


@sharepoint_bp.route("/fetch-page", methods=["POST"])
@require_permission("connectors", "read")
def fetch_page(user_id):
    """Fetch a SharePoint page and return its content as markdown."""
    data = _get_request_body()
    site_id = data.get("siteId") or data.get("site_id")
    page_id = data.get("pageId") or data.get("page_id")
    if not site_id or not page_id:
        return jsonify({"error": "siteId and pageId are required"}), 400

    _, _, token_err = _require_access_token(user_id)
    if token_err:
        return token_err

    try:
        svc = SharePointSearchService(user_id)
        result = svc.fetch_page_markdown(site_id=site_id, page_id=page_id)
    except requests.HTTPError as exc:
        return _handle_http_error(exc, user_id, "Fetch SharePoint page")
    except Exception:
        logger.exception("[SHAREPOINT] Fetch page failed for user %s", sanitize(user_id))
        return jsonify({"error": "Failed to fetch SharePoint page"}), 502

    return _safe_json_response(result)


@sharepoint_bp.route("/fetch-document", methods=["POST"])
@require_permission("connectors", "read")
def fetch_document(user_id):
    """Fetch a SharePoint document and return extracted text."""
    data = _get_request_body()
    drive_id = data.get("driveId") or data.get("drive_id")
    item_id = data.get("itemId") or data.get("item_id")
    if not drive_id or not item_id:
        return jsonify({"error": "driveId and itemId are required"}), 400

    _, _, token_err = _require_access_token(user_id)
    if token_err:
        return token_err

    try:
        svc = SharePointSearchService(user_id)
        result = svc.fetch_document_text(drive_id=drive_id, item_id=item_id)
    except requests.HTTPError as exc:
        return _handle_http_error(exc, user_id, "Fetch SharePoint document")
    except Exception:
        logger.exception("[SHAREPOINT] Fetch document failed for user %s", sanitize(user_id))
        return jsonify({"error": "Failed to fetch SharePoint document"}), 502

    return _safe_json_response(result)


@sharepoint_bp.route("/create-page", methods=["POST"])
@require_permission("connectors", "write")
def create_page(user_id):
    """Create a new SharePoint page."""
    data = _get_request_body()
    title = data.get("title")
    content = data.get("content")
    if not title or not content:
        return jsonify({"error": "title and content are required"}), 400

    _, _, token_err = _require_access_token(user_id)
    if token_err:
        return token_err

    site_id = data.get("siteId") or data.get("site_id")

    try:
        svc = SharePointSearchService(user_id)
        result = svc.create_page(title=title, markdown_content=content, site_id=site_id)
    except requests.HTTPError as exc:
        return _handle_http_error(exc, user_id, "Create SharePoint page")
    except Exception:
        logger.exception("[SHAREPOINT] Create page failed for user %s", sanitize(user_id))
        return jsonify({"error": "Failed to create SharePoint page"}), 502

    return _safe_json_response(result)


@sharepoint_bp.route("/sites", methods=["GET"])
@require_permission("connectors", "read")
def list_sites(user_id):
    """List SharePoint sites, optionally filtered by search query."""
    search_query = request.args.get("search", "")

    access_token, creds, token_err = _require_access_token(user_id)
    if token_err:
        return token_err

    try:
        client = SharePointClient(access_token)
        sites = client.search_sites(search_query)
        return _safe_json_response({"sites": sites, "count": len(sites)})
    except requests.HTTPError as exc:
        return _handle_list_sites_401(exc, user_id, creds, search_query)
    except Exception as exc:
        logger.exception("[SHAREPOINT] List sites failed for user %s: %s", sanitize(user_id), exc)
        return jsonify({"error": "Failed to list SharePoint sites"}), 502


def _handle_list_sites_401(exc: requests.HTTPError, user_id: str, creds: dict, search_query: str):
    """Handle 401 on list-sites by refreshing and retrying once."""
    status_code = exc.response.status_code if exc.response is not None else None
    if status_code != 401:
        logger.exception("[SHAREPOINT] List sites failed for user %s: %s", sanitize(user_id), exc)
        return jsonify({"error": "Failed to list SharePoint sites"}), 502

    refreshed = _refresh_sharepoint_credentials(user_id, creds)
    if not refreshed:
        return jsonify({"error": "SharePoint credentials expired"}), 401

    try:
        client = SharePointClient(refreshed.get("access_token"))
        sites = client.search_sites(search_query)
        return _safe_json_response({"sites": sites, "count": len(sites)})
    except Exception as retry_exc:
        logger.exception("[SHAREPOINT] List sites retry failed for user %s: %s", sanitize(user_id), retry_exc)
        return jsonify({"error": "Failed to list SharePoint sites"}), 502
