"""API routes for postmortem CRUD operations and exports.

Backed by the `artifacts` / `artifact_versions` tables (category='postmortem').
Export metadata lives in `postmortem_exports`.
"""

import logging
import time
from functools import wraps
from typing import Any, Dict, Optional

import requests
from flask import Blueprint, jsonify, request
from routes.audit_routes import record_audit_event

from connectors.confluence_connector.client import (
    ConfluenceClient,
    markdown_to_confluence_storage,
)
from services.artifacts.store import create_version
from utils.auth.token_management import get_token_data, store_tokens_in_db
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request, set_rls_context
from connectors.confluence_connector.auth import refresh_access_token
from utils.db.connection_pool import db_pool
from utils.log_sanitizer import sanitize
from utils.query_helpers import iso_utc
from utils.validation import is_valid_uuid, strip_nul

logger = logging.getLogger(__name__)

postmortem_bp = Blueprint("postmortem", __name__)
_LOG_PREFIX = "[Postmortem]"

_CATEGORY = "postmortem"


def with_incident_postmortem(require_postmortem=False):
    """Decorator that validates incident_id, resolves org_id, opens DB, sets RLS.

    Injects keyword args: org_id, conn, cursor, artifact_id (if require_postmortem).
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(user_id, incident_id, *args, **kwargs):
            if not is_valid_uuid(incident_id):
                return jsonify({"error": "Invalid incident ID"}), 400

            org_id = get_org_id_from_request()

            try:
                with db_pool.get_admin_connection() as conn:
                    with conn.cursor() as cursor:
                        set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX)

                        artifact_id = None
                        if require_postmortem:
                            cursor.execute(
                                """SELECT id FROM artifacts
                                   WHERE incident_id = %s AND org_id = %s
                                         AND category = %s""",
                                (incident_id, org_id, _CATEGORY),
                            )
                            row = cursor.fetchone()
                            if not row:
                                return jsonify({"error": "Postmortem not found"}), 404
                            artifact_id = str(row[0])

                        return fn(
                            user_id, incident_id, *args,
                            org_id=org_id, conn=conn, cursor=cursor,
                            postmortem_id=artifact_id, **kwargs,
                        )

            except Exception as e:
                logger.error(
                    "%s %s failed for incident %s: %s",
                    _LOG_PREFIX, fn.__name__, incident_id, e,
                )
                return jsonify({"error": f"Failed to {fn.__name__.replace('_', ' ')}"}), 500

        return wrapper
    return decorator


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


def _refresh_jira_credentials(user_id: str, creds: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Attempt to refresh OAuth Jira credentials."""
    from connectors.atlassian_auth.auth import refresh_access_token as _refresh_token

    refresh_token = creds.get("refresh_token")
    if not refresh_token:
        return None
    try:
        token_data = _refresh_token(refresh_token)
    except Exception as exc:
        logger.warning("[POSTMORTEM] Jira OAuth refresh failed for user %s: %s", user_id, exc)
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


def _build_postmortem_response(artifact_row, exports):
    """Build the standard postmortem response dict from an artifact row and exports map."""
    confluence = exports.get("confluence", {})
    jira = exports.get("jira", {})
    notion = exports.get("notion", {})

    return {
        "id": str(artifact_row["id"]),
        "incidentId": str(artifact_row["incident_id"]),
        "userId": artifact_row["user_id"],
        "content": artifact_row["content"],
        "generatedAt": iso_utc(artifact_row["created_at"]),
        "updatedAt": iso_utc(artifact_row["updated_at"]),
        "confluencePageId": confluence.get("external_id"),
        "confluencePageUrl": confluence.get("external_url"),
        "confluenceExportedAt": iso_utc(confluence.get("exported_at")),
        "jiraIssueId": jira.get("external_id"),
        "jiraIssueKey": jira.get("external_key"),
        "jiraIssueUrl": jira.get("external_url"),
        "jiraExportedAt": iso_utc(jira.get("exported_at")),
        "notionPageId": notion.get("external_id"),
        "notionPageUrl": notion.get("external_url"),
        "notionExportedAt": iso_utc(notion.get("exported_at")),
        "notionDatabaseId": notion.get("external_database_id"),
        "generationSessionId": artifact_row["generation_session_id"],
    }


def _fetch_exports(cursor, artifact_id):
    """Fetch postmortem_exports rows for an artifact, keyed by destination."""
    cursor.execute(
        """SELECT destination, external_id, external_key, external_url,
                  external_database_id, exported_at
           FROM postmortem_exports
           WHERE postmortem_id = %s""",
        (artifact_id,),
    )
    exports = {}
    for row in cursor.fetchall():
        exports[row[0]] = {
            "external_id": row[1],
            "external_key": row[2],
            "external_url": row[3],
            "external_database_id": row[4],
            "exported_at": row[5],
        }
    return exports


# ---------------------------------------------------------------------------
# GET / PATCH single postmortem
# ---------------------------------------------------------------------------


@postmortem_bp.route("/api/incidents/<incident_id>/postmortem", methods=["GET"])
@require_permission("postmortems", "read")
@with_incident_postmortem(require_postmortem=False)
def get_postmortem(user_id, incident_id, *, org_id, conn, cursor, postmortem_id, **kwargs):

    cursor.execute(
        """SELECT id, incident_id, user_id, content, created_at, updated_at,
                  generation_session_id
           FROM artifacts
           WHERE incident_id = %s AND org_id = %s AND category = %s""",
        (incident_id, org_id, _CATEGORY),
    )
    row = cursor.fetchone()

    if not row:
        return jsonify({"error": "Postmortem not found"}), 404

    # Row exists but content is NULL -> generation in progress
    if row[3] is None:
        return jsonify({"status": "generating", "generationSessionId": row[6]}), 202

    artifact_id = str(row[0])
    artifact_row = {
        "id": row[0],
        "incident_id": row[1],
        "user_id": row[2],
        "content": row[3],
        "created_at": row[4],
        "updated_at": row[5],
        "generation_session_id": row[6],
    }

    exports = _fetch_exports(cursor, artifact_id)
    postmortem = _build_postmortem_response(artifact_row, exports)
    return jsonify({"postmortem": postmortem})


@postmortem_bp.route("/api/incidents/<incident_id>/postmortem", methods=["PATCH"])
@require_permission("postmortems", "write")
@with_incident_postmortem(require_postmortem=True)
def update_postmortem(user_id, incident_id, *, org_id, conn, cursor, postmortem_id, **kwargs):

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

    content = strip_nul(content)

    # Snapshot the pre-edit content into version history
    cursor.execute("SELECT content FROM artifacts WHERE id = %s", (postmortem_id,))
    prev_row = cursor.fetchone()
    if prev_row and prev_row[0]:
        create_version(
            cursor, postmortem_id, org_id, user_id, prev_row[0],
            source="pre_edit", set_current=False,
        )

    cursor.execute(
        """UPDATE artifacts
           SET content = %s, updated_at = CURRENT_TIMESTAMP
           WHERE id = %s""",
        (content, postmortem_id),
    )
    conn.commit()

    record_audit_event(org_id, user_id, "update_postmortem", "postmortem", incident_id, {}, request)
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# Version history endpoints
# ---------------------------------------------------------------------------


@postmortem_bp.route("/api/incidents/<incident_id>/postmortem/versions", methods=["GET"])
@require_permission("postmortems", "read")
@with_incident_postmortem(require_postmortem=False)
def list_postmortem_versions(user_id, incident_id, *, org_id, conn, cursor, postmortem_id, **kwargs):
    """List version history for a postmortem."""
    cursor.execute(
        """SELECT v.id, v.version_number, v.source, v.user_id, v.created_at,
                  v.generation_session_id, a.current_version_id
           FROM artifact_versions v
           JOIN artifacts a ON v.artifact_id = a.id
           WHERE a.incident_id = %s AND a.org_id = %s AND a.category = %s
           ORDER BY v.version_number DESC""",
        (incident_id, org_id, _CATEGORY),
    )
    rows = cursor.fetchall()

    current_version_id = str(rows[0][6]) if rows and rows[0][6] else None

    versions = [
        {
            "id": str(row[0]),
            "versionNumber": row[1],
            "source": row[2],
            "userId": row[3],
            "createdAt": iso_utc(row[4]),
            "generationSessionId": str(row[5]) if row[5] else None,
        }
        for row in rows
    ]
    return jsonify({"versions": versions, "currentVersionId": current_version_id})


@postmortem_bp.route("/api/incidents/<incident_id>/postmortem/versions/<version_id>", methods=["GET"])
@require_permission("postmortems", "read")
@with_incident_postmortem(require_postmortem=False)
def get_postmortem_version(user_id, incident_id, version_id, *, org_id, conn, cursor, postmortem_id, **kwargs):
    """Get a specific postmortem version content."""
    if not is_valid_uuid(version_id):
        return jsonify({"error": "Invalid version ID"}), 400

    cursor.execute(
        """SELECT v.id, v.version_number, v.source, v.user_id, v.content, v.created_at
           FROM artifact_versions v
           JOIN artifacts a ON v.artifact_id = a.id
           WHERE v.id = %s AND a.incident_id = %s AND a.org_id = %s AND a.category = %s""",
        (version_id, incident_id, org_id, _CATEGORY),
    )
    row = cursor.fetchone()

    if not row:
        return jsonify({"error": "Version not found"}), 404

    return jsonify({
        "version": {
            "id": str(row[0]),
            "versionNumber": row[1],
            "source": row[2],
            "userId": row[3],
            "content": row[4],
            "createdAt": iso_utc(row[5]),
        }
    })


@postmortem_bp.route("/api/incidents/<incident_id>/postmortem/versions/<version_id>/restore", methods=["POST"])
@require_permission("postmortems", "write")
@with_incident_postmortem(require_postmortem=True)
def restore_postmortem_version(user_id, incident_id, version_id, *, org_id, conn, cursor, postmortem_id, **kwargs):
    """Restore a previous postmortem version as the current content."""
    if not is_valid_uuid(version_id):
        return jsonify({"error": "Invalid version ID"}), 400

    cursor.execute(
        """SELECT v.content
           FROM artifact_versions v
           JOIN artifacts a ON v.artifact_id = a.id
           WHERE v.id = %s AND a.incident_id = %s AND a.org_id = %s AND a.category = %s""",
        (version_id, incident_id, org_id, _CATEGORY),
    )
    row = cursor.fetchone()
    if not row:
        return jsonify({"error": "Version not found"}), 404

    restored_content = row[0]

    cursor.execute(
        """UPDATE artifacts
           SET content = %s, current_version_id = %s, updated_at = CURRENT_TIMESTAMP
           WHERE id = %s""",
        (restored_content, version_id, postmortem_id),
    )
    conn.commit()

    record_audit_event(org_id, user_id, "restore_postmortem_version", "postmortem", incident_id,
                       {"version_id": version_id}, request)
    return jsonify({"success": True, "content": restored_content})


# ---------------------------------------------------------------------------
# Regenerate
# ---------------------------------------------------------------------------


@postmortem_bp.route("/api/incidents/<incident_id>/postmortem/regenerate", methods=["POST"])
@require_permission("postmortems", "write")
def regenerate_postmortem(user_id, incident_id):
    """Trigger postmortem generation (or regeneration) via the built-in action."""
    if not is_valid_uuid(incident_id):
        return jsonify({"error": "Invalid incident ID"}), 400

    try:
        from services.actions.postmortem_action import dispatch_postmortem_action
        run_id = dispatch_postmortem_action(user_id, incident_id)
        return jsonify({"success": True, "runId": run_id}), 202

    except ValueError as e:
        if "Rate limited" in str(e):
            return jsonify({"error": "Rate limited \u2014 try again later"}), 429
        if "already running" in str(e):
            return jsonify({"error": "Generation already in progress"}), 409
        return jsonify({"error": "Unable to generate postmortem"}), 400
    except Exception as e:
        logger.error(
            "[POSTMORTEM] Failed to regenerate postmortem for incident %s: %s",
            incident_id,
            e,
        )
        return jsonify({"error": "Failed to regenerate postmortem"}), 500


# ---------------------------------------------------------------------------
# Export to Confluence
# ---------------------------------------------------------------------------


@postmortem_bp.route(
    "/api/incidents/<incident_id>/postmortem/export/confluence", methods=["POST"]
)
@require_permission("postmortems", "write")
def export_to_confluence(user_id, incident_id):

    if not is_valid_uuid(incident_id):
        return jsonify({"error": "Invalid incident ID"}), 400

    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    space_key = data.get("spaceKey")
    if not space_key:
        return jsonify({"error": "spaceKey is required"}), 400

    parent_page_id = data.get("parentPageId")

    org_id = get_org_id_from_request()

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX)
                cursor.execute(
                    """SELECT id, content FROM artifacts
                       WHERE incident_id = %s AND org_id = %s AND category = %s""",
                    (incident_id, org_id, _CATEGORY),
                )
                row = cursor.fetchone()
    except Exception as e:
        logger.error(
            "[POSTMORTEM] Failed to fetch postmortem for export, incident %s: %s",
            sanitize(incident_id),
            e,
        )
        return jsonify({"error": "Failed to fetch postmortem"}), 500

    if not row:
        return jsonify({"error": "Postmortem not found"}), 404

    artifact_id = str(row[0])
    content = row[1]

    if not content:
        return jsonify({"error": "Postmortem has no content to export"}), 400

    creds = get_token_data(user_id, "confluence")
    if not creds:
        return jsonify({"error": "Confluence not connected"}), 404

    auth_type = (creds.get("auth_type") or "oauth").lower()
    base_url = creds.get("base_url")
    token = creds.get("pat_token") if auth_type == "pat" else creds.get("access_token")

    if not base_url or not token:
        return jsonify({"error": "Confluence credentials incomplete"}), 400

    cloud_id = creds.get("cloud_id") if auth_type == "oauth" else None

    content_html = markdown_to_confluence_storage(content)
    title = f"Postmortem - Incident {incident_id[:8]}"

    try:
        client = ConfluenceClient(
            base_url, token, auth_type=auth_type, cloud_id=cloud_id
        )
        result = client.create_page(
            space_key, title, content_html, parent_id=parent_page_id
        )
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
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
            sanitize(incident_id),
        )
        return jsonify({"error": "Invalid response from Confluence"}), 502

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX)
                cursor.execute(
                    """INSERT INTO postmortem_exports
                           (postmortem_id, org_id, destination, external_id, external_url, exported_at)
                       VALUES (%s, %s, 'confluence', %s, %s, CURRENT_TIMESTAMP)
                       ON CONFLICT (postmortem_id, destination)
                       DO UPDATE SET external_id = EXCLUDED.external_id,
                                     external_url = EXCLUDED.external_url,
                                     exported_at = EXCLUDED.exported_at""",
                    (artifact_id, org_id, str(page_id), page_url),
                )
                conn.commit()
    except Exception as e:
        logger.warning(
            "[POSTMORTEM] Failed to update Confluence metadata for artifact %s: %s",
            artifact_id,
            e,
        )

    record_audit_event(org_id, user_id, "export_postmortem_confluence", "postmortem", incident_id,
                       {"page_url": page_url}, request)
    return jsonify({"success": True, "pageUrl": page_url, "pageId": str(page_id)})


# ---------------------------------------------------------------------------
# Export to Notion
# ---------------------------------------------------------------------------


@postmortem_bp.route(
    "/api/incidents/<incident_id>/postmortem/export/notion", methods=["POST"]
)
@require_permission("postmortems", "write")
def export_to_notion(user_id, incident_id):
    """Export postmortem to a Notion database."""
    if not is_valid_uuid(incident_id):
        return jsonify({"error": "Invalid incident ID"}), 400
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    database_id = data.get("databaseId") or data.get("database_id")
    if not database_id:
        return jsonify({"error": "databaseId is required"}), 400

    title_property = data.get("titleProperty") or data.get("title_property")
    property_mapping = data.get("propertyMapping") or data.get("property_mapping")
    action_items_db = (
        data.get("actionItemsDatabaseId")
        or data.get("action_items_database_id")
    )

    from chat.backend.agent.tools.notion import _export_postmortem_to_notion
    from connectors.notion_connector.client import NotionAuthExpiredError

    try:
        result = _export_postmortem_to_notion(
            user_id=user_id,
            incident_id=incident_id,
            database_id=database_id,
            title_property=title_property,
            property_mapping=property_mapping,
            action_items_database_id=action_items_db,
        )
    except NotionAuthExpiredError:
        return jsonify({
            "code": "reauth_required",
            "error": "Notion credentials expired \u2014 please reconnect",
        }), 401
    except ValueError as exc:
        logger.warning(
            "[POSTMORTEM] Notion export rejected for user %s: %s", user_id, exc
        )
        safe_msg = (
            exc.args[0]
            if exc.args and isinstance(exc.args[0], str)
            else "Invalid export request"
        )
        return jsonify({"error": safe_msg}), 400
    except RuntimeError as exc:
        logger.exception(
            "[POSTMORTEM] Notion export partially failed for user %s: %s", user_id, exc
        )
        return jsonify({"error": "Notion page created but content write failed \u2014 check Notion and retry"}), 502
    except Exception as exc:
        logger.exception(
            "[POSTMORTEM] Notion export failed for user %s: %s", user_id, exc
        )
        return jsonify({"error": "Failed to export to Notion"}), 502

    return jsonify(result)


# ---------------------------------------------------------------------------
# Export to Jira
# ---------------------------------------------------------------------------


@postmortem_bp.route(
    "/api/incidents/<incident_id>/postmortem/export/jira", methods=["POST"]
)
@require_permission("postmortems", "write")
def export_to_jira(user_id, incident_id):
    """Export postmortem to Jira as a parent issue with subtasks for action items."""
    if not is_valid_uuid(incident_id):
        return jsonify({"error": "Invalid incident ID"}), 400

    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}

    project_key = data.get("projectKey")
    if not project_key:
        return jsonify({"error": "projectKey is required"}), 400

    issue_type = data.get("issueType", "Task")

    org_id = get_org_id_from_request()

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX)
                cursor.execute(
                    """SELECT id, content FROM artifacts
                       WHERE incident_id = %s AND org_id = %s AND category = %s""",
                    (incident_id, org_id, _CATEGORY),
                )
                row = cursor.fetchone()
    except Exception as e:
        logger.error(
            "[POSTMORTEM] Failed to fetch postmortem for Jira export, incident %s: %s",
            sanitize(incident_id),
            e,
        )
        return jsonify({"error": "Failed to fetch postmortem"}), 500

    if not row:
        return jsonify({"error": "Postmortem not found"}), 404

    artifact_id = str(row[0])
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

    if auth_type == "pat" and not base_url:
        return jsonify({"error": "Jira credentials incomplete: base_url required for PAT auth"}), 400

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

    parent_key = parent_result.get("key") if isinstance(parent_result, dict) else None
    parent_id = parent_result.get("id") if isinstance(parent_result, dict) else None
    if not parent_key or not parent_id:
        logger.error("[POSTMORTEM] Jira create_issue returned incomplete result: %s", parent_result)
        return jsonify({"error": "Jira issue created but response was incomplete"}), 502
    parent_url = f"{base_url}/browse/{parent_key}" if base_url else None

    action_items = extract_action_items(content)
    subtask_keys = []
    for item in action_items:
        if not item.get("text") or item.get("checked"):
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
                set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX)
                cursor.execute(
                    """INSERT INTO postmortem_exports
                           (postmortem_id, org_id, destination, external_id, external_key, external_url, exported_at)
                       VALUES (%s, %s, 'jira', %s, %s, %s, CURRENT_TIMESTAMP)
                       ON CONFLICT (postmortem_id, destination)
                       DO UPDATE SET external_id = EXCLUDED.external_id,
                                     external_key = EXCLUDED.external_key,
                                     external_url = EXCLUDED.external_url,
                                     exported_at = EXCLUDED.exported_at""",
                    (artifact_id, org_id, str(parent_id), parent_key, parent_url),
                )
                conn.commit()
    except Exception as e:
        logger.warning("[POSTMORTEM] Failed to update Jira metadata for artifact %s: %s", artifact_id, e)

    record_audit_event(org_id, user_id, "export_postmortem_jira", "postmortem", incident_id,
                       {"issue_key": parent_key, "issue_url": parent_url}, request)

    return jsonify({
        "success": True,
        "issueKey": parent_key,
        "issueId": str(parent_id),
        "issueUrl": parent_url,
        "subtasks": subtask_keys,
    })


# ---------------------------------------------------------------------------
# List all postmortems
# ---------------------------------------------------------------------------


@postmortem_bp.route("/api/postmortems", methods=["GET"])
@require_permission("postmortems", "read")
def list_postmortems(user_id):

    try:
        limit = min(int(request.args.get("limit", 50)), 100)
        offset = max(int(request.args.get("offset", 0)), 0)
    except (ValueError, TypeError):
        limit, offset = 50, 0

    org_id = get_org_id_from_request()

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX)

                cursor.execute(
                    """SELECT a.id, a.incident_id, a.user_id, a.content,
                              a.created_at, a.updated_at, i.alert_title
                       FROM artifacts a
                       LEFT JOIN incidents i ON a.incident_id = i.id
                       WHERE a.org_id = %s AND a.category = %s
                       ORDER BY a.created_at DESC
                       LIMIT %s OFFSET %s""",
                    (org_id, _CATEGORY, limit, offset),
                )
                artifact_rows = cursor.fetchall()

                # Batch-fetch exports for all artifacts in the result set
                artifact_ids = [str(r[0]) for r in artifact_rows]
                exports_map: Dict[str, Dict] = {aid: {} for aid in artifact_ids}

                if artifact_ids:
                    cursor.execute(
                        """SELECT postmortem_id, destination, external_id, external_key,
                                  external_url, external_database_id, exported_at
                           FROM postmortem_exports
                           WHERE postmortem_id = ANY(%s)""",
                        (artifact_ids,),
                    )
                    for erow in cursor.fetchall():
                        exports_map.setdefault(str(erow[0]), {})[erow[1]] = {
                            "external_id": erow[2],
                            "external_key": erow[3],
                            "external_url": erow[4],
                            "external_database_id": erow[5],
                            "exported_at": erow[6],
                        }

        postmortems = []
        for row in artifact_rows:
            aid = str(row[0])
            exp = exports_map.get(aid, {})
            confluence = exp.get("confluence", {})
            jira = exp.get("jira", {})
            notion = exp.get("notion", {})

            postmortems.append({
                "id": aid,
                "incidentId": str(row[1]),
                "incidentTitle": row[6],
                "content": row[3],
                "generatedAt": iso_utc(row[4]),
                "updatedAt": iso_utc(row[5]),
                "confluencePageId": confluence.get("external_id"),
                "confluencePageUrl": confluence.get("external_url"),
                "confluenceExportedAt": iso_utc(confluence.get("exported_at")),
                "jiraIssueId": jira.get("external_id"),
                "jiraIssueKey": jira.get("external_key"),
                "jiraIssueUrl": jira.get("external_url"),
                "jiraExportedAt": iso_utc(jira.get("exported_at")),
                "notionPageId": notion.get("external_id"),
                "notionPageUrl": notion.get("external_url"),
                "notionExportedAt": iso_utc(notion.get("exported_at")),
                "notionDatabaseId": notion.get("external_database_id"),
            })

        return jsonify({"postmortems": postmortems})

    except Exception as e:
        logger.error(
            "[POSTMORTEM] Failed to fetch postmortems for user %s: %s",
            user_id,
            e,
        )
        return jsonify({"error": "Failed to fetch postmortems"}), 500
