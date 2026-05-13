"""
GitLab connector routes — connect, status, disconnect.

Uses org-level Group Access Token authentication.
The token is stored once per org and shared across all users.
On connect, all accessible projects are auto-discovered and saved.
"""
import json
import logging
import requests
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


def _validate_gitlab_token(base_url: str, token: str) -> dict | None:
    """Validate a GitLab access token and return token metadata."""
    headers = {"PRIVATE-TOKEN": token}
    try:
        resp = requests.get(
            f"{base_url}/api/v4/personal_access_tokens/self",
            headers=headers,
            timeout=GITLAB_TIMEOUT,
        )
        if resp.status_code == 200:
            return resp.json()

        resp = requests.get(
            f"{base_url}/api/v4/user",
            headers=headers,
            timeout=GITLAB_TIMEOUT,
        )
        if resp.status_code == 200:
            return resp.json()
    except requests.RequestException as e:
        logger.error(f"GitLab token validation failed: {e}")
    return None


def _fetch_all_accessible_projects(base_url: str, token: str) -> list[dict]:
    """Fetch all projects accessible by the token (paginated)."""
    headers = {"PRIVATE-TOKEN": token}
    all_projects = []
    page = 1
    per_page = 100

    while True:
        try:
            resp = requests.get(
                f"{base_url}/api/v4/projects",
                headers=headers,
                params={
                    "membership": "true",
                    "order_by": "last_activity_at",
                    "sort": "desc",
                    "per_page": per_page,
                    "page": page,
                    "simple": "true",
                },
                timeout=GITLAB_TIMEOUT,
            )
            if resp.status_code != 200:
                logger.error(f"GitLab API error fetching projects: {resp.status_code}")
                break

            projects = resp.json()
            if not projects:
                break

            all_projects.extend(projects)
            if len(projects) < per_page:
                break
            page += 1
            if page > 50:
                logger.warning("Hit pagination safety limit for GitLab projects")
                break
        except requests.RequestException as e:
            logger.error(f"Error fetching GitLab projects page {page}: {e}")
            break

    return all_projects


def _auto_connect_projects(user_id: str, base_url: str, token: str) -> int:
    """Fetch all accessible projects and save them to connected_repos."""
    projects = _fetch_all_accessible_projects(base_url, token)
    if not projects:
        return 0

    org_id = None
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT org_id FROM users WHERE id = %s", (user_id,))
                row = cur.fetchone()
                org_id = row[0] if row else None
    except Exception as e:
        logger.warning(f"Could not fetch org_id for auto-connect: {e}")

    count = 0
    connected_names = []
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[gitlab:auto_connect]")

                cur.execute(
                    "DELETE FROM connected_repos WHERE user_id = %s AND provider = 'gitlab'",
                    (user_id,),
                )

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
                    connected_names.append(full_name)
                    count += 1

                conn.commit()
    except Exception as e:
        logger.error(f"Error auto-connecting GitLab projects: {e}", exc_info=True)

    # Trigger metadata generation for each connected project
    try:
        from utils.repo_metadata import generate_repo_metadata
        for name in connected_names:
            generate_repo_metadata.delay(user_id, "gitlab", name)
    except Exception as e:
        logger.warning(f"Could not queue metadata generation: {e}")

    logger.info(f"Auto-connected {count} GitLab projects for user {user_id}")
    return count


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

        token_info = _validate_gitlab_token(base_url, access_token)
        if not token_info:
            return jsonify({"error": "Invalid GitLab access token or unreachable instance"}), 400

        username = token_info.get("username") or token_info.get("name") or "gitlab-bot"
        token_name = token_info.get("name", "")
        scopes = token_info.get("scopes", [])

        gitlab_token_data = {
            "access_token": access_token,
            "base_url": base_url,
            "username": username,
            "token_name": token_name,
            "scopes": scopes,
        }

        store_tokens_in_db(user_id, gitlab_token_data, "gitlab")
        logger.info(f"Stored GitLab credentials for user {user_id} (org-level, token_name={token_name})")

        project_count = _auto_connect_projects(user_id, base_url, access_token)

        return jsonify({
            "success": True,
            "message": f"Connected to GitLab as {username} — {project_count} project(s) auto-connected",
            "username": username,
            "base_url": base_url,
            "projects_connected": project_count,
        })

    except Exception as e:
        logger.error(f"Error connecting GitLab: {e}", exc_info=True)
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
    except Exception as e:
        logger.error(f"Error checking GitLab status: {e}", exc_info=True)
        return jsonify({"connected": False})


@gitlab_bp.route("/disconnect", methods=["POST"])
@require_permission("connectors", "write")
def gitlab_disconnect(user_id):
    """Remove the org-level GitLab token and all connected projects."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[gitlab:disconnect]")
                cur.execute(
                    "DELETE FROM connected_repos WHERE user_id = %s AND provider = 'gitlab'",
                    (user_id,),
                )
                conn.commit()

        delete_user_secret(user_id, "gitlab")
        logger.info(f"Disconnected GitLab for user {user_id}")
        return jsonify({"success": True, "message": "GitLab disconnected"})
    except Exception as e:
        logger.error(f"Error disconnecting GitLab: {e}", exc_info=True)
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
        logger.error(f"Error getting GitLab repo selections: {e}", exc_info=True)
        return jsonify({"error": "Failed to get project selections"}), 500
