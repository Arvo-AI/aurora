"""Confluence connector routes for auth, status, and disconnect."""

import logging
import secrets
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

import requests
from flask import Blueprint, jsonify, request

from connectors.confluence_connector.auth import (
    exchange_code_for_token,
    fetch_accessible_resources,
    get_auth_url,
    refresh_access_token,
    select_confluence_resource,
)
from connectors.confluence_connector.client import (
    ConfluenceClient,
    normalize_confluence_base_url,
    parse_confluence_page_id,
)
from connectors.confluence_connector.runbook_parser import parse_confluence_runbook
from utils.db.connection_pool import db_pool
from utils.web.cors_utils import create_cors_response
from utils.auth.oauth2_state_cache import retrieve_oauth2_state, store_oauth2_state
from utils.auth.stateless_auth import get_user_id_from_request
from utils.auth.token_management import get_token_data, store_tokens_in_db

logger = logging.getLogger(__name__)

confluence_bp = Blueprint("confluence", __name__)


def _get_stored_confluence_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve stored Confluence credentials for user."""
    try:
        return get_token_data(user_id, "confluence")
    except Exception as exc:
        logger.error("Failed to retrieve Confluence credentials for user %s: %s", user_id, exc)
        return None


def _extract_user_fields(user_payload: Dict[str, Any]) -> Dict[str, Optional[str]]:
    display_name = user_payload.get("displayName") or user_payload.get("publicName")
    email = user_payload.get("email") or user_payload.get("emailAddress")
    return {"user_display_name": display_name, "user_email": email}


def _refresh_confluence_credentials(user_id: str, creds: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    refresh_token = creds.get("refresh_token")
    if not refresh_token:
        return None

    try:
        token_data = refresh_access_token(refresh_token)
    except Exception as exc:
        logger.warning("[CONFLUENCE] OAuth refresh failed for user %s: %s", user_id, exc)
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

    store_tokens_in_db(user_id, updated_creds, "confluence")
    return updated_creds


def _fetch_page_payload(
    user_id: str,
    page_url: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[str], Optional[int]]:
    creds = _get_stored_confluence_credentials(user_id)
    if not creds:
        return None, None, "Confluence not connected", 404

    base_url = creds.get("base_url")
    auth_type = (creds.get("auth_type") or "oauth").lower()
    token = creds.get("pat_token") if auth_type == "pat" else creds.get("access_token")

    if not base_url or not token:
        return None, None, "Confluence credentials missing", 400

    try:
        page_parsed = urlparse(page_url)
        base_parsed = urlparse(base_url)
        if not page_parsed.scheme or not page_parsed.netloc:
            return None, None, "Invalid Confluence page URL", 400
        if not base_parsed.scheme or not base_parsed.netloc:
            return None, None, "Invalid Confluence base URL", 400
        if (
            page_parsed.scheme != base_parsed.scheme
            or page_parsed.netloc.lower() != base_parsed.netloc.lower()
        ):
            return None, None, "Confluence page URL does not match configured base URL", 400
    except Exception:
        return None, None, "Invalid Confluence page URL", 400

    page_id = parse_confluence_page_id(page_url)
    if not page_id:
        return None, None, "Unable to parse Confluence page ID from URL", 400

    cloud_id = creds.get("cloud_id") if auth_type == "oauth" else None
    try:
        client = ConfluenceClient(base_url, token, auth_type=auth_type, cloud_id=cloud_id)
        page_payload = client.get_page(page_id)
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response else None
        if status_code == 401 and auth_type == "oauth":
            refreshed = _refresh_confluence_credentials(user_id, creds)
            if refreshed:
                token = refreshed.get("access_token")
                cloud_id = refreshed.get("cloud_id") if auth_type == "oauth" else None
                try:
                    client = ConfluenceClient(base_url, token, auth_type=auth_type, cloud_id=cloud_id)
                    page_payload = client.get_page(page_id)
                except Exception as retry_exc:
                    logger.exception("[CONFLUENCE] Retry fetch failed for user %s: %s", user_id, retry_exc)
                    return None, creds, "Failed to fetch Confluence page", 502
            else:
                return None, creds, "Confluence credentials expired", 401
        else:
            logger.exception("[CONFLUENCE] Failed to fetch page for user %s: %s", user_id, exc)
            return None, creds, "Failed to fetch Confluence page", 502
    except Exception as exc:
        logger.exception("[CONFLUENCE] Failed to fetch page for user %s: %s", user_id, exc)
        return None, creds, "Failed to fetch Confluence page", 502

    return page_payload, creds, None, None


@confluence_bp.route("/connect", methods=["POST", "OPTIONS"])
def connect():
    """Connect Confluence Cloud (OAuth) or Data Center (PAT)."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    auth_type = (data.get("authType") or data.get("auth_type") or "oauth").lower()
    base_url = data.get("baseUrl") or data.get("base_url")

    if auth_type == "pat":
        pat_token = data.get("patToken") or data.get("pat_token")
        if not base_url:
            return jsonify({"error": "Confluence baseUrl is required for PAT auth"}), 400
        if not pat_token:
            return jsonify({"error": "Confluence PAT token is required"}), 400

        base_url = normalize_confluence_base_url(base_url)
        client = ConfluenceClient(base_url, pat_token, auth_type="pat")

        try:
            user_payload = client.get_current_user()
        except Exception as exc:
            logger.warning("[CONFLUENCE] PAT validation failed for user %s: %s", user_id, exc)
            return jsonify({"error": "Failed to validate Confluence PAT"}), 401

        user_fields = _extract_user_fields(user_payload or {})
        token_payload = {
            "auth_type": "pat",
            "base_url": base_url,
            "pat_token": pat_token,
            **user_fields,
        }

        store_tokens_in_db(user_id, token_payload, "confluence")
        return jsonify({
            "success": True,
            "connected": True,
            "authType": "pat",
            "baseUrl": base_url,
            "userDisplayName": user_fields["user_display_name"],
            "userEmail": user_fields["user_email"],
        })

    if auth_type != "oauth":
        return jsonify({"error": "Unsupported authType. Use 'oauth' or 'pat'."}), 400

    code = data.get("code")
    if not code:
        state = secrets.token_urlsafe(32)
        store_oauth2_state(state, user_id, "confluence")
        auth_url = get_auth_url(state=state)
        return jsonify({"authUrl": auth_url})

    state = data.get("state")
    if not state:
        return jsonify({"error": "Missing OAuth state parameter"}), 400

    state_data = retrieve_oauth2_state(state)
    if not state_data:
        return jsonify({"error": "Invalid or expired OAuth state"}), 400
    if state_data.get("user_id") != user_id or state_data.get("endpoint") != "confluence":
        logger.warning(
            "[CONFLUENCE] OAuth state mismatch for user %s (state user %s, endpoint %s)",
            user_id,
            state_data.get("user_id"),
            state_data.get("endpoint"),
        )
        return jsonify({"error": "OAuth state mismatch"}), 400

    try:
        token_data = exchange_code_for_token(code)
    except Exception as exc:
        logger.error("[CONFLUENCE] OAuth token exchange failed for user %s: %s", user_id, exc)
        return jsonify({"error": "Confluence OAuth token exchange failed"}), 502

    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")
    if not access_token:
        return jsonify({"error": "Confluence OAuth token exchange returned no access_token"}), 502

    cloud_id = None
    try:
        resources = fetch_accessible_resources(access_token)
        resource = select_confluence_resource(resources or [])
        if resource:
            cloud_id = resource.get("id")
            if not base_url:
                base_url = resource.get("url")
    except Exception as exc:
        logger.warning("[CONFLUENCE] Failed to resolve accessible resources: %s", exc)

    if not base_url:
        return jsonify({"error": "Confluence baseUrl is required for OAuth"}), 400

    base_url = normalize_confluence_base_url(base_url)
    client = ConfluenceClient(base_url, access_token, auth_type="oauth", cloud_id=cloud_id)

    try:
        user_payload = client.get_current_user()
    except Exception as exc:
        logger.warning("[CONFLUENCE] OAuth validation failed for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to validate Confluence OAuth token"}), 401

    user_fields = _extract_user_fields(user_payload or {})
    token_payload = {
        "auth_type": "oauth",
        "base_url": base_url,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "cloud_id": cloud_id,
        **user_fields,
    }

    store_tokens_in_db(user_id, token_payload, "confluence")
    return jsonify({
        "success": True,
        "connected": True,
        "authType": "oauth",
        "baseUrl": base_url,
        "cloudId": cloud_id,
        "userDisplayName": user_fields["user_display_name"],
        "userEmail": user_fields["user_email"],
    })


@confluence_bp.route("/status", methods=["GET", "OPTIONS"])
def status():
    """Check Confluence connection status."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    creds = _get_stored_confluence_credentials(user_id)
    if not creds:
        return jsonify({"connected": False})

    auth_type = (creds.get("auth_type") or "oauth").lower()
    base_url = creds.get("base_url")
    token = creds.get("pat_token") if auth_type == "pat" else creds.get("access_token")

    if not base_url or not token:
        return jsonify({"connected": False})

    cloud_id = creds.get("cloud_id") if auth_type == "oauth" else None
    try:
        client = ConfluenceClient(base_url, token, auth_type=auth_type, cloud_id=cloud_id)
        user_payload = client.get_current_user()
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response else None
        if status_code == 401 and auth_type == "oauth":
            refreshed = _refresh_confluence_credentials(user_id, creds)
            if refreshed:
                token = refreshed.get("access_token")
                cloud_id = refreshed.get("cloud_id") if auth_type == "oauth" else None
                try:
                    client = ConfluenceClient(base_url, token, auth_type=auth_type, cloud_id=cloud_id)
                    user_payload = client.get_current_user()
                except Exception as retry_exc:
                    logger.warning("[CONFLUENCE] Status validation retry failed for user %s: %s", user_id, retry_exc)
                    return jsonify({"connected": False})
            else:
                return jsonify({"connected": False})
        else:
            logger.warning("[CONFLUENCE] Status validation failed for user %s: %s", user_id, exc)
            return jsonify({"connected": False})
    except Exception as exc:
        logger.warning("[CONFLUENCE] Status validation failed for user %s: %s", user_id, exc)
        return jsonify({"connected": False})

    user_fields = _extract_user_fields(user_payload or {})
    return jsonify({
        "connected": True,
        "authType": auth_type,
        "baseUrl": base_url,
        "cloudId": creds.get("cloud_id"),
        "userDisplayName": user_fields["user_display_name"] or creds.get("user_display_name"),
        "userEmail": user_fields["user_email"] or creds.get("user_email"),
    })


@confluence_bp.route("/disconnect", methods=["POST", "DELETE", "OPTIONS"])
def disconnect():
    """Disconnect Confluence by removing stored credentials."""
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
                (user_id, "confluence"),
            )
            deleted_count = cursor.rowcount
            conn.commit()

        logger.info("[CONFLUENCE] Disconnected user %s (deleted %s token rows)", user_id, deleted_count)
        return jsonify({"success": True, "message": "Confluence disconnected successfully"})
    except Exception as exc:
        logger.exception("[CONFLUENCE] Failed to disconnect user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to disconnect Confluence"}), 500


@confluence_bp.route("/fetch", methods=["POST", "OPTIONS"])
def fetch_page():
    """Fetch a Confluence page by URL and return the raw page payload."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    page_url = data.get("url") or data.get("pageUrl") or data.get("page_url")
    if not page_url:
        return jsonify({"error": "Confluence page URL is required"}), 400

    page_payload, _, error_message, error_status = _fetch_page_payload(user_id, page_url)
    if error_message:
        return jsonify({"error": error_message}), error_status
    return jsonify(page_payload)


@confluence_bp.route("/parse", methods=["POST", "OPTIONS"])
def parse_page():
    """Fetch a Confluence page and return cleaned runbook content."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    page_url = data.get("url") or data.get("pageUrl") or data.get("page_url")
    if not page_url:
        return jsonify({"error": "Confluence page URL is required"}), 400

    page_payload, creds, error_message, error_status = _fetch_page_payload(user_id, page_url)
    if error_message:
        return jsonify({"error": error_message}), error_status

    storage_html = (page_payload.get("body") or {}).get("storage", {}).get("value") or ""
    parsed = parse_confluence_runbook(storage_html)

    title = page_payload.get("title")
    page_id = page_payload.get("id") or page_payload.get("pageId")
    base_url = (creds or {}).get("base_url")

    return jsonify({
        "title": title,
        "pageId": page_id,
        "pageUrl": page_url,
        "baseUrl": base_url,
        "markdown": parsed.get("markdown"),
        "sections": parsed.get("sections"),
        "steps": parsed.get("steps"),
    })
