"""
Bitbucket workspace/repo selection endpoints.
Manages which repos an org has connected for RCA investigation, and the
per-repo Incident Prevention (PR change gating) toggle.

Uses the `connected_repos` table as the sole source of truth.
The workspace is derived from repo_full_name (stored as "workspace/repo-slug").

Selection is org-scoped: one Bitbucket connector per org → one shared repo list.
Any user in the org can read/modify the selection; the `user_id` column records
who last wrote each row. `change_gating_enabled` is OR-ed across an org's
duplicate rows on read and written to ALL org rows on update, so per-user
duplicates can never disagree.
"""
import logging
import os

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
                    """SELECT repo_full_name,
                              MAX(default_branch),
                              MAX(metadata_summary),
                              MAX(metadata_status),
                              bool_or(change_gating_enabled)
                       FROM connected_repos
                       WHERE org_id = %s AND provider = 'bitbucket'
                       GROUP BY repo_full_name
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
                "change_gating_enabled": bool(r[4]),
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

                # Remove deselected repos org-wide (not just the current user's
                # rows). Capture Aurora-created hook uuids BEFORE the delete so
                # they can be removed from Bitbucket afterwards.
                removed = existing - incoming
                removed_hooks = []
                if removed:
                    cur.execute(
                        """SELECT DISTINCT repo_full_name, webhook_hook_uuid
                           FROM connected_repos
                           WHERE org_id = %s AND provider = 'bitbucket'
                             AND repo_full_name = ANY(%s)
                             AND webhook_hook_uuid IS NOT NULL""",
                        (org_id, list(removed)),
                    )
                    removed_hooks = cur.fetchall()
                    cur.execute(
                        "DELETE FROM connected_repos WHERE org_id = %s AND provider = 'bitbucket' AND repo_full_name = ANY(%s)",
                        (org_id, list(removed)),
                    )

                conn.commit()

        # Deselected repos must not keep delivering webhooks (best-effort).
        for repo_name, hook_uuid in removed_hooks:
            _delete_repo_hook(user_id, repo_name, hook_uuid)

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
    """Clear all Bitbucket connected repos for the org (hooks included)."""
    try:
        org_id = resolve_org(user_id)
        # Delete Aurora-created webhooks BEFORE dropping the rows that hold
        # their uuids.
        try:
            cleanup_org_hooks(user_id, org_id)
        except Exception:
            logger.warning("Bitbucket hook cleanup failed during clear", exc_info=True)
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


def _webhook_base_url() -> str:
    """Externally reachable API base for webhook URLs (ngrok-aware in dev)."""
    ngrok_url = os.getenv("NGROK_URL", "").rstrip("/")
    backend_url = os.getenv("NEXT_PUBLIC_BACKEND_URL", "").rstrip("/")
    base_url = ngrok_url if ngrok_url and backend_url.startswith("http://localhost") else backend_url
    if not base_url:
        base_url = request.host_url.rstrip("/")
    return base_url


def _delete_repo_hook(user_id: str, repo_full_name: str, hook_uuid: str) -> bool:
    """Best-effort delete of a Bitbucket repo webhook Aurora created."""
    try:
        from chat.backend.agent.tools.bitbucket.utils import get_bb_client_for_user

        client = get_bb_client_for_user(user_id)
        if client is None or "/" not in repo_full_name:
            return False
        ws, slug = repo_full_name.split("/", 1)
        result = client.delete_webhook(ws, slug, hook_uuid)
        if isinstance(result, dict) and result.get("error") and result.get("status") != 404:
            logger.warning(
                "Failed to delete Bitbucket hook %s on %s: status=%s",
                _sanitize_log(hook_uuid), _sanitize_log(repo_full_name), result.get("status"),
            )
            return False
        return True
    except Exception:
        logger.warning(
            "Error deleting Bitbucket hook on %s", _sanitize_log(repo_full_name),
            exc_info=True,
        )
        return False


def cleanup_org_hooks(user_id: str, org_id: str, repo_full_names=None) -> None:
    """Delete Aurora-created hooks + clear stored uuids for org repos.

    ``repo_full_names=None`` means every Bitbucket repo in the org
    (disconnect / clear-all); otherwise only the listed repos (deselect).
    Best-effort: API failures still clear the stored uuid so the DB never
    points at a hook we no longer manage.
    """
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[BitbucketHooks:cleanup]")
            if repo_full_names is None:
                cur.execute(
                    """SELECT DISTINCT repo_full_name, webhook_hook_uuid
                       FROM connected_repos
                       WHERE org_id = %s AND provider = 'bitbucket'
                         AND webhook_hook_uuid IS NOT NULL""",
                    (org_id,),
                )
            else:
                cur.execute(
                    """SELECT DISTINCT repo_full_name, webhook_hook_uuid
                       FROM connected_repos
                       WHERE org_id = %s AND provider = 'bitbucket'
                         AND repo_full_name = ANY(%s)
                         AND webhook_hook_uuid IS NOT NULL""",
                    (org_id, list(repo_full_names)),
                )
            hooks = cur.fetchall()
            for repo_full_name, hook_uuid in hooks:
                _delete_repo_hook(user_id, repo_full_name, hook_uuid)
            if hooks:
                cur.execute(
                    """UPDATE connected_repos
                          SET webhook_hook_uuid = NULL, updated_at = NOW()
                        WHERE org_id = %s AND provider = 'bitbucket'
                          AND repo_full_name = ANY(%s)""",
                    (org_id, [h[0] for h in hooks]),
                )
            conn.commit()


def _try_auto_create_hook(
    user_id: str, org_id: str, repo_full_name: str,
    webhook_url: str, webhook_events, secret: str,
) -> bool:
    """Best-effort API creation of the repo webhook; returns True on success.

    Needs repo admin (and webhook scopes the connector token usually lacks) —
    manual setup is the primary path, so failure here is informational only.
    An existing hook with the same URL counts as success.
    """
    try:
        from chat.backend.agent.tools.bitbucket.utils import get_bb_client_for_user

        client = get_bb_client_for_user(user_id)
        if client is None or "/" not in repo_full_name:
            return False
        ws, slug = repo_full_name.split("/", 1)
        existing = client.list_webhooks(ws, slug)
        if any(
            isinstance(h, dict) and h.get("url") == webhook_url
            for h in (existing if isinstance(existing, list) else [])
        ):
            return True
        created = client.create_webhook(
            ws, slug, webhook_url, webhook_events,
            description="Aurora Incident Prevention",
            secret=secret,
        )
        if not isinstance(created, dict) or created.get("error"):
            return False
        hook_uuid = created.get("uuid")
        if hook_uuid:
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cur:
                    set_rls_context(cur, conn, user_id, log_prefix="[BitbucketChangeGating:hook]")
                    cur.execute(
                        """UPDATE connected_repos
                              SET webhook_hook_uuid = %s, updated_at = NOW()
                            WHERE org_id = %s AND provider = 'bitbucket'
                              AND repo_full_name = %s""",
                        (hook_uuid, org_id, repo_full_name),
                    )
                    conn.commit()
        return True
    except Exception:
        logger.info(
            "Bitbucket hook auto-create unavailable for %s — manual setup required",
            _sanitize_log(repo_full_name),
        )
        return False


@bitbucket_selection_bp.route("/repo-selections/<path:repo_full_name>/change-gating", methods=["PUT"])
@require_permission("connectors", "write")
def update_change_gating(user_id, repo_full_name):
    """Enable or disable Incident Prevention for one Bitbucket repo (org-wide).

    On enable, returns ``{webhook_url, webhook_secret, webhook_events}`` ready
    to paste into the repo's Bitbucket webhook settings (creating the org
    secret on first use), and best-effort attempts to create the hook via the
    API — manual setup is the primary path (repo admin + webhook scopes are
    often unavailable), so an API failure is not an error.
    """
    try:
        from utils.flags.feature_flags import is_incident_prevention_enabled

        if not is_incident_prevention_enabled():
            return jsonify({"error": "Incident Prevention is disabled on this deployment."}), 409

        data = request.get_json(silent=True)
        enabled = data.get("enabled") if isinstance(data, dict) else None
        if not isinstance(enabled, bool):
            return jsonify({"error": "enabled must be a boolean"}), 400

        org_id = resolve_org(user_id)
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[BitbucketChangeGating]")
                if enabled:
                    cur.execute(
                        """SELECT MAX(default_branch)
                             FROM connected_repos
                            WHERE org_id = %s AND provider = 'bitbucket'
                              AND repo_full_name = %s""",
                        (org_id, repo_full_name),
                    )
                    row = cur.fetchone()
                    if row is None or row[0] is None:
                        # Repo missing entirely vs missing default_branch both
                        # block gating (the webhook filter compares against it).
                        cur.execute(
                            """SELECT 1 FROM connected_repos
                                WHERE org_id = %s AND provider = 'bitbucket'
                                  AND repo_full_name = %s LIMIT 1""",
                            (org_id, repo_full_name),
                        )
                        if cur.fetchone() is None:
                            return jsonify({"error": "Repository not found"}), 404
                        return jsonify({
                            "error": "This repository has no default branch recorded. "
                                     "Re-save the repository selection, then try again."
                        }), 409
                # Update ALL org rows for this repo (duplicates per user exist).
                cur.execute(
                    """UPDATE connected_repos
                          SET change_gating_enabled = %s, updated_at = NOW()
                        WHERE org_id = %s AND provider = 'bitbucket'
                          AND repo_full_name = %s""",
                    (enabled, org_id, repo_full_name),
                )
                if cur.rowcount == 0:
                    conn.rollback()
                    return jsonify({"error": "Repository not found"}), 404
                conn.commit()

        response = {
            "repo_full_name": repo_full_name,
            "change_gating_enabled": enabled,
        }

        webhook_events = ["pullrequest:created", "pullrequest:updated"]
        if enabled:
            from connectors.bitbucket_connector.webhook_secret import (
                get_or_create_webhook_secret,
            )

            secret = get_or_create_webhook_secret(org_id)
            webhook_url = f"{_webhook_base_url()}/bitbucket/webhook/{org_id}"
            response.update({
                "webhook_url": webhook_url,
                "webhook_secret": secret,
                "webhook_events": webhook_events,
                "webhook_note": (
                    "Use this SAME url and secret on every repository you "
                    "enable — the secret is shared across your organization."
                ),
            })

            response["webhook_auto_created"] = _try_auto_create_hook(
                user_id, org_id, repo_full_name, webhook_url, webhook_events, secret
            )
        else:
            # Disable: delete the hook if Aurora created one, clear the uuid.
            cleanup_org_hooks(user_id, org_id, [repo_full_name])

        logger.info(
            "Bitbucket Incident Prevention %s for %s (org %s, by user %s)",
            "enabled" if enabled else "disabled",
            _sanitize_log(repo_full_name), _sanitize_log(org_id), _sanitize_log(user_id),
        )
        return jsonify(response)
    except Exception:
        logger.exception("Error updating Bitbucket change gating")
        return jsonify({"error": "Failed to update Incident Prevention"}), 500


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
