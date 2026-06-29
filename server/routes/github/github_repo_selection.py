"""
GitHub multi-repo selection endpoints.
Manages which repos a user has connected for RCA investigation.

Read endpoints surface a per-repo ``auth_method`` (``"app"`` / ``"oauth"``
/ ``None``) computed in a single batched query — see
:func:`get_repo_selections` for the no-N+1 contract that mirrors
:mod:`utils.auth.github_auth_router`'s routing rules.
"""
import logging
import json
from flask import Blueprint, jsonify, request
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import set_rls_context
from utils.db.connection_pool import db_pool
from utils.db.org_scope import resolve_org, org_read_predicate

github_repo_selection_bp = Blueprint('github_repo_selection', __name__)
logger = logging.getLogger(__name__)


def _update_metadata_status(user_id: str, repo_full_name: str, status: str):
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE connected_repos SET metadata_status = %s, updated_at = NOW() WHERE provider = 'github' AND repo_full_name = %s",
                    (status, repo_full_name),
                )
                conn.commit()
    except Exception as e:
        logger.warning(f"Failed to revert metadata_status for {repo_full_name}: {e}")


@github_repo_selection_bp.route("/repo-selections", methods=["GET"])
@require_permission("connectors", "read")
def get_repo_selections(user_id):
    """Return all connected repos for this org plus a per-row ``auth_method``.

    The ``auth_method`` field mirrors the routing decision that
    :func:`utils.auth.github_auth_router.get_auth_for_user_repo` would
    make for each repo:

    - ``"app"`` when the repo row has a non-NULL ``installation_id`` AND
      the joined installation row exists with ``suspended_at IS NULL``.
    - ``"oauth"`` when no active App install is present but the row's
      owner has a stored OAuth ``access_token``.
    - ``None`` when neither path can resolve.
    """
    try:
        from utils.auth.github_auth_mode import is_oauth_token_honored
        from utils.auth.token_management import get_token_data

        org_id = resolve_org(user_id)
        predicate, pred_params = org_read_predicate(user_id, org_id)
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                # change_gating_enabled is OR-ed across the org's duplicate
                # rows for a repo (UNIQUE is per user): the webhook honors
                # ANY enrolled org row, so the UI must reflect the same
                # semantics rather than whichever row DISTINCT ON keeps.
                cur.execute(
                    f"""SELECT DISTINCT ON (r.repo_full_name)
                              r.repo_full_name, r.repo_id, r.default_branch,
                              r.is_private, r.metadata_summary, r.metadata_status,
                              r.repo_data, r.created_at, r.installation_id,
                              r.user_id,
                              (i.installation_id IS NOT NULL
                                  AND i.suspended_at IS NULL)
                                  AS has_active_installation,
                              r.org_change_gating_enabled
                         FROM (
                             SELECT *,
                                    bool_or(change_gating_enabled)
                                        OVER (PARTITION BY repo_full_name)
                                        AS org_change_gating_enabled
                               FROM connected_repos
                              WHERE provider = 'github'
                                AND {predicate}
                         ) r
                         LEFT JOIN github_installations i
                                ON i.installation_id = r.installation_id
                        ORDER BY r.repo_full_name, r.updated_at DESC""",
                    pred_params,
                )
                rows = cur.fetchall()

        oauth_enabled = is_oauth_token_honored()
        oauth_owner_cache: dict[str, bool] = {}

        def _owner_has_oauth(owner_id: str) -> bool:
            if not oauth_enabled or not owner_id:
                return False
            if owner_id not in oauth_owner_cache:
                try:
                    creds = get_token_data(owner_id, "github")
                except Exception:
                    creds = None
                oauth_owner_cache[owner_id] = bool(
                    creds and creds.get("access_token")
                )
            return oauth_owner_cache[owner_id]

        repos = []
        for r in rows:
            installation_id = r[8]
            row_owner = r[9]
            has_active_installation = r[10]
            if installation_id is not None and has_active_installation:
                auth_method: str | None = "app"
            elif _owner_has_oauth(row_owner):
                auth_method = "oauth"
            else:
                auth_method = None
            repos.append({
                "repo_full_name": r[0],
                "repo_id": r[1],
                "default_branch": r[2],
                "is_private": r[3],
                "metadata_summary": r[4],
                "metadata_status": r[5],
                "repo_data": r[6],
                "created_at": r[7].isoformat() if r[7] else None,
                "installation_id": installation_id,
                "auth_method": auth_method,
                "change_gating_enabled": bool(r[11]),
            })
        return jsonify({"repositories": repos})
    except Exception as e:
        logger.exception(f"Error getting repo selections: {e}")
        return jsonify({"error": "Failed to get repository selections"}), 500


@github_repo_selection_bp.route("/repo-selections", methods=["POST"])
@require_permission("connectors", "write")
def save_repo_selections(user_id):
    """Sync the set of connected repos. Upserts new, removes deselected."""
    try:
        data = request.get_json()
        repositories = data.get("repositories") if data else None
        if not isinstance(repositories, list):
            return jsonify({"error": "repositories must be an array"}), 400
        if not all(isinstance(r, dict) for r in repositories):
            return jsonify({"error": "every repositories entry must be an object"}), 400

        org_id = resolve_org(user_id)
        predicate, pred_params = org_read_predicate(user_id, org_id)

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[github_repo_selection:save_repo_selections]")
                cur.execute(
                    "SELECT repo_full_name, user_id FROM connected_repos WHERE provider = 'github'",
                )
                # {repo_full_name: owner_user_id} — we need the owner to delete the right row
                existing = {r[0]: r[1] for r in cur.fetchall()}

                incoming = set()
                newly_added = []

                for repo in repositories:
                    full_name = repo.get("full_name")
                    if not full_name:
                        continue
                    incoming.add(full_name)

                    owner_id = existing.get(full_name, user_id)
                    payload_install_id = repo.get("installation_id")
                    if not isinstance(payload_install_id, int):
                        payload_install_id = None
                    cur.execute(
                        """INSERT INTO connected_repos
                               (user_id, org_id, provider, repo_full_name, repo_id, default_branch,
                                is_private, installation_id, repo_data, metadata_status)
                           VALUES (%s, %s, 'github', %s, %s, %s, %s, %s, %s, 'pending')
                           ON CONFLICT (user_id, provider, repo_full_name) DO UPDATE SET
                               repo_data = EXCLUDED.repo_data,
                               default_branch = EXCLUDED.default_branch,
                               is_private = EXCLUDED.is_private,
                               installation_id = COALESCE(EXCLUDED.installation_id,
                                                          connected_repos.installation_id),
                               updated_at = NOW()""",
                        (
                            owner_id,
                            org_id,
                            full_name,
                            repo.get("id"),
                            repo.get("default_branch"),
                            repo.get("private", False),
                            payload_install_id,
                            json.dumps(repo),
                        ),
                    )
                    if full_name not in existing:
                        existing[full_name] = user_id
                        newly_added.append(full_name)

                if repositories and not incoming:
                    return jsonify({"error": "No valid repositories in request (all missing full_name)"}), 400

                removed = set(existing.keys()) - incoming
                if removed:
                    cur.execute(
                        "DELETE FROM connected_repos WHERE provider = 'github' AND repo_full_name = ANY(%s)",
                        (list(removed),),
                    )

                conn.commit()

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
    """Remove all connected repos for the org."""
    try:
        org_id = resolve_org(user_id)
        predicate, pred_params = org_read_predicate(user_id, org_id)
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[github_repo_selection:clear_repo_selections]")
                cur.execute("DELETE FROM connected_repos WHERE provider = 'github'")
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

        org_id = resolve_org(user_id)
        predicate, pred_params = org_read_predicate(user_id, org_id)

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE connected_repos
                       SET metadata_summary = %s, metadata_status = 'ready', updated_at = NOW()
                       WHERE provider = 'github' AND repo_full_name = %s""",
                    (summary, repo_full_name),
                )
                conn.commit()
        return jsonify({"message": "Metadata updated"})
    except Exception as e:
        logger.error(f"Error updating repo metadata: {e}", exc_info=True)
        return jsonify({"error": "Failed to update metadata"}), 500


@github_repo_selection_bp.route("/repo-selections/<path:repo_full_name>/change-gating", methods=["PUT"])
@require_permission("connectors", "write")
def update_change_gating(user_id, repo_full_name):
    """Enable or disable PR change gating for a specific repo."""
    try:
        data = request.get_json(silent=True)
        enabled = data.get("enabled") if isinstance(data, dict) else None
        if not isinstance(enabled, bool):
            return jsonify({"error": "enabled must be a boolean"}), 400

        org_id = resolve_org(user_id)
        predicate, pred_params = org_read_predicate(user_id, org_id)

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                if enabled:
                    # Duplicate org rows can exist for one repo (UNIQUE is
                    # per user); prefer an App-linked row so an OAuth-era
                    # sibling row can't trigger a spurious 409. Suspended
                    # installations can't deliver webhooks, so enabling
                    # would be a silent no-op — reject those too.
                    cur.execute(
                        f"""SELECT r.installation_id, i.installation_id, i.suspended_at
                             FROM connected_repos r
                             LEFT JOIN github_installations i
                                    ON i.installation_id = r.installation_id
                            WHERE r.provider = 'github'
                              AND r.repo_full_name = %s AND {predicate}
                            ORDER BY (r.installation_id IS NULL) ASC,
                                     r.updated_at DESC
                            LIMIT 1""",
                        (repo_full_name, *pred_params),
                    )
                    row = cur.fetchone()
                    if row is None:
                        return jsonify({"error": "Repository not found"}), 404
                    if row[0] is None:
                        return jsonify({
                            "error": "GitHub App installation is required for Incident Prevention. Install the Aurora GitHub App on this repository to enable it."
                        }), 409
                    # r.installation_id is set but no github_installations row
                    # matched (orphaned id, e.g. the App was removed): enabling
                    # would never deliver webhooks — a silent no-op. Reject it.
                    if row[1] is None:
                        return jsonify({
                            "error": "The GitHub App installation for this repository is no longer registered. Reinstall the Aurora GitHub App to enable Incident Prevention."
                        }), 409
                    if row[2] is not None:
                        return jsonify({
                            "error": "The GitHub App installation for this repository is suspended. Unsuspend it on GitHub to enable Incident Prevention."
                        }), 409
                cur.execute(
                    f"""UPDATE connected_repos
                       SET change_gating_enabled = %s, updated_at = NOW()
                       WHERE provider = 'github' AND repo_full_name = %s AND {predicate}""",
                    (enabled, repo_full_name, *pred_params),
                )
                if cur.rowcount == 0:
                    conn.rollback()
                    return jsonify({"error": "Repository not found"}), 404
                conn.commit()
        return jsonify({
            "repo_full_name": repo_full_name,
            "change_gating_enabled": enabled,
        })
    except Exception:
        logger.exception("Error updating change gating")
        return jsonify({"error": "Failed to update change gating"}), 500


@github_repo_selection_bp.route("/repo-metadata/generate", methods=["POST"])
@require_permission("connectors", "write")
def trigger_metadata_generation(user_id):
    """Trigger LLM metadata generation for a specific repo."""
    try:
        data = request.get_json()
        repo_full_name = data.get("repo_full_name") if data else None
        if not repo_full_name:
            return jsonify({"error": "repo_full_name is required"}), 400

        org_id = resolve_org(user_id)
        predicate, pred_params = org_read_predicate(user_id, org_id)

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE connected_repos SET metadata_status = 'generating', updated_at = NOW()
                       WHERE provider = 'github' AND repo_full_name = %s""",
                    (repo_full_name,),
                )
                conn.commit()

        from routes.github.github_repo_metadata import generate_repo_metadata
        try:
            generate_repo_metadata.delay(user_id, repo_full_name)
        except Exception as e:
            logger.error(f"Failed to enqueue metadata gen for {repo_full_name}: {e}")
            _update_metadata_status(user_id, repo_full_name, "error")
            return jsonify({"error": "Failed to start metadata generation"}), 500
        return jsonify({"message": "Metadata generation started"})
    except Exception as e:
        logger.error(f"Error triggering metadata generation: {e}", exc_info=True)
        return jsonify({"error": "Failed to trigger metadata generation"}), 500
