"""
GitHub multi-repo selection endpoints.
Manages which repos a user has connected for RCA investigation.
"""
import logging
import json
from flask import Blueprint, jsonify, request
from utils.db.connection_pool import db_pool
from utils.auth.rbac_decorators import require_permission

github_repo_selection_bp = Blueprint('github_repo_selection', __name__)
logger = logging.getLogger(__name__)


def _get_user_org_id(user_id: str) -> str | None:
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT org_id FROM users WHERE id = %s", (user_id,))
                row = cur.fetchone()
                return row[0] if row else None
    except Exception as e:
        logger.warning(f"Error fetching org_id for user {user_id}: {e}")
        return None


def _update_metadata_status(user_id: str, repo_full_name: str, status: str):
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE github_connected_repos SET metadata_status = %s, updated_at = NOW() WHERE user_id = %s AND repo_full_name = %s",
                    (status, user_id, repo_full_name),
                )
                conn.commit()
    except Exception as e:
        logger.warning(f"Failed to revert metadata_status for {repo_full_name}: {e}")


@github_repo_selection_bp.route("/repo-selections", methods=["GET"])
@require_permission("connectors", "read")
def get_repo_selections(user_id):
    """Return all connected repos with metadata for this user."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT repo_full_name, repo_id, default_branch, is_private,
                              metadata_summary, metadata_status, repo_data, created_at
                       FROM github_connected_repos
                       WHERE user_id = %s ORDER BY repo_full_name""",
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
                "repo_data": r[6],
                "created_at": r[7].isoformat() if r[7] else None,
            }
            for r in rows
        ]
        return jsonify({"repositories": repos})
    except Exception as e:
        logger.error(f"Error getting repo selections: {e}", exc_info=True)
        return jsonify({"error": "Failed to get repository selections"}), 500


@github_repo_selection_bp.route("/repo-selections", methods=["POST"])
@require_permission("connectors", "write")
def save_repo_selections(user_id):
    """Sync the set of connected repos. Upserts new, removes deselected, triggers metadata gen."""
    try:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Request body must be a JSON object"}), 400
        repositories = data.get("repositories")
        if not isinstance(repositories, list):
            return jsonify({"error": "repositories array is required"}), 400

        org_id = _get_user_org_id(user_id)

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT repo_full_name FROM github_connected_repos WHERE user_id = %s",
                    (user_id,),
                )
                existing = {r[0] for r in cur.fetchall()}

                incoming = set()
                newly_added = []

                for repo in repositories:
                    if not isinstance(repo, dict):
                        return jsonify({"error": "Each repository must be an object"}), 400
                    full_name = repo.get("full_name")
                    if not full_name:
                        continue
                    incoming.add(full_name)

                    cur.execute(
                        """INSERT INTO github_connected_repos
                               (user_id, org_id, repo_full_name, repo_id, default_branch,
                                is_private, repo_data, metadata_status)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')
                           ON CONFLICT (user_id, repo_full_name) DO UPDATE SET
                               repo_data = EXCLUDED.repo_data,
                               default_branch = EXCLUDED.default_branch,
                               is_private = EXCLUDED.is_private,
                               updated_at = NOW()""",
                        (
                            user_id,
                            org_id,
                            full_name,
                            repo.get("id"),
                            repo.get("default_branch"),
                            repo.get("private", False),
                            json.dumps(repo),
                        ),
                    )
                    if full_name not in existing:
                        newly_added.append(full_name)

                if repositories and not incoming:
                    return jsonify({"error": "No valid repositories in request (all missing full_name)"}), 400

                removed = existing - incoming
                if removed:
                    cur.execute(
                        "DELETE FROM github_connected_repos WHERE user_id = %s AND repo_full_name = ANY(%s)",
                        (user_id, list(removed)),
                    )

                conn.commit()

        # Fire metadata generation for newly added repos
        for repo_name in newly_added:
            try:
                from routes.github.github_repo_metadata import generate_repo_metadata
                generate_repo_metadata.delay(user_id, repo_name)
            except Exception as e:
                logger.warning(f"Failed to enqueue metadata gen for {repo_name}: {e}")
                _update_metadata_status(user_id, repo_name, "error")

        return jsonify({
            "message": f"Saved {len(incoming)} repos, removed {len(removed)}, generating metadata for {len(newly_added)}",
            "added": newly_added,
            "removed": list(removed),
        })
    except Exception as e:
        logger.error(f"Error saving repo selections: {e}", exc_info=True)
        return jsonify({"error": "Failed to save repository selections"}), 500


@github_repo_selection_bp.route("/repo-selections", methods=["DELETE"])
@require_permission("connectors", "write")
def clear_repo_selections(user_id):
    """Remove all connected repos for a user."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM github_connected_repos WHERE user_id = %s", (user_id,))
                conn.commit()
        return jsonify({"message": "All repository selections cleared"})
    except Exception as e:
        logger.error(f"Error clearing repo selections: {e}", exc_info=True)
        return jsonify({"error": "Failed to clear repository selections"}), 500


@github_repo_selection_bp.route("/repo-selections/<path:repo_full_name>/metadata", methods=["PUT"])
@require_permission("connectors", "write")
def update_repo_metadata(user_id, repo_full_name):
    """Update the metadata summary for a specific repo (human edit)."""
    try:
        data = request.get_json()
        summary = data.get("metadata_summary") if data else None
        if summary is None:
            return jsonify({"error": "metadata_summary is required"}), 400

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE github_connected_repos
                       SET metadata_summary = %s, metadata_status = 'ready', updated_at = NOW()
                       WHERE user_id = %s AND repo_full_name = %s""",
                    (summary, user_id, repo_full_name),
                )
                if cur.rowcount == 0:
                    return jsonify({"error": "Repository not found"}), 404
                conn.commit()
        return jsonify({"message": "Metadata updated"})
    except Exception as e:
        logger.error(f"Error updating repo metadata: {e}", exc_info=True)
        return jsonify({"error": "Failed to update metadata"}), 500


@github_repo_selection_bp.route("/repo-metadata/generate", methods=["POST"])
@require_permission("connectors", "write")
def trigger_metadata_generation(user_id):
    """Trigger LLM metadata generation for a specific repo."""
    try:
        data = request.get_json()
        repo_full_name = data.get("repo_full_name") if data else None
        if not repo_full_name:
            return jsonify({"error": "repo_full_name is required"}), 400

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE github_connected_repos SET metadata_status = 'generating', updated_at = NOW()
                       WHERE user_id = %s AND repo_full_name = %s""",
                    (user_id, repo_full_name),
                )
                if cur.rowcount == 0:
                    return jsonify({"error": "Repository not found"}), 404
                conn.commit()

        from routes.github.github_repo_metadata import generate_repo_metadata
        try:
            generate_repo_metadata.delay(user_id, repo_full_name)
        except Exception as e:
            logger.error(f"Failed to enqueue metadata gen for {repo_full_name}: {e}")
            _update_metadata_status(user_id, repo_full_name, "pending")
            return jsonify({"error": "Failed to start metadata generation"}), 500
        return jsonify({"message": "Metadata generation started"})
    except Exception as e:
        logger.error(f"Error triggering metadata generation: {e}", exc_info=True)
        return jsonify({"error": "Failed to trigger metadata generation"}), 500
