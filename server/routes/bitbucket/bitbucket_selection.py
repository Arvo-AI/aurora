"""
Bitbucket workspace/repo selection endpoints.
Manages which repos an org has connected for RCA investigation.

Uses the `connected_repos` table as the sole source of truth.
The workspace is derived from repo_full_name (stored as "workspace/repo-slug").

Selection is org-scoped: one Bitbucket connector per org → one shared repo list.
Any user in the org can read/modify the selection; the `user_id` column records
who last wrote each row.
"""
import logging

from flask import Blueprint, jsonify, request

from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import set_rls_context
from utils.db.connection_pool import db_pool
from utils.db.org_scope import resolve_org
from utils.log_sanitizer import sanitize as _sanitize_log

bitbucket_selection_bp = Blueprint("bitbucket_selection", __name__)
logger = logging.getLogger(__name__)


@bitbucket_selection_bp.route("/workspace-selection", methods=["GET"])
@require_permission("connectors", "read")
def get_workspace_selection(user_id):
    """Return connected Bitbucket repos for the org."""
    try:
        org_id = resolve_org(user_id)
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[BitbucketSelection:get]")
                cur.execute(
                    """SELECT repo_full_name, default_branch, metadata_summary, metadata_status
                       FROM connected_repos
                       WHERE org_id = %s AND provider = 'bitbucket'
                       ORDER BY repo_full_name""",
                    (org_id,),
                )
                rows = cur.fetchall()

        if not rows:
            return jsonify({"workspace": None, "repositories": []})

        workspaces = set()
        repositories = []
        for r in rows:
            full_name = r[0]
            ws = full_name.split("/")[0] if "/" in full_name else None
            slug = full_name.split("/", 1)[1] if "/" in full_name else full_name
            if ws:
                workspaces.add(ws)
            repositories.append({
                "slug": slug,
                "name": slug,
                "full_name": full_name,
                "workspace": ws,
                "default_branch": r[1],
                "metadata_summary": r[2],
                "metadata_status": r[3],
                "mainbranch": {"name": r[1]} if r[1] else None,
            })

        # Return first workspace as the default selection for the dropdown
        workspace = next(iter(workspaces)) if workspaces else None

        return jsonify({
            "workspace": workspace,
            "workspaces": list(workspaces),
            "repositories": repositories,
        })

    except Exception as e:
        logger.error("Error getting workspace selection: %s", e, exc_info=True)
        return jsonify({"error": "Failed to get workspace selection"}), 500


@bitbucket_selection_bp.route("/workspace-selection", methods=["POST", "PUT"])
@require_permission("connectors", "write")
def save_workspace_selection(user_id):
    """Save the Bitbucket workspace selection for the org.

    Replaces the org's selection for the given workspace — removes repos that
    were deselected regardless of which user originally added them.
    """
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

        if not repositories:
            repositories = [repository]

        newly_added = []

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                org_id = set_rls_context(cur, conn, user_id, log_prefix="[BitbucketSelection:save]")

                # Get ALL org-level repos for this workspace (regardless of who added them)
                cur.execute(
                    "SELECT repo_full_name FROM connected_repos WHERE org_id = %s AND provider = 'bitbucket' AND repo_full_name LIKE %s",
                    (org_id, f"{workspace}/%"),
                )
                existing = {row[0] for row in cur.fetchall()}

                incoming = set()
                for repo in repositories:
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
                               (user_id, org_id, provider, repo_full_name, default_branch,
                                is_private, metadata_status)
                           VALUES (%s, %s, 'bitbucket', %s, %s, %s, 'pending')
                           ON CONFLICT (user_id, provider, repo_full_name) DO UPDATE SET
                               default_branch = COALESCE(EXCLUDED.default_branch, connected_repos.default_branch),
                               is_private = EXCLUDED.is_private,
                               updated_at = NOW()""",
                        (user_id, org_id, full_name, default_branch, repo.get("is_private", False)),
                    )
                    if full_name not in existing:
                        newly_added.append(full_name)

                # Remove deselected repos org-wide (not just the current user's rows)
                removed = existing - incoming
                if removed:
                    cur.execute(
                        "DELETE FROM connected_repos WHERE org_id = %s AND provider = 'bitbucket' AND repo_full_name = ANY(%s)",
                        (org_id, list(removed)),
                    )

                conn.commit()

        # Kick off metadata generation for newly added repos
        for repo_name in newly_added:
            try:
                from utils.repo_metadata import generate_repo_metadata
                generate_repo_metadata.delay(user_id, "bitbucket", repo_name)
            except Exception as e:
                logger.warning("Failed to enqueue metadata gen for %s: %s", _sanitize_log(repo_name), e)

        logger.info("Saved Bitbucket selection for org %s (by user %s): %s / %d repos (%d new)", _sanitize_log(org_id), _sanitize_log(user_id), _sanitize_log(workspace), len(incoming), len(newly_added))

        return jsonify({
            "message": f"Saved {len(incoming)} repos, removed {len(removed)}",
            "workspace": workspace,
            "repositories": repositories,
            "added": newly_added,
            "removed": list(removed),
        })

    except Exception as e:
        logger.error("Error saving workspace selection: %s", e, exc_info=True)
        return jsonify({"error": "Failed to save workspace selection"}), 500


@bitbucket_selection_bp.route("/workspace-selection", methods=["DELETE"])
@require_permission("connectors", "write")
def clear_workspace_selection(user_id):
    """Clear all Bitbucket connected repos for the org."""
    try:
        org_id = resolve_org(user_id)
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[BitbucketSelection:clear]")
                cur.execute(
                    "DELETE FROM connected_repos WHERE org_id = %s AND provider = 'bitbucket'",
                    (org_id,),
                )
                conn.commit()

        logger.info("Cleared Bitbucket workspace selection for org %s (by user %s)", _sanitize_log(org_id), _sanitize_log(user_id))
        return jsonify({"message": "Workspace selection cleared successfully"})

    except Exception as e:
        logger.error("Error clearing workspace selection: %s", e, exc_info=True)
        return jsonify({"error": "Failed to clear workspace selection"}), 500


@bitbucket_selection_bp.route("/repo-metadata/generate", methods=["POST"])
@require_permission("connectors", "write")
def trigger_metadata_generation(user_id):
    """Trigger LLM metadata generation for a specific Bitbucket repo."""
    try:
        data = request.get_json()
        repo_full_name = data.get("repo_full_name") if data else None
        if not repo_full_name:
            return jsonify({"error": "repo_full_name is required"}), 400

        org_id = resolve_org(user_id)
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[BitbucketMetadata:generate]")
                cur.execute(
                    """UPDATE connected_repos SET metadata_status = 'generating', updated_at = NOW()
                       WHERE org_id = %s AND provider = 'bitbucket' AND repo_full_name = %s""",
                    (org_id, repo_full_name),
                )
                conn.commit()

        from utils.repo_metadata import generate_repo_metadata
        try:
            generate_repo_metadata.delay(user_id, "bitbucket", repo_full_name)
        except Exception as e:
            logger.exception("Failed to enqueue metadata gen for %s", _sanitize_log(repo_full_name))
            return jsonify({"error": "Failed to start metadata generation"}), 500
        return jsonify({"message": "Metadata generation started"})
    except Exception as e:
        logger.exception("Error triggering metadata generation for user=%s", _sanitize_log(user_id))
        return jsonify({"error": "Failed to trigger metadata generation"}), 500


@bitbucket_selection_bp.route("/repo-metadata/<path:repo_full_name>", methods=["PUT"])
@require_permission("connectors", "write")
def update_repo_metadata(user_id, repo_full_name):
    """Update the metadata summary for a specific Bitbucket repo (human edit)."""
    try:
        data = request.get_json()
        summary = data.get("metadata_summary") if data else None
        if summary is None:
            return jsonify({"error": "metadata_summary is required"}), 400

        org_id = resolve_org(user_id)
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[BitbucketMetadata:update]")
                cur.execute(
                    """UPDATE connected_repos
                       SET metadata_summary = %s, metadata_status = 'ready', updated_at = NOW()
                       WHERE org_id = %s AND provider = 'bitbucket' AND repo_full_name = %s""",
                    (summary, org_id, repo_full_name),
                )
                conn.commit()
        return jsonify({"message": "Metadata updated"})
    except Exception as e:
        logger.exception("Error updating repo metadata for %s", _sanitize_log(repo_full_name))
        return jsonify({"error": "Failed to update metadata"}), 500
