"""
Bitbucket workspace selection storage endpoints.
Stores the user's selected workspace and repository.
"""
import logging

from flask import Blueprint, jsonify, request

from utils.auth.stateless_auth import get_credentials_from_db
from utils.auth.token_management import store_tokens_in_db
from utils.auth.rbac_decorators import require_permission

bitbucket_selection_bp = Blueprint("bitbucket_selection", __name__)
logger = logging.getLogger(__name__)


def _sync_selected_repos(user_id: str, workspace: str, selected_repos: list):
    """Sync user-selected repos into connected_repos. Upserts new, removes deselected."""
    from utils.db.connection_pool import db_pool
    from utils.db.org_scope import resolve_org
    from utils.auth.stateless_auth import set_rls_context

    org_id = resolve_org(user_id)
    newly_added = []
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[BitbucketSelection:sync]")

                # Get existing repos to detect new additions
                cur.execute(
                    "SELECT repo_full_name FROM connected_repos WHERE user_id = %s AND provider = 'bitbucket'",
                    (user_id,),
                )
                existing = {row[0] for row in cur.fetchall()}

                incoming = set()
                for repo in selected_repos:
                    if not isinstance(repo, dict) or not repo.get("slug"):
                        continue
                    slug = repo["slug"]
                    full_name = f"{workspace}/{slug}"
                    incoming.add(full_name)
                    default_branch = None
                    mainbranch = repo.get("mainbranch")
                    if mainbranch:
                        default_branch = mainbranch.get("name")
                    cur.execute(
                        """INSERT INTO connected_repos
                               (user_id, org_id, provider, repo_full_name, default_branch, metadata_status)
                           VALUES (%s, %s, 'bitbucket', %s, %s, 'pending')
                           ON CONFLICT (user_id, provider, repo_full_name) DO UPDATE SET
                               default_branch = COALESCE(EXCLUDED.default_branch, connected_repos.default_branch),
                               updated_at = NOW()""",
                        (user_id, org_id, full_name, default_branch),
                    )
                    if full_name not in existing:
                        newly_added.append(full_name)

                # Remove deselected repos
                if incoming:
                    cur.execute(
                        """DELETE FROM connected_repos
                           WHERE user_id = %s AND provider = 'bitbucket'
                             AND repo_full_name NOT IN (SELECT unnest(%s::text[]))""",
                        (user_id, list(incoming)),
                    )
                else:
                    cur.execute(
                        "DELETE FROM connected_repos WHERE user_id = %s AND provider = 'bitbucket'",
                        (user_id,),
                    )

                conn.commit()

        # Kick off metadata generation for newly added repos
        for repo_name in newly_added:
            try:
                from utils.repo_metadata import generate_repo_metadata
                generate_repo_metadata.delay(user_id, "bitbucket", repo_name)
            except Exception as e:
                logger.warning(f"Failed to enqueue metadata gen for {repo_name}: {e}")

        logger.info(f"Synced {len(incoming)} Bitbucket repos for user {user_id} ({len(newly_added)} new)")
    except Exception as e:
        logger.warning(f"Failed to sync selected repos: {e}")


@bitbucket_selection_bp.route("/workspace-selection", methods=["GET"])
@require_permission("connectors", "read")
def get_workspace_selection(user_id):
    """Get the stored Bitbucket workspace selection for a user."""
    try:
        selection = get_credentials_from_db(user_id, "bitbucket_workspace_selection") or {}

        return jsonify({
            "workspace": selection.get("workspace"),
            "repository": selection.get("repository"),
            "repositories": selection.get("repositories"),
        })

    except Exception as e:
        logger.error(f"Error getting workspace selection: {e}", exc_info=True)
        return jsonify({"error": "Failed to get workspace selection"}), 500


@bitbucket_selection_bp.route("/workspace-selection", methods=["POST", "PUT"])
@require_permission("connectors", "write")
def save_workspace_selection(user_id):
    """Save the Bitbucket workspace selection and sync repos to connected_repos."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Request body required"}), 400

        workspace = data.get("workspace")
        repositories = data.get("repositories")
        repository = data.get("repository")

        if not workspace:
            return jsonify({"error": "Workspace is required"}), 400
        if not repositories and not repository:
            return jsonify({"error": "At least one repository is required"}), 400

        # Normalize: support both single repo (legacy) and multi-repo
        if not repositories:
            repositories = [repository]

        selection_data = {
            "workspace": workspace,
            "repositories": repositories,
            "repository": repositories[0] if repositories else None,
        }

        store_tokens_in_db(user_id, selection_data, "bitbucket_workspace_selection")

        # Sync only selected repos into connected_repos (validated via API)
        _sync_selected_repos(user_id, workspace, repositories)

        logger.info(f"Saved Bitbucket workspace selection for user {user_id}: {workspace} / {len(repositories)} repos")

        return jsonify({
            "message": "Workspace selection saved successfully",
            "workspace": workspace,
            "repositories": repositories,
        })

    except Exception as e:
        logger.error(f"Error saving workspace selection: {e}", exc_info=True)
        return jsonify({"error": "Failed to save workspace selection"}), 500


@bitbucket_selection_bp.route("/workspace-selection", methods=["DELETE"])
@require_permission("connectors", "write")
def clear_workspace_selection(user_id):
    """Clear the Bitbucket workspace selection for a user."""
    try:
        from utils.secrets.secret_ref_utils import delete_user_secret

        delete_user_secret(user_id, "bitbucket_workspace_selection")

        logger.info(f"Cleared Bitbucket workspace selection for user {user_id}")
        return jsonify({"message": "Workspace selection cleared successfully"})

    except Exception as e:
        logger.error(f"Error clearing workspace selection: {e}", exc_info=True)
        return jsonify({"error": "Failed to clear workspace selection"}), 500
