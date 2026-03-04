"""Unified Atlassian connection routes.

Handles OAuth and PAT flows for Confluence, Jira, or both products in a single
authorization flow.  Stores separate ``provider`` entries per product.
"""

from __future__ import annotations

import logging
import secrets as _secrets
import time
from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, request

from connectors.atlassian_auth.auth import (
    exchange_code_for_token,
    fetch_accessible_resources,
    get_auth_url,
    refresh_access_token,
    select_resource_for_product,
)
from connectors.confluence_connector.client import (
    ConfluenceClient,
    normalize_confluence_base_url,
)
from connectors.jira_connector.client import JiraClient
from utils.auth.oauth2_state_cache import retrieve_oauth2_state, store_oauth2_state
from utils.auth.stateless_auth import get_user_id_from_request
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.db.connection_pool import db_pool
from utils.web.cors_utils import create_cors_response

logger = logging.getLogger(__name__)

atlassian_bp = Blueprint("atlassian", __name__)

VALID_PRODUCTS = {"confluence", "jira"}


def _refresh_credentials(user_id: str, creds: Dict[str, Any], provider: str) -> Optional[Dict[str, Any]]:
    """Attempt to refresh OAuth credentials for a given provider."""
    refresh_token = creds.get("refresh_token")
    if not refresh_token:
        return None
    try:
        token_data = refresh_access_token(refresh_token)
    except Exception as exc:
        logger.warning("[ATLASSIAN] OAuth refresh failed for user %s provider %s: %s", user_id, provider, exc)
        return None

    access_token = token_data.get("access_token")
    if not access_token:
        return None

    updated = dict(creds)
    updated["access_token"] = access_token
    new_refresh = token_data.get("refresh_token")
    if new_refresh:
        updated["refresh_token"] = new_refresh
    expires_in = token_data.get("expires_in")
    if expires_in:
        updated["expires_in"] = expires_in
        updated["expires_at"] = int(time.time()) + int(expires_in)

    store_tokens_in_db(user_id, updated, provider)
    return updated


def _validate_confluence(access_token: str, base_url: str, auth_type: str, cloud_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Validate Confluence credentials and return user info."""
    base_url = normalize_confluence_base_url(base_url)
    client = ConfluenceClient(base_url, access_token, auth_type=auth_type, cloud_id=cloud_id)
    try:
        payload = client.get_current_user()
        return payload
    except Exception as exc:
        logger.warning("[ATLASSIAN] Confluence validation failed: %s", exc)
        return None


def _validate_jira(access_token: str, base_url: str, auth_type: str, cloud_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Validate Jira credentials and return user info."""
    client = JiraClient(base_url or "", access_token, auth_type=auth_type, cloud_id=cloud_id)
    try:
        payload = client.get_myself()
        return payload
    except Exception as exc:
        logger.warning("[ATLASSIAN] Jira validation failed: %s", exc)
        return None


# ------------------------------------------------------------------
# POST /atlassian/connect
# ------------------------------------------------------------------

@atlassian_bp.route("/connect", methods=["POST", "OPTIONS"])
def connect():
    """Unified connect for Atlassian products (Confluence/Jira/both)."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    products: List[str] = data.get("products") or ["confluence"]
    products = [p.lower() for p in products if p.lower() in VALID_PRODUCTS]
    if not products:
        return jsonify({"error": "At least one product required (confluence, jira)"}), 400

    auth_type = (data.get("authType") or "oauth").lower()

    # ------------------------------------------------------------------
    # PAT flow (per-product)
    # ------------------------------------------------------------------
    if auth_type == "pat":
        results: Dict[str, Any] = {}
        for product in products:
            pat_token = data.get(f"{product}PatToken") or data.get("patToken")
            base_url = data.get(f"{product}BaseUrl") or data.get("baseUrl")
            if not base_url or not pat_token:
                results[product] = {"connected": False, "error": f"baseUrl and patToken required for {product}"}
                continue

            if product == "confluence":
                user_payload = _validate_confluence(pat_token, base_url, "pat", None)
            else:
                user_payload = _validate_jira(pat_token, base_url, "pat", None)

            if not user_payload:
                results[product] = {"connected": False, "error": f"Failed to validate {product} PAT"}
                continue

            token_payload: Dict[str, Any] = {
                "auth_type": "pat",
                "base_url": base_url.rstrip("/"),
                "pat_token": pat_token,
            }
            if product == "jira":
                token_payload["agent_tier"] = data.get("agentTier", "read")

            store_tokens_in_db(user_id, token_payload, product)
            results[product] = {"connected": True, "authType": "pat", "baseUrl": base_url}

        return jsonify({"success": True, "results": results})

    # ------------------------------------------------------------------
    # OAuth flow
    # ------------------------------------------------------------------
    if auth_type != "oauth":
        return jsonify({"error": "Unsupported authType. Use 'oauth' or 'pat'."}), 400

    code = data.get("code")

    # Step 1: no code yet – generate auth URL
    if not code:
        state = _secrets.token_urlsafe(32)
        # Encode products in the endpoint field (e.g. "atlassian:confluence,jira")
        endpoint_key = "atlassian:" + ",".join(sorted(products))
        store_oauth2_state(state, user_id, endpoint_key)
        auth_url = get_auth_url(state=state, products=products)
        return jsonify({"authUrl": auth_url})

    # Step 2: exchange code for token
    state = data.get("state")
    if not state:
        return jsonify({"error": "Missing OAuth state parameter"}), 400

    state_data = retrieve_oauth2_state(state)
    if not state_data:
        return jsonify({"error": "Invalid or expired OAuth state"}), 400
    if state_data.get("user_id") != user_id:
        return jsonify({"error": "OAuth state mismatch"}), 400

    # Extract products from the endpoint key (e.g. "atlassian:confluence,jira")
    endpoint_key = state_data.get("endpoint", "")
    if endpoint_key.startswith("atlassian:"):
        stored_products = endpoint_key.split(":", 1)[1].split(",")
    else:
        stored_products = products

    try:
        token_data = exchange_code_for_token(code)
    except Exception as exc:
        logger.error("[ATLASSIAN] OAuth token exchange failed for user %s: %s", user_id, exc)
        return jsonify({"error": "Atlassian OAuth token exchange failed"}), 502

    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")
    if not access_token:
        return jsonify({"error": "Atlassian OAuth returned no access_token"}), 502

    # Discover cloud resources
    cloud_id = None
    site_url = None
    try:
        resources = fetch_accessible_resources(access_token)
        logger.info("[ATLASSIAN] Accessible resources for user %s: %s", user_id, resources)
        for product in stored_products:
            resource = select_resource_for_product(resources or [], product)
            if resource:
                cloud_id = cloud_id or resource.get("id")
                site_url = site_url or resource.get("url")
    except Exception as exc:
        logger.warning("[ATLASSIAN] Failed to resolve accessible resources: %s", exc)

    base_url = data.get("baseUrl") or site_url or ""

    results = {}
    for product in stored_products:
        if product == "confluence":
            user_payload = _validate_confluence(access_token, base_url, "oauth", cloud_id)
        else:
            user_payload = _validate_jira(access_token, base_url, "oauth", cloud_id)

        if not user_payload:
            results[product] = {"connected": False, "error": f"Token lacks {product} access"}
            continue

        payload: Dict[str, Any] = {
            "auth_type": "oauth",
            "base_url": base_url.rstrip("/") if base_url else "",
            "access_token": access_token,
            "refresh_token": refresh_token,
            "cloud_id": cloud_id,
        }
        if product == "jira":
            payload["agent_tier"] = data.get("agentTier", "read")

        store_tokens_in_db(user_id, payload, product)
        results[product] = {"connected": True, "authType": "oauth", "baseUrl": base_url, "cloudId": cloud_id}

    return jsonify({"success": True, "connected": True, "results": results})


# ------------------------------------------------------------------
# GET /atlassian/status
# ------------------------------------------------------------------

@atlassian_bp.route("/status", methods=["GET", "OPTIONS"])
def status():
    """Return connection status for all Atlassian products."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    result: Dict[str, Any] = {}

    for product in VALID_PRODUCTS:
        creds = get_token_data(user_id, product)
        if not creds:
            result[product] = {"connected": False}
            continue

        auth_type = (creds.get("auth_type") or "oauth").lower()
        base_url = creds.get("base_url", "")
        cloud_id = creds.get("cloud_id") if auth_type == "oauth" else None
        token = creds.get("pat_token") if auth_type == "pat" else creds.get("access_token")

        if not token:
            result[product] = {"connected": False}
            continue

        if product == "confluence":
            user_payload = _validate_confluence(token, base_url, auth_type, cloud_id)
        else:
            user_payload = _validate_jira(token, base_url, auth_type, cloud_id)

        if not user_payload and auth_type == "oauth":
            refreshed = _refresh_credentials(user_id, creds, product)
            if refreshed:
                token = refreshed.get("access_token")
                if product == "confluence":
                    user_payload = _validate_confluence(token, base_url, auth_type, cloud_id)
                else:
                    user_payload = _validate_jira(token, base_url, auth_type, cloud_id)

        if not user_payload:
            result[product] = {"connected": False}
            continue

        info: Dict[str, Any] = {
            "connected": True,
            "authType": auth_type,
            "baseUrl": base_url,
            "cloudId": cloud_id,
        }
        if product == "jira":
            info["agentTier"] = creds.get("agent_tier", "read")

        result[product] = info

    return jsonify(result)


# ------------------------------------------------------------------
# POST /atlassian/disconnect
# ------------------------------------------------------------------

@atlassian_bp.route("/disconnect", methods=["POST", "OPTIONS"])
def disconnect():
    """Disconnect one or all Atlassian products."""
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    product = (data.get("product") or "").lower()
    targets = list(VALID_PRODUCTS) if product == "all" else [product]

    if not all(t in VALID_PRODUCTS for t in targets):
        return jsonify({"error": "product must be 'confluence', 'jira', or 'all'"}), 400

    deleted = 0
    try:
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            for target in targets:
                cursor.execute(
                    "DELETE FROM user_tokens WHERE user_id = %s AND provider = %s",
                    (user_id, target),
                )
                deleted += cursor.rowcount
            conn.commit()
    except Exception as exc:
        logger.error("[ATLASSIAN] Disconnect failed for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to disconnect"}), 500

    return jsonify({"success": True, "deleted": deleted})
