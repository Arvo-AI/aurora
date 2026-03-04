"""API routes for postmortem CRUD operations and Confluence export."""

import logging
import time
from datetime import timezone
from typing import Any, Dict, Optional
from uuid import UUID

import requests
from flask import Blueprint, jsonify, request

from connectors.confluence_connector.client import (
    ConfluenceClient,
    markdown_to_confluence_storage,
)
from utils.auth.stateless_auth import get_user_id_from_request
from utils.auth.token_management import get_token_data, store_tokens_in_db
from connectors.confluence_connector.auth import refresh_access_token
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

postmortem_bp = Blueprint("postmortem", __name__)


def _validate_uuid(value: str) -> bool:
    """Validate that a string is a valid UUID."""
    try:
        UUID(value)
        return True
    except (ValueError, TypeError):
        return False


def _format_timestamp(ts) -> Optional[str]:
    """Format timestamp ensuring UTC timezone."""
    if not ts:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.isoformat()


def _refresh_confluence_credentials(user_id: str, creds: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Attempt to refresh OAuth Confluence credentials."""
    refresh_token = creds.get("refresh_token")
    if not refresh_token:
        return None

    try:
        token_data = refresh_access_token(refresh_token)
    except Exception as exc:
        logger.warning(
            "[POSTMORTEM] OAuth refresh failed for user %s: %s", user_id, exc
        )
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


@postmortem_bp.route("/api/incidents/<incident_id>/postmortem", methods=["GET"])
def get_postmortem(incident_id):
    """Fetch the postmortem for an incident."""
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    if not _validate_uuid(incident_id):
        return jsonify({"error": "Invalid incident ID"}), 400

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SET myapp.current_user_id = %s", (user_id,))
                conn.commit()
                cursor.execute(
                    """SELECT id, incident_id, user_id, content, generated_at, updated_at,
                              confluence_page_id, confluence_page_url, confluence_exported_at,
                              jira_issue_id, jira_issue_key, jira_issue_url, jira_exported_at
                       FROM postmortems
                       WHERE incident_id = %s AND user_id = %s""",
                    (incident_id, user_id),
                )
                row = cursor.fetchone()

        if not row:
            return jsonify({"error": "Postmortem not found"}), 404

        postmortem = {
            "id": str(row[0]),
            "incidentId": str(row[1]),
            "userId": row[2],
            "content": row[3],
            "generatedAt": _format_timestamp(row[4]),
            "updatedAt": _format_timestamp(row[5]),
            "confluencePageId": row[6],
            "confluencePageUrl": row[7],
            "confluenceExportedAt": _format_timestamp(row[8]),
            "jiraIssueId": row[9],
            "jiraIssueKey": row[10],
            "jiraIssueUrl": row[11],
            "jiraExportedAt": _format_timestamp(row[12]),
        }
        return jsonify({"postmortem": postmortem})

    except Exception as e:
        logger.error(
            "[POSTMORTEM] Failed to fetch postmortem for incident %s: %s",
            incident_id,
            e,
        )
        return jsonify({"error": "Failed to fetch postmortem"}), 500


@postmortem_bp.route("/api/incidents/<incident_id>/postmortem", methods=["PATCH"])
def update_postmortem(incident_id):
    """Update postmortem content."""
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    if not _validate_uuid(incident_id):
        return jsonify({"error": "Invalid incident ID"}), 400

    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    content = data.get("content")
    if not content or not isinstance(content, str) or not content.strip():
        return jsonify({"error": "Content is required"}), 400

    if len(content) > 100000:
        return jsonify(
            {"error": "Content exceeds maximum length of 100000 characters"}
        ), 400

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SET myapp.current_user_id = %s", (user_id,))
                conn.commit()
                cursor.execute(
                    """UPDATE postmortems
                       SET content = %s, updated_at = CURRENT_TIMESTAMP
                       WHERE incident_id = %s AND user_id = %s""",
                    (content, incident_id, user_id),
                )
                updated = cursor.rowcount
                conn.commit()

        if not updated:
            return jsonify({"error": "Postmortem not found"}), 404

        return jsonify({"success": True})

    except Exception as e:
        logger.error(
            "[POSTMORTEM] Failed to update postmortem for incident %s: %s",
            incident_id,
            e,
        )
        return jsonify({"error": "Failed to update postmortem"}), 500


@postmortem_bp.route(
    "/api/incidents/<incident_id>/postmortem/export/confluence", methods=["POST"]
)
def export_to_confluence(incident_id):
    """Export postmortem to Confluence."""
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    if not _validate_uuid(incident_id):
        return jsonify({"error": "Invalid incident ID"}), 400

    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    space_key = data.get("spaceKey")
    if not space_key:
        return jsonify({"error": "spaceKey is required"}), 400

    parent_page_id = data.get("parentPageId")

    # Fetch postmortem content from DB
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SET myapp.current_user_id = %s", (user_id,))
                conn.commit()
                cursor.execute(
                    """SELECT id, content FROM postmortems
                       WHERE incident_id = %s AND user_id = %s""",
                    (incident_id, user_id),
                )
                row = cursor.fetchone()
    except Exception as e:
        logger.error(
            "[POSTMORTEM] Failed to fetch postmortem for export, incident %s: %s",
            incident_id,
            e,
        )
        return jsonify({"error": "Failed to fetch postmortem"}), 500

    if not row:
        return jsonify({"error": "Postmortem not found"}), 404

    postmortem_id = row[0]
    content = row[1]

    if not content:
        return jsonify({"error": "Postmortem has no content to export"}), 400

    # Get Confluence credentials
    creds = get_token_data(user_id, "confluence")
    if not creds:
        return jsonify({"error": "Confluence not connected"}), 404

    auth_type = (creds.get("auth_type") or "oauth").lower()
    base_url = creds.get("base_url")
    token = creds.get("pat_token") if auth_type == "pat" else creds.get("access_token")

    if not base_url or not token:
        return jsonify({"error": "Confluence credentials incomplete"}), 400

    cloud_id = creds.get("cloud_id") if auth_type == "oauth" else None

    # Convert markdown to Confluence storage format
    content_html = markdown_to_confluence_storage(content)

    # Build page title from first line or fallback
    title = f"Postmortem - Incident {incident_id[:8]}"

    # Create page on Confluence
    try:
        client = ConfluenceClient(
            base_url, token, auth_type=auth_type, cloud_id=cloud_id
        )
        result = client.create_page(
            space_key, title, content_html, parent_id=parent_page_id
        )
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response else None
        if status_code == 401 and auth_type == "oauth":
            refreshed = _refresh_confluence_credentials(user_id, creds)
            if refreshed:
                token = refreshed.get("access_token")
                cloud_id = refreshed.get("cloud_id") if auth_type == "oauth" else None
                try:
                    client = ConfluenceClient(
                        base_url, token, auth_type=auth_type, cloud_id=cloud_id
                    )
                    result = client.create_page(
                        space_key, title, content_html, parent_id=parent_page_id
                    )
                except Exception as retry_exc:
                    logger.exception(
                        "[POSTMORTEM] Retry Confluence export failed for user %s: %s",
                        user_id,
                        retry_exc,
                    )
                    return jsonify({"error": "Failed to export to Confluence"}), 502
            else:
                return jsonify({"error": "Confluence credentials expired"}), 401
        else:
            logger.exception(
                "[POSTMORTEM] Confluence export failed for user %s: %s", user_id, exc
            )
            return jsonify({"error": "Failed to export to Confluence"}), 502
    except Exception as exc:
        logger.exception(
            "[POSTMORTEM] Confluence export failed for user %s: %s", user_id, exc
        )
        return jsonify({"error": "Failed to export to Confluence"}), 502

    page_id = result.get("id")
    page_url = result.get("url")

    if not page_id:
        logger.error(
            "[POSTMORTEM] Confluence export returned no page id for incident %s",
            incident_id,
        )
        return jsonify({"error": "Invalid response from Confluence"}), 502

    # Update postmortem record with Confluence details
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SET myapp.current_user_id = %s", (user_id,))
                conn.commit()
                cursor.execute(
                    """UPDATE postmortems
                       SET confluence_page_id = %s,
                           confluence_page_url = %s,
                           confluence_exported_at = CURRENT_TIMESTAMP
                       WHERE id = %s AND user_id = %s""",
                    (str(page_id), page_url, str(postmortem_id), user_id),
                )
                conn.commit()
    except Exception as e:
        logger.warning(
            "[POSTMORTEM] Failed to update Confluence metadata for postmortem %s: %s",
            postmortem_id,
            e,
        )
        # Still return success since the page was created

    return jsonify({"success": True, "pageUrl": page_url, "pageId": str(page_id)})



@postmortem_bp.route("/api/postmortems", methods=["GET"])
def list_postmortems():
    """Fetch postmortems for the authenticated user with pagination."""
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    try:
        limit = min(int(request.args.get("limit", 50)), 100)
        offset = max(int(request.args.get("offset", 0)), 0)
    except (ValueError, TypeError):
        limit, offset = 50, 0

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SET myapp.current_user_id = %s", (user_id,))
                conn.commit()
                cursor.execute(
                    """SELECT p.id, p.incident_id, p.user_id, p.content, p.generated_at, p.updated_at,
                              p.confluence_page_id, p.confluence_page_url, p.confluence_exported_at,
                              i.alert_title,
                              p.jira_issue_id, p.jira_issue_key, p.jira_issue_url, p.jira_exported_at
                       FROM postmortems p
                       LEFT JOIN incidents i ON p.incident_id = i.id
                       WHERE p.user_id = %s
                       ORDER BY p.generated_at DESC
                       LIMIT %s OFFSET %s""",
                    (user_id, limit, offset),
                )
                rows = cursor.fetchall()

        postmortems = []
        for row in rows:
            postmortem = {
                "id": str(row[0]),
                "incidentId": str(row[1]),
                "incidentTitle": row[9],
                "content": row[3],
                "generatedAt": _format_timestamp(row[4]),
                "updatedAt": _format_timestamp(row[5]),
                "confluencePageId": row[6],
                "confluencePageUrl": row[7],
                "confluenceExportedAt": _format_timestamp(row[8]),
                "jiraIssueId": row[10],
                "jiraIssueKey": row[11],
                "jiraIssueUrl": row[12],
                "jiraExportedAt": _format_timestamp(row[13]),
            }
            postmortems.append(postmortem)

        return jsonify({"postmortems": postmortems})

    except Exception as e:
        logger.error(
            "[POSTMORTEM] Failed to fetch postmortems for user %s: %s",
            user_id,
            e,
        )
        return jsonify({"error": "Failed to fetch postmortems"}), 500


@postmortem_bp.route(
    "/api/incidents/<incident_id>/postmortem/export/jira", methods=["POST"]
)
def export_to_jira(incident_id):
    """Export postmortem to Jira as a parent issue with subtasks for action items."""
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "User authentication required"}), 401

    if not _validate_uuid(incident_id):
        return jsonify({"error": "Invalid incident ID"}), 400

    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    project_key = data.get("projectKey")
    if not project_key:
        return jsonify({"error": "projectKey is required"}), 400

    issue_type = data.get("issueType", "Task")

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SET myapp.current_user_id = %s", (user_id,))
                conn.commit()
                cursor.execute(
                    """SELECT id, content FROM postmortems
                       WHERE incident_id = %s AND user_id = %s""",
                    (incident_id, user_id),
                )
                row = cursor.fetchone()
    except Exception as e:
        logger.error(
            "[POSTMORTEM] Failed to fetch postmortem for Jira export, incident %s: %s",
            incident_id,
            e,
        )
        return jsonify({"error": "Failed to fetch postmortem"}), 500

    if not row:
        return jsonify({"error": "Postmortem not found"}), 404

    postmortem_id = row[0]
    content = row[1]

    if not content:
        return jsonify({"error": "Postmortem has no content to export"}), 400

    creds = get_token_data(user_id, "jira")
    if not creds:
        return jsonify({"error": "Jira not connected"}), 404

    auth_type = (creds.get("auth_type") or "oauth").lower()
    base_url = creds.get("base_url", "")
    cloud_id = creds.get("cloud_id") if auth_type == "oauth" else None
    token = creds.get("pat_token") if auth_type == "pat" else creds.get("access_token")

    if not token:
        return jsonify({"error": "Jira credentials incomplete"}), 400

    from connectors.jira_connector.adf_converter import markdown_to_adf, extract_action_items, text_to_adf
    from connectors.jira_connector.client import JiraClient

    description_adf = markdown_to_adf(content)
    title = f"Postmortem - Incident {incident_id[:8]}"

    try:
        client = JiraClient(base_url, token, auth_type=auth_type, cloud_id=cloud_id)
        parent_result = client.create_issue(
            project_key=project_key,
            summary=title,
            issue_type=issue_type,
            description_adf=description_adf,
        )
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response else None
        if status_code == 401 and auth_type == "oauth":
            refreshed = _refresh_jira_credentials(user_id, creds)
            if refreshed:
                token = refreshed.get("access_token")
                try:
                    client = JiraClient(base_url, token, auth_type=auth_type, cloud_id=cloud_id)
                    parent_result = client.create_issue(
                        project_key=project_key,
                        summary=title,
                        issue_type=issue_type,
                        description_adf=description_adf,
                    )
                except Exception as retry_exc:
                    logger.exception("[POSTMORTEM] Retry Jira export failed for user %s: %s", user_id, retry_exc)
                    return jsonify({"error": "Failed to export to Jira"}), 502
            else:
                return jsonify({"error": "Jira credentials expired"}), 401
        else:
            logger.exception("[POSTMORTEM] Jira export failed for user %s: %s", user_id, exc)
            return jsonify({"error": "Failed to export to Jira"}), 502
    except Exception as exc:
        logger.exception("[POSTMORTEM] Jira export failed for user %s: %s", user_id, exc)
        return jsonify({"error": "Failed to export to Jira"}), 502

    parent_key = parent_result.get("key")
    parent_id = parent_result.get("id")
    parent_url = f"{base_url}/browse/{parent_key}" if parent_key and base_url else None

    action_items = extract_action_items(content)
    subtask_keys = []
    for item in action_items:
        if not item.get("text"):
            continue
        try:
            sub_result = client.create_subtask(
                parent_key=parent_key,
                project_key=project_key,
                summary=item["text"][:255],
                description_adf=text_to_adf(item["text"]),
            )
            subtask_keys.append(sub_result.get("key"))
        except Exception as sub_exc:
            logger.warning("[POSTMORTEM] Failed to create subtask: %s", sub_exc)

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SET myapp.current_user_id = %s", (user_id,))
                conn.commit()
                cursor.execute(
                    """UPDATE postmortems
                       SET jira_issue_id = %s,
                           jira_issue_key = %s,
                           jira_issue_url = %s,
                           jira_exported_at = CURRENT_TIMESTAMP
                       WHERE id = %s AND user_id = %s""",
                    (str(parent_id), parent_key, parent_url, str(postmortem_id), user_id),
                )
                conn.commit()
    except Exception as e:
        logger.warning("[POSTMORTEM] Failed to update Jira metadata for postmortem %s: %s", postmortem_id, e)

    return jsonify({
        "success": True,
        "issueKey": parent_key,
        "issueId": str(parent_id),
        "issueUrl": parent_url,
        "subtasks": subtask_keys,
    })


def _refresh_jira_credentials(user_id: str, creds: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Attempt to refresh OAuth Jira credentials."""
    from routes.jira.jira_routes import _refresh_jira_credentials as _refresh
    return _refresh(user_id, creds)