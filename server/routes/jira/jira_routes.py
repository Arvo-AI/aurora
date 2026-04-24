"""Jira-specific API routes: search, issue CRUD, comments, links, settings."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, request

from connectors.atlassian_auth.auth import refresh_access_token
from connectors.jira_connector.client import JiraClient
from connectors.jira_connector.adf_converter import markdown_to_adf, text_to_adf
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_user_preference, store_user_preference
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.log_sanitizer import sanitize
from utils.secrets.secret_ref_utils import delete_user_secret

logger = logging.getLogger(__name__)

jira_bp = Blueprint("jira", __name__)


def _get_jira_client(user_id: str) -> tuple[Optional[JiraClient], Optional[Dict[str, Any]], Optional[str]]:
    """Build a JiraClient from stored credentials.

    Returns (client, creds, error_message).
    """
    creds = get_token_data(user_id, "jira")
    if not creds:
        return None, None, "Jira not connected"

    auth_type = (creds.get("auth_type") or "oauth").lower()
    base_url = creds.get("base_url", "")
    cloud_id = creds.get("cloud_id") if auth_type == "oauth" else None
    token = creds.get("pat_token") if auth_type == "pat" else creds.get("access_token")

    if not token:
        return None, creds, "Jira credentials incomplete"

    client = JiraClient(base_url, token, auth_type=auth_type, cloud_id=cloud_id)
    return client, creds, None


def _with_refresh(user_id, creds, client, operation):
    """Run *operation(client)* and retry once with refreshed credentials on 401."""
    import requests as _requests
    try:
        return operation(client)
    except _requests.exceptions.HTTPError as exc:
        if getattr(getattr(exc, "response", None), "status_code", None) != 401:
            raise
    except Exception:
        raise

    refreshed = _refresh_jira_credentials(user_id, creds)
    if not refreshed:
        raise ValueError("Jira token refresh failed — user should reconnect.")
    new_token = refreshed.get("access_token")
    if not new_token:
        raise ValueError("Jira token refresh returned no access_token.")
    new_client = JiraClient(
        creds.get("base_url", ""),
        new_token,
        auth_type=(creds.get("auth_type") or "oauth").lower(),
        cloud_id=creds.get("cloud_id"),
    )
    return operation(new_client)


def _refresh_jira_credentials(user_id: str, creds: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Attempt to refresh OAuth Jira credentials."""
    refresh_token = creds.get("refresh_token")
    if not refresh_token:
        return None
    try:
        token_data = refresh_access_token(refresh_token)
    except Exception as exc:
        logger.warning("[JIRA] OAuth refresh failed for user %s: %s", user_id, exc)
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

    store_tokens_in_db(user_id, updated, "jira")
    return updated


# ------------------------------------------------------------------
# POST /jira/search
# ------------------------------------------------------------------

@jira_bp.route("/search", methods=["POST", "OPTIONS"])
@require_permission("connectors", "read")
def search(user_id):
    client, creds, error = _get_jira_client(user_id)
    if error:
        return jsonify({"error": error}), 404 if not creds else 400

    data = request.get_json(force=True, silent=True) or {}
    jql = data.get("jql", "")
    try:
        max_results = min(int(data.get("maxResults", 20)), 100)
    except (TypeError, ValueError):
        return jsonify({"error": "maxResults must be a number"}), 400

    try:
        result = _with_refresh(user_id, creds, client, lambda c: c.search_issues(jql, max_results=max_results))
        return jsonify(result)
    except Exception as exc:
        logger.error("[JIRA] Search failed for user %s: %s", user_id, exc)
        return jsonify({"error": "Jira search failed"}), 502


# ------------------------------------------------------------------
# GET /jira/issue/<issue_key>
# ------------------------------------------------------------------

@jira_bp.route("/issue/<issue_key>", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def get_issue(user_id, issue_key):
    client, creds, error = _get_jira_client(user_id)
    if error:
        return jsonify({"error": error}), 404 if not creds else 400

    try:
        result = _with_refresh(user_id, creds, client, lambda c: c.get_issue(issue_key))
        return jsonify(result)
    except Exception as exc:
        logger.error("[JIRA] Get issue failed for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to get Jira issue"}), 502


# ------------------------------------------------------------------
# POST /jira/issue (create)
# ------------------------------------------------------------------

@jira_bp.route("/issue", methods=["POST", "OPTIONS"])
@require_permission("connectors", "write")
def create_issue(user_id):
    client, creds, error = _get_jira_client(user_id)
    if error:
        return jsonify({"error": error}), 404 if not creds else 400

    data = request.get_json(force=True, silent=True) or {}
    project_key = data.get("projectKey")
    summary = data.get("summary")
    if not project_key or not summary:
        return jsonify({"error": "projectKey and summary are required"}), 400

    description = data.get("description", "")
    description_adf = markdown_to_adf(description) if description else None
    issue_type = data.get("issueType", "Task")
    labels = data.get("labels")
    parent_key = data.get("parentKey")

    try:
        result = _with_refresh(user_id, creds, client, lambda c: c.create_issue(
            project_key=project_key,
            summary=summary,
            issue_type=issue_type,
            description_adf=description_adf,
            labels=labels,
            parent_key=parent_key,
        ))
        return jsonify(result), 201
    except Exception as exc:
        logger.error("[JIRA] Create issue failed for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to create Jira issue"}), 502


# ------------------------------------------------------------------
# PATCH /jira/issue/<issue_key> (update)
# ------------------------------------------------------------------

@jira_bp.route("/issue/<issue_key>", methods=["PATCH", "OPTIONS"])
@require_permission("connectors", "write")
def update_issue(user_id, issue_key):
    client, creds, error = _get_jira_client(user_id)
    if error:
        return jsonify({"error": error}), 404 if not creds else 400

    data = request.get_json(force=True, silent=True) or {}
    fields = data.get("fields")
    if not fields:
        return jsonify({"error": "fields object required"}), 400

    try:
        _with_refresh(user_id, creds, client, lambda c: c.update_issue(issue_key, fields=fields))
        return jsonify({"success": True})
    except Exception as exc:
        logger.error("[JIRA] Update issue failed for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to update Jira issue"}), 502


# ------------------------------------------------------------------
# POST /jira/issue/<issue_key>/comment
# ------------------------------------------------------------------

@jira_bp.route("/issue/<issue_key>/comment", methods=["POST", "OPTIONS"])
@require_permission("connectors", "write")
def add_comment(user_id, issue_key):
    client, creds, error = _get_jira_client(user_id)
    if error:
        return jsonify({"error": error}), 404 if not creds else 400

    data = request.get_json(force=True, silent=True) or {}
    body_text = data.get("body", "")
    if not body_text:
        return jsonify({"error": "body is required"}), 400

    body_adf = text_to_adf(body_text)

    try:
        result = _with_refresh(user_id, creds, client, lambda c: c.add_comment(issue_key, body_adf))
        return jsonify(result), 201
    except Exception as exc:
        logger.error("[JIRA] Add comment failed for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to add Jira comment"}), 502


# ------------------------------------------------------------------
# POST /jira/issue/link
# ------------------------------------------------------------------

@jira_bp.route("/issue/link", methods=["POST", "OPTIONS"])
@require_permission("connectors", "write")
def link_issues(user_id):
    client, creds, error = _get_jira_client(user_id)
    if error:
        return jsonify({"error": error}), 404 if not creds else 400

    data = request.get_json(force=True, silent=True) or {}
    inward_key = data.get("inwardKey")
    outward_key = data.get("outwardKey")
    link_type = data.get("linkType", "Relates")

    if not inward_key or not outward_key:
        return jsonify({"error": "inwardKey and outwardKey are required"}), 400

    try:
        _with_refresh(user_id, creds, client, lambda c: c.link_issues(inward_key, outward_key, link_type))
        return jsonify({"success": True}), 201
    except Exception as exc:
        logger.error("[JIRA] Link issues failed for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to link Jira issues"}), 502


# ------------------------------------------------------------------
# GET /jira/settings
# ------------------------------------------------------------------

JIRA_MODE_KEY = "jira_mode"
VALID_MODES = ("full", "comment_only")


@jira_bp.route("/settings", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def get_settings(user_id):
    mode = get_user_preference(user_id, JIRA_MODE_KEY, default="comment_only")
    return jsonify({"jiraMode": mode})


# ------------------------------------------------------------------
# PUT /jira/settings
# ------------------------------------------------------------------

@jira_bp.route("/settings", methods=["PUT", "OPTIONS"])
@require_permission("connectors", "write")
def update_settings(user_id):
    data = request.get_json(force=True, silent=True) or {}
    mode = data.get("jiraMode")

    if mode not in VALID_MODES:
        return jsonify({"error": f"jiraMode must be one of: {', '.join(VALID_MODES)}"}), 400

    store_user_preference(user_id, JIRA_MODE_KEY, mode)
    logger.info("[JIRA] Updated settings for user %s: jiraMode=%s", sanitize(user_id), sanitize(mode))

    return jsonify({"success": True, "jiraMode": mode})


# ------------------------------------------------------------------
# GET /jira/status
# ------------------------------------------------------------------

@jira_bp.route("/status", methods=["GET", "OPTIONS"])
@require_permission("connectors", "read")
def jira_status(user_id):
    """Check Jira connection status."""
    creds = get_token_data(user_id, "jira")
    if not creds:
        return jsonify({"connected": False})

    auth_type = (creds.get("auth_type") or "oauth").lower()
    base_url = creds.get("base_url", "")
    token = creds.get("pat_token") if auth_type == "pat" else creds.get("access_token")

    if not token:
        return jsonify({"connected": False})

    cloud_id = creds.get("cloud_id") if auth_type == "oauth" else None
    client = JiraClient(base_url, token, auth_type=auth_type, cloud_id=cloud_id)
    try:
        client.get_myself()
    except Exception:
        if auth_type == "oauth":
            refreshed = _refresh_jira_credentials(user_id, creds)
            if refreshed:
                token = refreshed.get("access_token")
                try:
                    JiraClient(base_url, token, auth_type=auth_type, cloud_id=cloud_id).get_myself()
                except Exception:
                    return jsonify({"connected": False})
            else:
                return jsonify({"connected": False})
        else:
            return jsonify({"connected": False})

    return jsonify({
        "connected": True,
        "authType": auth_type,
        "baseUrl": base_url,
        "cloudId": creds.get("cloud_id"),
    })


# ------------------------------------------------------------------
# POST|DELETE /jira/disconnect
# ------------------------------------------------------------------

@jira_bp.route("/disconnect", methods=["POST", "DELETE", "OPTIONS"])
@require_permission("connectors", "write")
def disconnect(user_id):
    """Disconnect Jira by removing stored credentials."""
    try:
        success, deleted_count = delete_user_secret(user_id, "jira")
        if not success:
            logger.warning("[JIRA] Failed to clean up secrets during disconnect")
            return jsonify({"success": False, "error": "Failed to delete stored credentials"}), 500

        logger.info("[JIRA] Disconnected provider (deleted %s token rows)", deleted_count)
        return jsonify({"success": True, "message": "Jira disconnected successfully", "deleted": deleted_count})
    except Exception as exc:
        logger.exception("[JIRA] Failed to disconnect provider")
        return jsonify({"error": "Failed to disconnect Jira"}), 500
