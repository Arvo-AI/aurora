"""Jira-specific API routes: search, issue CRUD, comments, links, settings."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, request

from connectors.atlassian_auth.auth import refresh_access_token
from connectors.jira_connector.client import JiraClient
from connectors.jira_connector.adf_converter import markdown_to_adf, text_to_adf
from utils.auth.stateless_auth import get_user_id_from_request
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.web.cors_utils import create_cors_response

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
def search():
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    client, creds, error = _get_jira_client(user_id)
    if error:
        return jsonify({"error": error}), 404 if not creds else 400

    data = request.get_json(force=True, silent=True) or {}
    jql = data.get("jql", "")
    max_results = min(int(data.get("maxResults", 20)), 100)

    try:
        result = client.search_issues(jql, max_results=max_results)
        return jsonify(result)
    except Exception as exc:
        logger.error("[JIRA] Search failed for user %s: %s", user_id, exc)
        return jsonify({"error": "Jira search failed"}), 502


# ------------------------------------------------------------------
# GET /jira/issue/<issue_key>
# ------------------------------------------------------------------

@jira_bp.route("/issue/<issue_key>", methods=["GET", "OPTIONS"])
def get_issue(issue_key):
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    client, creds, error = _get_jira_client(user_id)
    if error:
        return jsonify({"error": error}), 404 if not creds else 400

    try:
        result = client.get_issue(issue_key)
        return jsonify(result)
    except Exception as exc:
        logger.error("[JIRA] Get issue failed for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to get Jira issue"}), 502


# ------------------------------------------------------------------
# POST /jira/issue (create)
# ------------------------------------------------------------------

@jira_bp.route("/issue", methods=["POST", "OPTIONS"])
def create_issue():
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

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
        result = client.create_issue(
            project_key=project_key,
            summary=summary,
            issue_type=issue_type,
            description_adf=description_adf,
            labels=labels,
            parent_key=parent_key,
        )
        return jsonify(result), 201
    except Exception as exc:
        logger.error("[JIRA] Create issue failed for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to create Jira issue"}), 502


# ------------------------------------------------------------------
# PATCH /jira/issue/<issue_key> (update)
# ------------------------------------------------------------------

@jira_bp.route("/issue/<issue_key>", methods=["PATCH", "OPTIONS"])
def update_issue(issue_key):
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    client, creds, error = _get_jira_client(user_id)
    if error:
        return jsonify({"error": error}), 404 if not creds else 400

    data = request.get_json(force=True, silent=True) or {}
    fields = data.get("fields")
    if not fields:
        return jsonify({"error": "fields object required"}), 400

    try:
        client.update_issue(issue_key, fields=fields)
        return jsonify({"success": True})
    except Exception as exc:
        logger.error("[JIRA] Update issue failed for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to update Jira issue"}), 502


# ------------------------------------------------------------------
# POST /jira/issue/<issue_key>/comment
# ------------------------------------------------------------------

@jira_bp.route("/issue/<issue_key>/comment", methods=["POST", "OPTIONS"])
def add_comment(issue_key):
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    client, creds, error = _get_jira_client(user_id)
    if error:
        return jsonify({"error": error}), 404 if not creds else 400

    data = request.get_json(force=True, silent=True) or {}
    body_text = data.get("body", "")
    if not body_text:
        return jsonify({"error": "body is required"}), 400

    body_adf = text_to_adf(body_text)

    try:
        result = client.add_comment(issue_key, body_adf)
        return jsonify(result), 201
    except Exception as exc:
        logger.error("[JIRA] Add comment failed for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to add Jira comment"}), 502


# ------------------------------------------------------------------
# POST /jira/issue/link
# ------------------------------------------------------------------

@jira_bp.route("/issue/link", methods=["POST", "OPTIONS"])
def link_issues():
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

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
        client.link_issues(inward_key, outward_key, link_type)
        return jsonify({"success": True}), 201
    except Exception as exc:
        logger.error("[JIRA] Link issues failed for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to link Jira issues"}), 502


# ------------------------------------------------------------------
# GET/PATCH /jira/settings
# ------------------------------------------------------------------

@jira_bp.route("/settings", methods=["GET", "PATCH", "OPTIONS"])
def settings():
    if request.method == "OPTIONS":
        return create_cors_response()

    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    creds = get_token_data(user_id, "jira")
    if not creds:
        return jsonify({"error": "Jira not connected"}), 404

    if request.method == "GET":
        return jsonify({
            "agentTier": creds.get("agent_tier", "read"),
        })

    data = request.get_json(force=True, silent=True) or {}
    agent_tier = data.get("agentTier")
    if agent_tier and agent_tier in ("read", "write"):
        creds["agent_tier"] = agent_tier
        store_tokens_in_db(user_id, creds, "jira")

    return jsonify({"success": True, "agentTier": creds.get("agent_tier", "read")})
