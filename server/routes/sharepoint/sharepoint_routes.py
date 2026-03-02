"""SharePoint connector routes for auth, status, search, and content operations."""

import logging
import secrets
import time
from typing import Any, Dict, Optional

import requests
from flask import Blueprint, jsonify, request

from connectors.sharepoint_connector.auth import (
    exchange_code_for_token,
    get_auth_url,
    refresh_access_token,
)
from connectors.sharepoint_connector.client import SharePointClient
from connectors.sharepoint_connector.search_service import SharePointSearchService
from utils.db.connection_pool import db_pool
from utils.web.cors_utils import create_cors_response
from utils.auth.oauth2_state_cache import retrieve_oauth2_state, store_oauth2_state
from utils.auth.stateless_auth import get_user_id_from_request
from utils.auth.token_management import get_token_data, store_tokens_in_db

logger = logging.getLogger(__name__)

sharepoint_bp = Blueprint("sharepoint", __name__)


def _get_stored_sharepoint_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve stored SharePoint credentials for user."""
    try:
        return get_token_data(user_id, "sharepoint")
    except Exception as exc:
        logger.error("Failed to retrieve SharePoint credentials for user %s: %s", user_id, exc)
        return None


def _refresh_sharepoint_credentials(user_id: str, creds: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Attempt to refresh SharePoint OAuth credentials."""
    refresh_token = creds.get("refresh_token")
    if not refresh_token:
        return None

    try:
        token_data = refresh_access_token(refresh_token)
    except Exception as exc:
        logger.warning("[SHAREPOINT] OAuth refresh failed for user %s: %s", user_id, exc)
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


@sharepoint_bp.route("/connect", methods=["POST", "OPTIONS"])
def connect():
    """Connect SharePoint via Microsoft OAuth2."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    code = data.get("code")
    if not code:
        # Step 1: Generate state and return auth URL
        state = secrets.token_urlsafe(32)
        store_oauth2_state(state, user_id, "sharepoint")
        auth_url = get_auth_url(state=state)
        return jsonify({"authUrl": auth_url})

    # Step 2: Exchange code for token
    state = data.get("state")
    if not state:
        return jsonify({"error": "Missing OAuth state parameter"}), 400

    state_data = retrieve_oauth2_state(state)
    if not state_data:
        return jsonify({"error": "Invalid or expired OAuth state"}), 400
    if state_data.get("user_id") != user_id or state_data.get("endpoint") != "sharepoint":
        logger.warning("[SHAREPOINT] OAuth state mismatch for user %s", user_id)
        return jsonify({"error": "OAuth state mismatch"}), 400

    try:
        token_data = exchange_code_for_token(code)
    except Exception as exc:
        logger.error("[SHAREPOINT] OAuth token exchange failed for user %s: %s", user_id, exc)
        return jsonify({"error": "SharePoint OAuth token exchange failed"}), 502

    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")
    if not access_token:
        return jsonify({"error": "SharePoint OAuth token exchange returned no access_token"}), 502

    # Validate token by fetching user profile
    try:
        client = SharePointClient(access_token)
        user_profile = client.get_current_user()
    except Exception as exc:
        logger.warning("[SHAREPOINT] OAuth validation failed for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to validate SharePoint OAuth token"}), 401

    display_name = (user_profile or {}).get("displayName")
    email = (user_profile or {}).get("mail") or (user_profile or {}).get("userPrincipalName")

    token_payload = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "user_display_name": display_name,
        "user_email": email,
    }

    expires_in = token_data.get("expires_in")
    if expires_in:
        token_payload["expires_in"] = expires_in
        token_payload["expires_at"] = int(time.time()) + int(expires_in)

    store_tokens_in_db(user_id, token_payload, "sharepoint")
    return jsonify({
        "success": True,
        "connected": True,
        "userDisplayName": display_name,
        "userEmail": email,
    })


@sharepoint_bp.route("/status", methods=["GET", "OPTIONS"])
def status():
    """Check SharePoint connection status."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    creds = _get_stored_sharepoint_credentials(user_id)
    if not creds:
        return jsonify({"connected": False})

    access_token = creds.get("access_token")
    if not access_token:
        return jsonify({"connected": False})

    try:
        client = SharePointClient(access_token)
        user_profile = client.get_current_user()
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response else None
        if status_code == 401:
            refreshed = _refresh_sharepoint_credentials(user_id, creds)
            if refreshed:
                try:
                    client = SharePointClient(refreshed.get("access_token"))
                    user_profile = client.get_current_user()
                except Exception as retry_exc:
                    logger.warning("[SHAREPOINT] Status validation retry failed for user %s: %s", user_id, retry_exc)
                    return jsonify({"connected": False})
            else:
                return jsonify({"connected": False})
        else:
            logger.warning("[SHAREPOINT] Status validation failed for user %s: %s", user_id, exc)
            return jsonify({"connected": False})
    except Exception as exc:
        logger.warning("[SHAREPOINT] Status validation failed for user %s: %s", user_id, exc)
        return jsonify({"connected": False})

    display_name = (user_profile or {}).get("displayName") or creds.get("user_display_name")
    email = (
        (user_profile or {}).get("mail")
        or (user_profile or {}).get("userPrincipalName")
        or creds.get("user_email")
    )

    return jsonify({
        "connected": True,
        "userDisplayName": display_name,
        "userEmail": email,
    })


@sharepoint_bp.route("/disconnect", methods=["POST", "DELETE", "OPTIONS"])
def disconnect():
    """Disconnect SharePoint by removing stored credentials."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM user_tokens WHERE user_id = %s AND provider = %s",
                (user_id, "sharepoint"),
            )
            deleted_count = cursor.rowcount
            conn.commit()

        logger.info("[SHAREPOINT] Disconnected user %s (deleted %s token rows)", user_id, deleted_count)
        return jsonify({"success": True, "message": "SharePoint disconnected successfully"})
    except Exception as exc:
        logger.exception("[SHAREPOINT] Failed to disconnect user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to disconnect SharePoint"}), 500


@sharepoint_bp.route("/search", methods=["POST", "OPTIONS"])
def search():
    """Search SharePoint for content matching query."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    query = data.get("query")
    if not query:
        return jsonify({"error": "Search query is required"}), 400

    site_id = data.get("siteId") or data.get("site_id")
    max_results = data.get("maxResults") or data.get("max_results") or 10

    creds = _get_stored_sharepoint_credentials(user_id)
    if not creds:
        return jsonify({"error": "SharePoint not connected"}), 404

    access_token = creds.get("access_token")
    if not access_token:
        return jsonify({"error": "SharePoint credentials missing"}), 400

    try:
        svc = SharePointSearchService(user_id)
        results = svc.search(query=query, site_id=site_id, max_results=max_results)
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response else None
        if status_code == 401:
            return jsonify({"error": "SharePoint credentials expired"}), 401
        logger.exception("[SHAREPOINT] Search failed for user %s", user_id)
        return jsonify({"error": "Failed to search SharePoint"}), 502
    except Exception:
        logger.exception("[SHAREPOINT] Search failed for user %s", user_id)
        return jsonify({"error": "Failed to search SharePoint"}), 502

    return jsonify({"results": results, "count": len(results)})


@sharepoint_bp.route("/fetch-page", methods=["POST", "OPTIONS"])
def fetch_page():
    """Fetch a SharePoint page and return its content as markdown."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    site_id = data.get("siteId") or data.get("site_id")
    page_id = data.get("pageId") or data.get("page_id")
    if not site_id or not page_id:
        return jsonify({"error": "siteId and pageId are required"}), 400

    creds = _get_stored_sharepoint_credentials(user_id)
    if not creds:
        return jsonify({"error": "SharePoint not connected"}), 404

    access_token = creds.get("access_token")
    if not access_token:
        return jsonify({"error": "SharePoint credentials missing"}), 400

    try:
        svc = SharePointSearchService(user_id)
        result = svc.fetch_page_markdown(site_id=site_id, page_id=page_id)
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response else None
        if status_code == 401:
            return jsonify({"error": "SharePoint credentials expired"}), 401
        logger.exception("[SHAREPOINT] Fetch page failed for user %s", user_id)
        return jsonify({"error": "Failed to fetch SharePoint page"}), 502
    except Exception:
        logger.exception("[SHAREPOINT] Fetch page failed for user %s", user_id)
        return jsonify({"error": "Failed to fetch SharePoint page"}), 502

    return jsonify(result)


@sharepoint_bp.route("/fetch-document", methods=["POST", "OPTIONS"])
def fetch_document():
    """Fetch a SharePoint document and return extracted text."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    drive_id = data.get("driveId") or data.get("drive_id")
    item_id = data.get("itemId") or data.get("item_id")
    if not drive_id or not item_id:
        return jsonify({"error": "driveId and itemId are required"}), 400

    creds = _get_stored_sharepoint_credentials(user_id)
    if not creds:
        return jsonify({"error": "SharePoint not connected"}), 404

    access_token = creds.get("access_token")
    if not access_token:
        return jsonify({"error": "SharePoint credentials missing"}), 400

    try:
        svc = SharePointSearchService(user_id)
        result = svc.fetch_document_text(drive_id=drive_id, item_id=item_id)
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response else None
        if status_code == 401:
            return jsonify({"error": "SharePoint credentials expired"}), 401
        logger.exception("[SHAREPOINT] Fetch document failed for user %s", user_id)
        return jsonify({"error": "Failed to fetch SharePoint document"}), 502
    except Exception:
        logger.exception("[SHAREPOINT] Fetch document failed for user %s", user_id)
        return jsonify({"error": "Failed to fetch SharePoint document"}), 502

    return jsonify(result)


@sharepoint_bp.route("/create-page", methods=["POST", "OPTIONS"])
def create_page():
    """Create a new SharePoint page."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    title = data.get("title")
    content = data.get("content")
    if not title or not content:
        return jsonify({"error": "title and content are required"}), 400

    site_id = data.get("siteId") or data.get("site_id")

    creds = _get_stored_sharepoint_credentials(user_id)
    if not creds:
        return jsonify({"error": "SharePoint not connected"}), 404

    access_token = creds.get("access_token")
    if not access_token:
        return jsonify({"error": "SharePoint credentials missing"}), 400

    try:
        svc = SharePointSearchService(user_id)
        result = svc.create_page(title=title, markdown_content=content, site_id=site_id)
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response else None
        if status_code == 401:
            return jsonify({"error": "SharePoint credentials expired"}), 401
        logger.exception("[SHAREPOINT] Create page failed for user %s", user_id)
        return jsonify({"error": "Failed to create SharePoint page"}), 502
    except Exception:
        logger.exception("[SHAREPOINT] Create page failed for user %s", user_id)
        return jsonify({"error": "Failed to create SharePoint page"}), 502

    return jsonify(result)


@sharepoint_bp.route("/sites", methods=["GET", "OPTIONS"])
def list_sites():
    """List SharePoint sites, optionally filtered by search query."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    search_query = request.args.get("search", "")

    creds = _get_stored_sharepoint_credentials(user_id)
    if not creds:
        return jsonify({"error": "SharePoint not connected"}), 404

    access_token = creds.get("access_token")
    if not access_token:
        return jsonify({"error": "SharePoint credentials missing"}), 400

    try:
        client = SharePointClient(access_token)
        sites = client.search_sites(search_query)
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response else None
        if status_code == 401:
            refreshed = _refresh_sharepoint_credentials(user_id, creds)
            if refreshed:
                try:
                    client = SharePointClient(refreshed.get("access_token"))
                    sites = client.search_sites(search_query)
                except Exception as retry_exc:
                    logger.exception("[SHAREPOINT] List sites retry failed for user %s: %s", user_id, retry_exc)
                    return jsonify({"error": "Failed to list SharePoint sites"}), 502
            else:
                return jsonify({"error": "SharePoint credentials expired"}), 401
        else:
            logger.exception("[SHAREPOINT] List sites failed for user %s: %s", user_id, exc)
            return jsonify({"error": "Failed to list SharePoint sites"}), 502
    except Exception as exc:
        logger.exception("[SHAREPOINT] List sites failed for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to list SharePoint sites"}), 502

    return jsonify({"sites": sites, "count": len(sites)})
