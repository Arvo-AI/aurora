"""Jira-specific API routes: search, issue CRUD, comments, links, settings."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint, jsonify, request

from connectors.atlassian_auth.auth import refresh_access_token
from connectors.jira_connector.client import JiraClient
from connectors.jira_connector.adf_converter import markdown_to_adf, text_to_adf
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_user_preference, resolve_org_id, set_rls_context
from utils.db.db_utils import connect_to_db_as_user
from utils.web.public_url import external_backend_url
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

@jira_bp.route("/search", methods=["POST"])
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

@jira_bp.route("/issue/<issue_key>", methods=["GET"])
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

@jira_bp.route("/issue", methods=["POST"])
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

@jira_bp.route("/issue/<issue_key>", methods=["PATCH"])
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

@jira_bp.route("/issue/<issue_key>/comment", methods=["POST"])
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

@jira_bp.route("/issue/link", methods=["POST"])
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
JIRA_COMMENT_BACK_KEY = "jira_comment_back"
JIRA_RCA_RULES_KEY = "jira_rca_rules"
VALID_MODES = ("full", "comment_only")
# Jira project keys are a letter plus letters, digits, or underscores.
_PROJECT_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
_MAX_RCA_RULES = 50
_MAX_ISSUE_TYPES = 30


def _parse_rule_item(item: Any) -> Tuple[Optional[str], Optional[list], Optional[str]]:
    """Pull a project key and type list out of one rule, or an error."""
    if not isinstance(item, dict):
        return None, None, "Each rule needs a projectKey and issueTypes"
    project_key = str(item.get("projectKey") or "").strip().upper()
    # Reject anything that is not a project key so a payload cannot widen the match.
    if not _PROJECT_KEY_RE.fullmatch(project_key):
        return None, None, "projectKey must look like a Jira project key"
    types_raw = item.get("issueTypes")
    if not isinstance(types_raw, list) or not types_raw:
        return None, None, "issueTypes must list at least one ticket type"
    return project_key, types_raw, None


def _clean_issue_type_name(type_name: Any) -> Tuple[Optional[str], Optional[str]]:
    if not isinstance(type_name, str):
        return None, "issueTypes must be ticket type names"
    # Collapse whitespace so "Bug" and " Bug " are the same type.
    cleaned = " ".join(type_name.split())
    if not cleaned or len(cleaned) > 80:
        return None, "Each issue type name must be 1–80 characters"
    return cleaned, None


def _merge_issue_types(
    existing: Optional[List[str]],
    types_raw: list,
) -> Tuple[Optional[List[str]], Optional[str]]:
    seen: Dict[str, str] = {}
    # A repeated project in one payload merges into the earlier row.
    if existing:
        for name in existing:
            seen[name.casefold()] = name
    for type_name in types_raw:
        cleaned, error = _clean_issue_type_name(type_name)
        if error or cleaned is None:
            return None, error
        folded = cleaned.casefold()
        # Keep the first spelling when the same type is listed twice.
        if folded not in seen:
            seen[folded] = cleaned
    if len(seen) > _MAX_ISSUE_TYPES:
        return None, f"At most {_MAX_ISSUE_TYPES} ticket types per project"
    return list(seen.values()), None


def normalize_rca_rules(raw: Any) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Validate project × ticket-type rules. Empty list means nothing auto-triggers."""
    if not isinstance(raw, list):
        return None, "rcaRules must be a list"
    if len(raw) > _MAX_RCA_RULES:
        return None, f"At most {_MAX_RCA_RULES} projects can be configured"

    by_key: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for item in raw:
        project_key, types_raw, error = _parse_rule_item(item)
        if error or project_key is None or types_raw is None:
            return None, error
        names, error = _merge_issue_types(
            by_key[project_key]["issueTypes"] if project_key in by_key else None,
            types_raw,
        )
        if error or names is None:
            return None, error
        # First time we see this project, keep the payload order.
        if project_key not in by_key:
            order.append(project_key)
        by_key[project_key] = {"projectKey": project_key, "issueTypes": names}

    return [by_key[key] for key in order], None


def get_jira_rca_rules(user_id: str, *, strict: bool = False) -> List[Dict[str, Any]]:
    """Stored automatic-RCA rules. A missing or corrupt value triggers nothing."""
    stored = get_user_preference(user_id, JIRA_RCA_RULES_KEY, default=[], strict=strict)
    if not isinstance(stored, list):
        return []
    return stored


def extract_webhook_scope(issue: Dict[str, Any]) -> Tuple[str, str]:
    """Project key and ticket type from a Jira webhook issue."""
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    project = fields.get("project") if isinstance(fields.get("project"), dict) else {}
    project_key = str(project.get("key") or "").strip()
    # Some automation bodies send only the issue key (PROJ-123).
    if not project_key:
        issue_key = str(issue.get("key") or "")
        head, sep, tail = issue_key.rpartition("-")
        if sep and head and tail.isdigit():
            project_key = head
    issuetype = fields.get("issuetype") if isinstance(fields.get("issuetype"), dict) else {}
    return project_key, str(issuetype.get("name") or "").strip()


def rca_rule_matches(project_key: str, issue_type: str, rules: Any) -> bool:
    """True when this project and ticket type were explicitly opted in."""
    if not project_key or not issue_type or not isinstance(rules, list):
        return False
    project_folded = project_key.strip().casefold()
    type_folded = issue_type.strip().casefold()
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        rule_key = str(rule.get("projectKey") or "").strip().casefold()
        if rule_key != project_folded:
            continue
        types = rule.get("issueTypes") or []
        if any(str(name).strip().casefold() == type_folded for name in types if isinstance(name, str)):
            return True
    return False


def jira_comment_back_enabled(user_id: str) -> bool:
    """True only when the org opted in. Unset stays off, unlike the old jira_mode default."""
    # Anything other than an explicit true stays off.
    return get_user_preference(user_id, JIRA_COMMENT_BACK_KEY, default=False) is True


def _store_jira_settings(
    user_id: str,
    *,
    mode: Optional[str],
    comment_back: Optional[bool],
    rca_rules: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    """Write all supplied Jira prefs in one transaction."""
    if mode is None and comment_back is None and rca_rules is None:
        return True

    conn = None
    cursor = None
    try:
        conn = connect_to_db_as_user()
        cursor = conn.cursor()
        org_id = set_rls_context(cursor, conn, user_id, log_prefix="[JIRA:Settings]")
        if not org_id:
            return False

        updates: List[Tuple[str, Any]] = []
        if mode is not None:
            updates.append((JIRA_MODE_KEY, mode))
        if comment_back is not None:
            updates.append((JIRA_COMMENT_BACK_KEY, comment_back))
        # An empty list is a real value: it turns automatic RCA off.
        if rca_rules is not None:
            updates.append((JIRA_RCA_RULES_KEY, rca_rules))

        for key, value in updates:
            cursor.execute(
                "DELETE FROM user_preferences WHERE org_id = %s AND preference_key = %s",
                (org_id, key),
            )
            cursor.execute(
                """
                INSERT INTO user_preferences (user_id, org_id, preference_key, preference_value)
                VALUES (%s, %s, %s, %s)
                """,
                (user_id, org_id, key, json.dumps(value)),
            )
        conn.commit()
        return True
    except Exception:
        logger.exception("[JIRA] Failed to store settings for user %s", sanitize(user_id))
        if conn:
            conn.rollback()
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def _settings_payload(user_id: str) -> Dict[str, Any]:
    mode = get_user_preference(user_id, JIRA_MODE_KEY, default="comment_only")
    return {
        "jiraMode": mode,
        "commentBack": jira_comment_back_enabled(user_id),
        "rcaRules": get_jira_rca_rules(user_id),
    }


@jira_bp.route("/settings", methods=["GET"])
@require_permission("connectors", "read")
def get_settings(user_id):
    return jsonify(_settings_payload(user_id))


# ------------------------------------------------------------------
# PUT /jira/settings
# ------------------------------------------------------------------

@jira_bp.route("/settings", methods=["PUT"])
@require_permission("connectors", "write")
def update_settings(user_id):
    data = request.get_json(force=True, silent=True) or {}
    mode = data.get("jiraMode")
    comment_back = data.get("commentBack")
    has_rules = "rcaRules" in data

    # Each control saves on its own — mode, comment-back, and RCA rules.
    if mode is None and comment_back is None and not has_rules:
        return jsonify({"error": "jiraMode, commentBack, or rcaRules is required"}), 400

    if mode is not None and mode not in VALID_MODES:
        return jsonify({"error": f"jiraMode must be one of: {', '.join(VALID_MODES)}"}), 400
    if comment_back is not None and not isinstance(comment_back, bool):
        return jsonify({"error": "commentBack must be a boolean"}), 400

    rca_rules = None
    if has_rules:
        rca_rules, rules_error = normalize_rca_rules(data.get("rcaRules"))
        if rules_error:
            return jsonify({"error": rules_error}), 400

    if not _store_jira_settings(
        user_id, mode=mode, comment_back=comment_back, rca_rules=rca_rules,
    ):
        return jsonify({"error": "Failed to store Jira settings"}), 500

    # Jira prefs are org-scoped — invalidate every member's cached tool list.
    from chat.backend.agent.tools.mcp_tools import (
        clear_credentials_cache,
        clear_credentials_cache_for_org,
    )

    org_id = resolve_org_id(user_id)
    if org_id:
        clear_credentials_cache_for_org(org_id)
    else:
        clear_credentials_cache(user_id)

    logger.info(
        "[JIRA] Updated settings for user %s: jiraMode=%s commentBack=%s rcaRules=%s",
        sanitize(user_id),
        sanitize(mode),
        sanitize(comment_back),
        sanitize(len(rca_rules) if rca_rules is not None else None),
    )

    payload = _settings_payload(user_id)
    payload["success"] = True
    return jsonify(payload)


# ------------------------------------------------------------------
# GET /jira/status
# ------------------------------------------------------------------

@jira_bp.route("/status", methods=["GET"])
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

@jira_bp.route("/disconnect", methods=["POST", "DELETE"])
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


# ------------------------------------------------------------------
# GET /jira/projects — projects the connected account can see
# ------------------------------------------------------------------

@jira_bp.route("/projects", methods=["GET"])
@require_permission("connectors", "read")
def list_jira_projects(user_id):
    """Project keys for the automatic-RCA picker."""
    client, creds, error = _get_jira_client(user_id)
    if error:
        return jsonify({"error": error}), 404 if not creds else 400

    try:
        projects = _with_refresh(user_id, creds, client, lambda c: c.list_projects())
    except Exception:
        logger.exception("[JIRA] List projects failed for user %s", sanitize(user_id))
        return jsonify({"error": "Failed to list Jira projects"}), 502

    slim = []
    for project in projects or []:
        key = str(project.get("key") or "").strip()
        # A project without a key cannot be selected or matched later.
        if not key:
            continue
        slim.append({"key": key, "name": project.get("name") or key})
    return jsonify({"projects": slim})


# ------------------------------------------------------------------
# GET /jira/projects/<project_key>/issue-types
# ------------------------------------------------------------------

@jira_bp.route("/projects/<project_key>/issue-types", methods=["GET"])
@require_permission("connectors", "read")
def list_project_issue_types(user_id, project_key):
    """Ticket types in one project, for the automatic-RCA picker."""
    if not _PROJECT_KEY_RE.fullmatch(project_key or ""):
        return jsonify({"error": "projectKey must look like a Jira project key"}), 400

    client, creds, error = _get_jira_client(user_id)
    if error:
        return jsonify({"error": error}), 404 if not creds else 400

    try:
        types = _with_refresh(
            user_id, creds, client, lambda c: c.get_issue_types(project_key),
        )
    except Exception:
        logger.exception("[JIRA] List issue types failed for user %s", sanitize(user_id))
        return jsonify({"error": "Failed to list Jira issue types"}), 502

    names = []
    seen = set()
    for issue_type in types or []:
        name = str((issue_type or {}).get("name") or "").strip()
        folded = name.casefold()
        # Blank or repeated names are not distinct ticket types to opt into.
        if not name or folded in seen:
            continue
        seen.add(folded)
        names.append(name)
    return jsonify({"projectKey": project_key, "issueTypes": names})


# ------------------------------------------------------------------
# POST /jira/webhook/<user_id> — Jira automation webhook receiver
# ------------------------------------------------------------------

@jira_bp.route("/webhook/<user_id>", methods=["POST"])
def webhook(user_id: str):
    """Receive a Jira webhook and trigger Aurora's RCA pipeline.

    Configure in Jira under Settings > System > Webhooks, or via
    Jira Automation rules that POST to this URL on issue creation.
    Accepts standard Jira webhook payloads (issue_created, issue_updated).
    """
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400

    # Guard: only accept webhooks for users who actually have Jira connected.
    # Without this, any POST to /jira/webhook/<user_id> would enqueue Celery
    # work and dispatch an LLM-backed RCA for any (guessable) user_id — the
    # same connector-existence check the PagerDuty/OpsGenie handlers enforce.
    if not get_token_data(user_id, "jira"):
        logger.warning("[JIRA][WEBHOOK] Webhook for user %s with no Jira connection", sanitize(user_id))
        return jsonify({"error": "Jira not connected for this user"}), 404

    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"error": "Invalid or missing JSON payload"}), 400

    issue = payload.get("issue", {})

    if not issue:
        return jsonify({"status": "ignored", "reason": "no issue in payload"}), 200

    # Automatic RCA is opt-in. No saved project × type rule means ignore the webhook.
    # Asking Aurora to investigate a named ticket does not go through this path.
    project_key, issue_type = extract_webhook_scope(issue)
    try:
        rules = get_jira_rca_rules(user_id, strict=True)
    except Exception:
        # A failed read must not look like "no rules" — Jira will retry a 503.
        logger.exception("[JIRA][WEBHOOK] Could not read RCA rules for user %s", sanitize(user_id))
        return jsonify({"status": "error", "reason": "could not read RCA rules"}), 503
    if not rca_rule_matches(project_key, issue_type, rules):
        logger.info(
            "[JIRA][WEBHOOK] Ignored %s (project=%s type=%s): not configured for automatic RCA",
            sanitize(issue.get("key", "?")),
            sanitize(project_key or "?"),
            sanitize(issue_type or "?"),
        )
        return jsonify({"status": "ignored", "reason": "project and issue type not configured for RCA"}), 200

    from routes.jira.tasks import process_jira_webhook
    try:
        process_jira_webhook.delay(payload=payload, user_id=user_id)
    except Exception:
        # Broker/registration failure is on our side, not the sender's — return
        # 503 so Jira retries rather than treating it as a malformed webhook (500).
        logger.exception("[JIRA][WEBHOOK] Failed to enqueue task for user %s (issue=%s)",
                         sanitize(user_id), sanitize(issue.get("key", "?")))
        return jsonify({"status": "error", "reason": "could not enqueue webhook for processing"}), 503

    return jsonify({"status": "accepted", "issue": issue.get("key", "unknown")}), 202


# ------------------------------------------------------------------
# GET /jira/webhook-url — returns the webhook URL for this user
# ------------------------------------------------------------------

@jira_bp.route("/webhook-url", methods=["GET"])
@require_permission("connectors", "read")
def get_webhook_url(user_id):
    """Return the Jira webhook URL for the authenticated user."""
    import os
    # NEXT_PUBLIC_BACKEND_URL can be a cluster-internal address that Jira
    # can't reach. Prefer the ngrok tunnel in local dev, then a public URL.
    backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL", "http://localhost:5080").rstrip("/")
    public_url = os.getenv("PUBLIC_API_URL", "").rstrip("/")
    tunneled = external_backend_url(backend_url)
    # Ngrok replaces localhost; otherwise prefer an explicit public API URL.
    base_url = tunneled if tunneled != backend_url else (public_url or backend_url)
    url = f"{base_url}/jira/webhook/{user_id}"
    # These are the recommended Jira events to subscribe the webhook to — not a
    # server-enforced allowlist. The handler filters by project and issue type.
    return jsonify({"webhook_url": url, "recommended_events": ["jira:issue_created", "jira:issue_updated"]})
