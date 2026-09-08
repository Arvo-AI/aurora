"""
GitLab connector routes — connect, status, disconnect.

Uses org-level Group Access Token authentication.
The token is stored once per org and shared across all users.
On connect, all accessible projects are auto-discovered and saved.
"""
import json
import logging
import requests
from typing import Optional
from urllib.parse import urlparse
from flask import Blueprint, request, jsonify
from utils.auth.rbac_decorators import require_permission
from utils.auth.token_management import store_tokens_in_db
from utils.auth.stateless_auth import get_credentials_from_db, set_rls_context
from utils.secrets.secret_ref_utils import delete_user_secret
from utils.db.connection_pool import db_pool

gitlab_bp = Blueprint("gitlab", __name__)
logger = logging.getLogger(__name__)

GITLAB_TIMEOUT = 20
DEFAULT_GITLAB_URL = "https://gitlab.com"

def _parse_next_link(link_header: str) -> str | None:
    """Extract the next URL from a Link header without regex (avoids ReDoS)."""
    for part in link_header.split(","):
        if 'rel="next"' in part:
            start = part.find("<")
            end = part.find(">", start)
            if start != -1 and end != -1:
                return part[start + 1:end]
    return None


def _is_valid_gitlab_base_url(url: str) -> bool:
    """Validate that a URL is a well-formed http(s) URL."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _validate_gitlab_token(base_url: str, token: str) -> tuple[Optional[dict], str]:
    """
    Validate a GitLab access token and return (token_metadata, source).

    source is 'token_api' if /personal_access_tokens/self succeeded,
    or 'user_api' if we fell back to /user (less reliable for scopes/name).
    Returns (None, '') on failure.
    """
    headers = {"PRIVATE-TOKEN": token}
    try:
        resp = requests.get(
            f"{base_url}/api/v4/personal_access_tokens/self",
            headers=headers,
            timeout=GITLAB_TIMEOUT,
        )
        if resp.status_code == 200:
            return resp.json(), "token_api"

        resp = requests.get(
            f"{base_url}/api/v4/user",
            headers=headers,
            timeout=GITLAB_TIMEOUT,
        )
        if resp.status_code == 200:
            logger.warning("GitLab token validated via /user fallback — scopes/token name unavailable")
            return resp.json(), "user_api"
    except requests.RequestException:
        logger.exception("GitLab token validation request failed")
    return None, ""


def _fetch_all_accessible_projects(base_url: str, token: str) -> tuple[list[dict], Optional[str]]:
    """
    Fetch all projects accessible by the token using Link-header pagination.

    Returns (projects, warning) where warning is set if pagination was truncated.
    """
    headers = {"PRIVATE-TOKEN": token}
    all_projects: list[dict] = []
    url: Optional[str] = f"{base_url}/api/v4/projects"
    params = {
        "membership": "true",
        "order_by": "last_activity_at",
        "sort": "desc",
        "per_page": 100,
        "simple": "true",
    }
    max_pages = 50
    page_count = 0
    warning: Optional[str] = None

    while url:
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=GITLAB_TIMEOUT)
            params = None  # Only use params on the first request; subsequent use Link URL

            if resp.status_code != 200:
                logger.error("GitLab API error fetching projects: %d", resp.status_code)
                break

            projects = resp.json()
            if not projects:
                break

            all_projects.extend(projects)
            page_count += 1

            if page_count >= max_pages:
                warning = "Pagination limit reached; some projects may not have been discovered."
                logger.warning(warning)
                break

            # Follow Link: <...>; rel="next" header
            link_header = resp.headers.get("Link", "")
            url = _parse_next_link(link_header)

        except requests.RequestException:
            logger.exception("Error fetching GitLab projects (page %d)", page_count + 1)
            break

    return all_projects, warning


def _auto_connect_projects(user_id: str, base_url: str, token: str) -> tuple[int, Optional[str]]:
    """
    Fetch all accessible projects and save them to connected_repos.

    Returns (count, error_message). error_message is None on full success.
    """
    projects, pagination_warning = _fetch_all_accessible_projects(base_url, token)
    if not projects:
        if pagination_warning:
            return 0, pagination_warning
        return 0, None

    org_id = None
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT org_id FROM users WHERE id = %s", (user_id,))
                row = cur.fetchone()
                org_id = row[0] if row else None
    except Exception as e:
        logger.warning("Could not fetch org_id for auto-connect: %s", e)

    count = 0
    connected_names: list[str] = []
    commit_succeeded = False
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[gitlab:auto_connect]")

                cur.execute(
                    "DELETE FROM connected_repos WHERE user_id = %s AND provider = 'gitlab'",
                    (user_id,),
                )

                staged_names: list[str] = []
                for proj in projects:
                    full_name = proj.get("path_with_namespace", "")
                    if not full_name:
                        continue
                    cur.execute(
                        """INSERT INTO connected_repos
                               (user_id, org_id, provider, repo_full_name, repo_id,
                                default_branch, is_private, repo_data, metadata_status)
                           VALUES (%s, %s, 'gitlab', %s, %s, %s, %s, %s, 'pending')
                           ON CONFLICT (user_id, provider, repo_full_name) DO UPDATE SET
                               repo_data = EXCLUDED.repo_data,
                               default_branch = EXCLUDED.default_branch,
                               is_private = EXCLUDED.is_private,
                               updated_at = NOW()""",
                        (
                            user_id,
                            org_id,
                            full_name,
                            proj.get("id"),
                            proj.get("default_branch", "main"),
                            proj.get("visibility", "private") == "private",
                            json.dumps(proj),
                        ),
                    )
                    staged_names.append(full_name)

                conn.commit()
                commit_succeeded = True
                connected_names = staged_names
                count = len(staged_names)
    except Exception as e:
        logger.exception("Error auto-connecting GitLab projects")
        return 0, f"Database error: {type(e).__name__}"

    # Trigger metadata generation only for successfully committed projects
    if commit_succeeded and connected_names:
        try:
            from utils.repo_metadata import generate_repo_metadata
            for name in connected_names:
                generate_repo_metadata.delay(user_id, "gitlab", name)
        except Exception as e:
            logger.warning("Could not queue metadata generation: %s", e)

    logger.info("Auto-connected %d GitLab projects for user %s", count, user_id)
    return count, pagination_warning


@gitlab_bp.route("/connect", methods=["POST"])
@require_permission("connectors", "write")
def gitlab_connect(user_id):
    """Store an org-level GitLab Group Access Token and auto-connect all accessible projects."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Request body is required"}), 400

        access_token = data.get("access_token", "").strip()
        base_url = (data.get("base_url") or DEFAULT_GITLAB_URL).rstrip("/")

        if not access_token:
            return jsonify({"error": "access_token is required"}), 400
        if not _is_valid_gitlab_base_url(base_url):
            return jsonify({"error": "Invalid base_url; must be an http(s) URL"}), 400

        token_info, validation_source = _validate_gitlab_token(base_url, access_token)
        if not token_info:
            return jsonify({"error": "Invalid GitLab access token or unreachable instance"}), 400

        # When validated via /user fallback, fields like scopes/token_name are unavailable
        if validation_source == "token_api":
            username = token_info.get("username") or token_info.get("name") or "gitlab-bot"
            token_name = token_info.get("name", "")
            scopes = token_info.get("scopes", [])
        else:
            username = token_info.get("username") or token_info.get("name") or "gitlab-bot"
            token_name = ""
            scopes = []

        gitlab_token_data = {
            "access_token": access_token,
            "base_url": base_url,
            "username": username,
            "token_name": token_name,
            "scopes": scopes,
            "validation_source": validation_source,
        }

        store_tokens_in_db(user_id, gitlab_token_data, "gitlab")
        logger.info("Stored GitLab credentials for user %s (org-level)", user_id)

        try:
            from utils.auth.tool_registry import seed_org_tool_permissions
            from utils.auth.stateless_auth import get_org_id_for_user
            org_id = get_org_id_for_user(user_id)
            if org_id:
                seed_org_tool_permissions(org_id, user_id)
        except Exception:
            logger.warning("[GITLAB-CONNECT] failed to seed tool permissions", exc_info=True)

        project_count, connect_warning = _auto_connect_projects(user_id, base_url, access_token)

        response = {
            "success": True,
            "message": f"Connected to GitLab as {username} — {project_count} project(s) auto-connected",
            "username": username,
            "base_url": base_url,
            "projects_connected": project_count,
        }
        if connect_warning:
            response["warnings"] = [connect_warning]

        return jsonify(response)

    except Exception as e:
        logger.exception("Error connecting GitLab")
        return jsonify({"error": "Failed to connect GitLab"}), 500


@gitlab_bp.route("/status", methods=["GET"])
@require_permission("connectors", "read")
def gitlab_status(user_id):
    """Check if an org-level GitLab token is configured."""
    try:
        creds = get_credentials_from_db(user_id, "gitlab")
        if creds and creds.get("access_token"):
            return jsonify({
                "connected": True,
                "username": creds.get("username", ""),
                "base_url": creds.get("base_url", DEFAULT_GITLAB_URL),
                "token_name": creds.get("token_name", ""),
            })
        return jsonify({"connected": False})
    except Exception:
        logger.exception("Error checking GitLab status")
        return jsonify({"connected": False})


@gitlab_bp.route("/disconnect", methods=["POST"])
@require_permission("connectors", "write")
def gitlab_disconnect(user_id):
    """Remove the org-level GitLab token and all connected projects."""
    try:
        delete_success = delete_user_secret(user_id, "gitlab")
        if not delete_success:
            logger.error("Failed to delete GitLab secret for user %s", user_id)
            return jsonify({"error": "Failed to disconnect GitLab"}), 500

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[gitlab:disconnect]")
                cur.execute(
                    "DELETE FROM connected_repos WHERE user_id = %s AND provider = 'gitlab'",
                    (user_id,),
                )
                conn.commit()

        logger.info("Disconnected GitLab for user %s", user_id)
        return jsonify({"success": True, "message": "GitLab disconnected"})
    except Exception as e:
        logger.exception("Error disconnecting GitLab")
        return jsonify({"error": "Failed to disconnect GitLab"}), 500


@gitlab_bp.route("/repo-selections", methods=["GET"])
@require_permission("connectors", "read")
def get_repo_selections(user_id):
    """Return all auto-connected GitLab projects for this user/org."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[gitlab:repo_selections]")
                cur.execute(
                    """SELECT repo_full_name, repo_id, default_branch, is_private,
                              metadata_summary, metadata_status, created_at
                       FROM connected_repos
                       WHERE user_id = %s AND provider = 'gitlab'
                       ORDER BY repo_full_name""",
                    (user_id,),
                )
                rows = cur.fetchall()

        repos = [
            {
                "repo_full_name": r[0],
                "repo_id": r[1],
                "default_branch": r[2],
                "is_private": r[3],
                "metadata_summary": r[4],
                "metadata_status": r[5],
                "created_at": r[6].isoformat() if r[6] else None,
            }
            for r in rows
        ]
        return jsonify({"repositories": repos})
    except Exception as e:
        logger.exception("Error getting GitLab repo selections")
        return jsonify({"error": "Failed to get project selections"}), 500


@gitlab_bp.route("/repo-selections/<path:repo_full_name>/metadata", methods=["PUT"])
@require_permission("connectors", "write")
def update_repo_metadata(user_id, repo_full_name):
    """Update the metadata summary for a specific GitLab project (human edit)."""
    try:
        data = request.get_json()
        summary = data.get("metadata_summary") if data else None
        if summary is None:
            return jsonify({"error": "metadata_summary is required"}), 400

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[gitlab:update_metadata]")
                cur.execute(
                    """UPDATE connected_repos
                       SET metadata_summary = %s, metadata_status = 'ready', updated_at = NOW()
                       WHERE provider = 'gitlab' AND repo_full_name = %s""",
                    (summary, repo_full_name),
                )
                conn.commit()
        return jsonify({"message": "Metadata updated"})
    except Exception as e:
        logger.exception("Error updating GitLab repo metadata")
        return jsonify({"error": "Failed to update metadata"}), 500


@gitlab_bp.route("/repo-metadata/generate", methods=["POST"])
@require_permission("connectors", "write")
def trigger_metadata_generation(user_id):
    """Trigger LLM metadata generation for a specific GitLab project."""
    try:
        data = request.get_json()
        repo_full_name = data.get("repo_full_name") if data else None
        if not repo_full_name:
            return jsonify({"error": "repo_full_name is required"}), 400

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[gitlab:trigger_metadata]")
                cur.execute(
                    """UPDATE connected_repos SET metadata_status = 'generating', updated_at = NOW()
                       WHERE provider = 'gitlab' AND repo_full_name = %s""",
                    (repo_full_name,),
                )
                conn.commit()

        from utils.repo_metadata import generate_repo_metadata
        try:
            generate_repo_metadata.delay(user_id, "gitlab", repo_full_name)
        except Exception as e:
            logger.error("Failed to enqueue metadata gen for %s: %s", repo_full_name, e)
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cur:
                    set_rls_context(cur, conn, user_id, log_prefix="[gitlab:trigger_metadata]")
                    cur.execute(
                        "UPDATE connected_repos SET metadata_status = 'pending', updated_at = NOW() WHERE provider = 'gitlab' AND repo_full_name = %s",
                        (repo_full_name,),
                    )
                    conn.commit()
            return jsonify({"error": "Failed to start metadata generation"}), 500
        return jsonify({"message": "Metadata generation started"})
    except Exception as e:
        logger.exception("Error triggering GitLab metadata generation")
        return jsonify({"error": "Failed to trigger metadata generation"}), 500
