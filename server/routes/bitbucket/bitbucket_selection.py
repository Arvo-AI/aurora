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
import time
from concurrent.futures import ThreadPoolExecutor

from celery_config import celery_app
from flask import Blueprint, jsonify, request

from connectors.bitbucket_connector.webhook_secret import get_or_create_webhook_secret
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
        expected_url = f"{_webhook_base_url()}/bitbucket/webhook/{org_id}"
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[BitbucketSelection:get]")
                cur.execute(
                    """SELECT repo_full_name,
                              MAX(default_branch),
                              MAX(metadata_summary),
                              MAX(metadata_status),
                              bool_or(change_gating_enabled),
                              bool_or(webhook_verified_url = %s),
                              bool_or(webhook_verified_url IS NOT NULL
                                      AND webhook_verified_url <> %s)
                       FROM connected_repos
                       WHERE org_id = %s AND provider = 'bitbucket'
                       GROUP BY repo_full_name
                       ORDER BY repo_full_name""",
                    (expected_url, expected_url, org_id),
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
                # Green ONLY when a delivery landed, or Verify saw the hook, at
                # the URL Aurora serves right now. A stored hook uuid is NOT
                # evidence: it says Aurora once created a hook, not that the
                # hook still exists or can still reach us.
                "webhook_configured": bool(r[5]),
                # True when the hook was verified at a DIFFERENT public URL
                # than Aurora now serves: it can no longer reach us, so the
                # UI must surface it as broken rather than merely unverified.
                "webhook_stale": bool(r[6]),
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
        if repositories is None:
            if not repository:
                return jsonify({"error": "At least one repository is required"}), 400
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
                               is_private = COALESCE(EXCLUDED.is_private, connected_repos.is_private),
                               updated_at = NOW()""",
                        (user_id, org_id, full_name, default_branch, repo.get("is_private")),
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
    from connectors.bitbucket_connector.webhook_secret import webhook_base_url

    return webhook_base_url()


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


def cleanup_org_hooks(user_id: str, org_id: str, repo_full_names=None) -> list[str]:
    """Delete Aurora-created hooks + clear verification state for org repos.

    Returns the repos whose hook could NOT be deleted, so the caller can tell
    the admin to remove it by hand. That is the COMMON case, not an edge one:
    Bitbucket refuses API deletion of hooks created through its web UI, so
    "disable" can rarely guarantee the hook is gone.

    ``repo_full_names=None`` means every Bitbucket repo in the org
    (disconnect / clear-all); otherwise only the listed repos (deselect).
    The pooled DB connection is NOT held across the Bitbucket HTTP calls
    (each has a 30s timeout — holding it would starve the pool).
    """
    # Phase 1: read the hook list, release the connection.
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

    # Phase 2: Bitbucket API calls, no DB connection held.
    # Same 3-wide pool as bulk enable — Bitbucket has no bulk webhook API.
    def _one(item):
        repo_full_name, hook_uuid = item
        return repo_full_name, _delete_repo_hook(user_id, repo_full_name, hook_uuid)

    deleted, failed = [], []
    with ThreadPoolExecutor(max_workers=3) as pool:
        for repo_full_name, ok in pool.map(_one, hooks):
            (deleted if ok else failed).append(repo_full_name)

    # Phase 3: clear verification state for EVERY targeted repo, whether or
    # not Bitbucket let us delete the hook. Aurora's own view of "is this
    # verified" must not depend on a remote delete we often can't perform —
    # otherwise a failed delete leaves the repo looking connected. The uuid
    # is cleared only where the delete succeeded, so a live hook keeps its
    # handle for a later retry (it no longer affects the badge).
    scope = list(repo_full_names) if repo_full_names is not None else None
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[BitbucketHooks:cleanup]")
            cur.execute(
                """UPDATE connected_repos
                      SET webhook_verified_at = NULL,
                          webhook_verified_url = NULL,
                          webhook_hook_uuid = CASE WHEN repo_full_name = ANY(%s)
                                                   THEN NULL ELSE webhook_hook_uuid END,
                          updated_at = NOW()
                    WHERE org_id = %s AND provider = 'bitbucket'
                      AND (%s::text[] IS NULL OR repo_full_name = ANY(%s))""",
                (deleted, org_id, scope, scope),
            )
            conn.commit()
    if failed:
        logger.warning(
            "Bitbucket hook cleanup incomplete for org %s: %d of %d deleted "
            "(uuids kept for retry on the failed ones)",
            _sanitize_log(org_id), len(deleted), len(hooks),
        )
    return failed


def _until_not_429(fn):
    delay = 1
    result = fn()
    for _ in range(7):
        if not (isinstance(result, dict) and result.get("status") == 429):
            return result
        time.sleep(delay)
        delay = min(delay * 2, 32)
        result = fn()
    return result


def _try_auto_create_hook(
    user_id: str, org_id: str, repo_full_name: str,
    webhook_url: str, webhook_events, secret: str,
) -> bool:
    """Best-effort API creation of the repo webhook; returns True on success.

    Needs the write:webhook:bitbucket scope, which the connector token often
    lacks — manual setup is the primary path, so failure here is informational
    only. An existing hook with the same URL counts as success (its uuid is
    recorded too, so disable/disconnect can still delete it). A success also
    records verification: creating/finding the hook is the same bar as Verify.
    """
    def _mark_verified(hook_uuid=None) -> None:
        # Isolated: by this point the hook EXISTS on Bitbucket, so a DB blip
        # here must not flip the return to False (which would tell the admin
        # to create a second hook manually). The cost of a lost uuid is only
        # that disable/disconnect can't auto-delete this hook later.
        try:
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cur:
                    set_rls_context(cur, conn, user_id, log_prefix="[BitbucketChangeGating:hook]")
                    cur.execute(
                        """UPDATE connected_repos
                              SET webhook_hook_uuid = COALESCE(%s, webhook_hook_uuid),
                                  webhook_verified_at = COALESCE(webhook_verified_at, NOW()),
                                  webhook_verified_url = %s,
                                  updated_at = NOW()
                            WHERE org_id = %s AND provider = 'bitbucket'
                              AND repo_full_name = %s""",
                        (hook_uuid, webhook_url, org_id, repo_full_name),
                    )
                    conn.commit()
        except Exception:
            logger.warning(
                "Bitbucket hook created for %s but uuid persistence failed — "
                "automatic hook deletion on disable will not work for this repo",
                _sanitize_log(repo_full_name),
            )

    try:
        from chat.backend.agent.tools.bitbucket.utils import get_bb_client_for_user

        client = get_bb_client_for_user(user_id)
        if client is None or "/" not in repo_full_name:
            return False
        ws, slug = repo_full_name.split("/", 1)
        existing = _until_not_429(lambda: client.list_webhooks(ws, slug))
        for hook in existing if isinstance(existing, list) else []:
            if not (isinstance(hook, dict) and hook.get("url") == webhook_url):
                continue
            # Same bar as verify_change_gating_webhook: an existing hook only
            # counts as configured when it is active AND carries both
            # pullrequest triggers — a disabled or partial hook would report
            # success while deliveries silently never arrive.
            if not hook.get("active"):
                continue
            if {"pullrequest:created", "pullrequest:updated"} - set(hook.get("events") or []):
                continue
            _mark_verified(hook.get("uuid"))
            return True
        created = _until_not_429(lambda: client.create_webhook(
            ws, slug, webhook_url, webhook_events,
            description="Aurora Incident Prevention",
            secret=secret,
        ))
        if not isinstance(created, dict) or created.get("error"):
            return False
        _mark_verified(created.get("uuid"))
        return True
    except Exception:
        logger.info(
            "Bitbucket hook auto-create unavailable for %s — manual setup required",
            _sanitize_log(repo_full_name),
        )
        return False


def _apply_change_gating_bulk(user_id, org_id, names, enabled=True):
    # ponytail: Bitbucket has no bulk webhook API. 3-wide pool; raise workers if 429s stay rare at 500 repos.
    if not enabled:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[BitbucketChangeGating:bulk]")
                cur.execute(
                    """UPDATE connected_repos
                          SET change_gating_enabled = FALSE, updated_at = NOW()
                        WHERE org_id = %s AND provider = 'bitbucket'
                          AND repo_full_name = ANY(%s)""",
                    (org_id, names),
                )
                conn.commit()
        failed = cleanup_org_hooks(user_id, org_id, names)
        return {
            "org_id": org_id,
            "change_gating_enabled": False,
            "webhook_cleanup_failed": bool(failed),
            "results": [{"repo_full_name": n} for n in names],
        }

    webhook_events = ["pullrequest:created", "pullrequest:updated"]
    webhook_url = f"{_webhook_base_url()}/bitbucket/webhook/{org_id}"
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[BitbucketChangeGating:bulk]")
            cur.execute(
                """SELECT repo_full_name, MAX(default_branch),
                          bool_or(webhook_verified_url = %s)
                     FROM connected_repos
                    WHERE org_id = %s AND provider = 'bitbucket'
                      AND repo_full_name = ANY(%s)
                    GROUP BY repo_full_name""",
                (webhook_url, org_id, names),
            )
            rows = cur.fetchall()
            to_enable = [r[0] for r in rows if r[1] is not None]
            skipped_no_branch = [r[0] for r in rows if r[1] is None]
            hook_ok = {r[0] for r in rows if r[2]}
            if to_enable:
                cur.execute(
                    """UPDATE connected_repos
                          SET change_gating_enabled = TRUE, updated_at = NOW()
                        WHERE org_id = %s AND provider = 'bitbucket'
                          AND repo_full_name = ANY(%s)""",
                    (org_id, to_enable),
                )
            conn.commit()

    results = [{"repo_full_name": n, "error": "no_default_branch"} for n in skipped_no_branch]
    results.extend({"repo_full_name": n} for n in to_enable if n in hook_ok)
    out = {"org_id": org_id, "change_gating_enabled": True, "results": results}
    need_hooks = [n for n in to_enable if n not in hook_ok]
    if not need_hooks and not to_enable:
        return out

    secret = get_or_create_webhook_secret(org_id)
    # Secret is attached on job poll only if a repo needs the manual dialog.
    # Celery INFO-logs the return value; never put the secret in the task result.
    out.update({
        "webhook_url": webhook_url,
        "webhook_events": webhook_events,
    })
    if need_hooks:
        def _one(name):
            return {
                "repo_full_name": name,
                "webhook_auto_created": _try_auto_create_hook(
                    user_id, org_id, name, webhook_url, webhook_events, secret
                ),
            }
        with ThreadPoolExecutor(max_workers=3) as pool:
            results.extend(pool.map(_one, need_hooks))
    return out


@celery_app.task(name="bitbucket.enable_change_gating_bulk", time_limit=3600, queue="high")
def enable_change_gating_bulk(user_id, org_id, names, enabled=True):
    return _apply_change_gating_bulk(user_id, org_id, names, enabled)


@bitbucket_selection_bp.route("/repo-selections/change-gating", methods=["PUT"])
@require_permission("connectors", "write")
def update_change_gating_bulk(user_id):
    """Enqueue Incident Prevention enable for selected connected Bitbucket repos."""
    try:
        from utils.flags.feature_flags import is_incident_prevention_enabled

        if not is_incident_prevention_enabled():
            return jsonify({"error": "Incident Prevention is disabled on this deployment."}), 409

        data = request.get_json(silent=True)
        enabled = data.get("enabled") if isinstance(data, dict) else None
        if not isinstance(enabled, bool):
            return jsonify({"error": "enabled must be a boolean"}), 400
        names = data.get("repo_full_names")
        if not isinstance(names, list) or not names or not all(
            isinstance(n, str) and n.strip() for n in names
        ):
            return jsonify({"error": "repo_full_names must be a non-empty list of strings"}), 400

        org_id = resolve_org(user_id)
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[BitbucketChangeGating:bulk]")
                cur.execute(
                    """SELECT repo_full_name FROM connected_repos
                       WHERE org_id = %s AND provider = 'bitbucket'
                         AND repo_full_name = ANY(%s)
                       GROUP BY repo_full_name""",
                    (org_id, names),
                )
                found = {r[0] for r in cur.fetchall()}
        missing = [n for n in names if n not in found]
        if missing:
            return jsonify({"error": "Repository not found", "missing": missing}), 404

        task = enable_change_gating_bulk.delay(user_id, org_id, names, enabled=enabled)
        logger.info(
            "Bitbucket Incident Prevention bulk-%s queued %s repos (org %s, by user %s, task %s)",
            "enable" if enabled else "disable",
            len(names), _sanitize_log(org_id), _sanitize_log(user_id), task.id,
        )
        return jsonify({"task_id": task.id, "count": len(names)}), 202
    except Exception:
        logger.exception("Error queueing Bitbucket change gating bulk enable")
        return jsonify({"error": "Failed to update Incident Prevention"}), 500


@bitbucket_selection_bp.route("/repo-selections/change-gating/jobs/<task_id>", methods=["GET"])
@require_permission("connectors", "write")
def get_change_gating_bulk_job(user_id, task_id):
    """Poll the Celery bulk-enable job."""
    task = enable_change_gating_bulk.AsyncResult(task_id)
    if task.state in ("PENDING", "STARTED"):
        return jsonify({"state": task.state, "complete": False})
    if task.state == "SUCCESS":
        result = dict(task.result or {})
        owner = result.pop("org_id", None)
        if owner is not None and owner != resolve_org(user_id):
            return jsonify({"error": "Not found"}), 404
        results = result.get("results") or []
        if result.get("change_gating_enabled") and any(
            r.get("webhook_auto_created") is False for r in results
        ):
            result["webhook_secret"] = get_or_create_webhook_secret(resolve_org(user_id))
        return jsonify({"state": task.state, "complete": True, "result": result})
    logger.error(
        "Bitbucket bulk-enable task %s ended in state %s: %s",
        _sanitize_log(task_id), task.state, task.info,
    )
    return jsonify({
        "state": task.state,
        "complete": True,
        "error": True,
        "status": "Failed to update Incident Prevention",
    }), 200


@bitbucket_selection_bp.route("/repo-selections/<path:repo_full_name>/change-gating", methods=["PUT"])
@require_permission("connectors", "write")
def update_change_gating(user_id, repo_full_name):
    """Enable or disable Incident Prevention for one Bitbucket repo (org-wide).

    On enable, returns ``{webhook_url, webhook_secret, webhook_events}`` ready
    to paste into the repo's Bitbucket webhook settings (creating the org
    secret on first use). On a genuine off -> on transition it also tries to
    create the hook via the API and reports ``webhook_auto_created``; manual
    setup is the primary path (the write:webhook:bitbucket scope is often
    missing), so failure there is not an error. Calling this for an
    already-enabled repo just re-returns the details and omits
    ``webhook_auto_created`` — the UI uses that to reopen the setup dialog,
    which must not mutate anything in Bitbucket.
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
        was_enabled = False
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[BitbucketChangeGating]")
                if enabled:
                    cur.execute(
                        """SELECT MAX(default_branch), bool_or(change_gating_enabled)
                             FROM connected_repos
                            WHERE org_id = %s AND provider = 'bitbucket'
                              AND repo_full_name = %s""",
                        (org_id, repo_full_name),
                    )
                    row = cur.fetchone()
                    was_enabled = bool(row and row[1])
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

            # Only create a hook on a real off -> on transition. The UI also
            # calls this endpoint to re-show the url/secret after the setup
            # dialog was dismissed, and creating a webhook as a side effect of
            # *viewing* config is surprising: it silently repairs (or
            # duplicates) a hook the admin was inspecting. Already-on means
            # "show me the details again", so leave Bitbucket alone and omit
            # the flag entirely — absent means "not attempted", which is
            # neither the success nor the failure message.
            if not was_enabled:
                response["webhook_auto_created"] = _try_auto_create_hook(
                    user_id, org_id, repo_full_name, webhook_url, webhook_events, secret
                )
        else:
            # Disable: try to delete the hook, always clear verification state.
            # Deletion usually fails (Bitbucket blocks API deletes of hooks made
            # in its UI), so say so — gating is off either way, but Bitbucket
            # keeps POSTing until the admin removes the hook by hand.
            if cleanup_org_hooks(user_id, org_id, [repo_full_name]):
                response["webhook_cleanup_failed"] = True

        logger.info(
            "Bitbucket Incident Prevention %s for %s (org %s, by user %s)",
            "enabled" if enabled else "disabled",
            _sanitize_log(repo_full_name), _sanitize_log(org_id), _sanitize_log(user_id),
        )
        return jsonify(response)
    except Exception:
        logger.exception("Error updating Bitbucket change gating")
        return jsonify({"error": "Failed to update Incident Prevention"}), 500


@bitbucket_selection_bp.route("/repo-selections/<path:repo_full_name>/change-gating/verify", methods=["POST"])
@require_permission("connectors", "write")
def verify_change_gating_webhook(user_id, repo_full_name):
    """Check whether the repo's Incident Prevention webhook exists RIGHT NOW.

    Asks Bitbucket for the repo's hooks and requires one that is active, has
    both pullrequest triggers, and points at the org's current webhook URL.
    A match stores the hook uuid (covers manually created hooks, so cleanup
    can try to delete it) and the URL it was verified against.

    A definitive miss (no hook / disabled / missing triggers) CLEARS any
    previous verification: without that, a repo verified once stays green
    forever after its hook is deleted or broken in Bitbucket.

    Listing needs the webhook read scope. When Aurora can't look, that is not
    proof of absence, so it falls back to delivery evidence (a past delivery
    at this URL passed HMAC) and leaves stored state untouched.

    POST + connectors:write because of those persistence side effects.
    """
    def _clear_verification() -> None:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[BitbucketChangeGating:verify]")
                cur.execute(
                    """UPDATE connected_repos
                          SET webhook_verified_at = NULL,
                              webhook_verified_url = NULL,
                              updated_at = NOW()
                        WHERE org_id = %s AND provider = 'bitbucket'
                          AND repo_full_name = %s
                          AND webhook_verified_at IS NOT NULL""",
                    (org_id, repo_full_name),
                )
                conn.commit()

    try:
        org_id = resolve_org(user_id)
        webhook_url = f"{_webhook_base_url()}/bitbucket/webhook/{org_id}"

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[BitbucketChangeGating:verify]")
                cur.execute(
                    """SELECT bool_or(webhook_verified_url = %s)
                         FROM connected_repos
                        WHERE org_id = %s AND provider = 'bitbucket'
                          AND repo_full_name = %s""",
                    (webhook_url, org_id, repo_full_name),
                )
                row = cur.fetchone()
        delivery_seen = bool(row and row[0])

        from chat.backend.agent.tools.bitbucket.utils import get_bb_client_for_user

        client = get_bb_client_for_user(user_id)
        if client is None or "/" not in repo_full_name:
            return jsonify({
                "verified": delivery_seen,
                "reason": "delivery_seen" if delivery_seen else "bitbucket_not_connected",
                "detail": None if delivery_seen else "Reconnect Bitbucket, then verify again.",
            }), 200
        ws, slug = repo_full_name.split("/", 1)
        hooks = client.list_webhooks(ws, slug)
        if isinstance(hooks, dict) and hooks.get("error"):
            # Most common cause: the connector token lacks webhook read
            # scope — we can't see hooks, which is not proof of absence, so
            # stored state stands and past delivery evidence still counts.
            return jsonify({
                "verified": delivery_seen,
                "reason": "delivery_seen" if delivery_seen else "cannot_list_hooks",
                "detail": None if delivery_seen else
                          "Aurora can't read this repository's webhook list (that needs the "
                          "read:webhook:bitbucket scope), so it will confirm the hook "
                          "automatically the first time Bitbucket sends a pull request event.",
            }), 200

        match = None
        for hook in hooks if isinstance(hooks, list) else []:
            if isinstance(hook, dict) and hook.get("url") == webhook_url:
                match = hook
                break
        # From here Bitbucket's hook list is authoritative, so a miss clears
        # any stale verification instead of letting the badge stay green.
        if match is None:
            _clear_verification()
            return jsonify({
                "verified": False,
                "reason": "hook_not_found",
                "detail": "No webhook on this repository points at Aurora. Add it under "
                          "Repository settings → Webhooks.",
            }), 200

        events = set(match.get("events") or [])
        missing_events = {"pullrequest:created", "pullrequest:updated"} - events
        if not match.get("active"):
            _clear_verification()
            return jsonify({
                "verified": False,
                "reason": "hook_inactive",
                "detail": "The webhook exists but is disabled. Enable it in Bitbucket "
                          "under Repository settings → Webhooks.",
            }), 200
        if missing_events:
            _clear_verification()
            return jsonify({
                "verified": False,
                "reason": "missing_events",
                "detail": f"Hook exists but is missing triggers: {', '.join(sorted(missing_events))}",
            }), 200

        # Hook confirmed at the CURRENT url — persist its uuid (covers manual
        # setups) and the url it was verified against, so a later change to
        # Aurora's public url marks this repo stale instead of leaving it green.
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[BitbucketChangeGating:verify]")
                cur.execute(
                    """UPDATE connected_repos
                          SET webhook_hook_uuid = COALESCE(%s, webhook_hook_uuid),
                              webhook_verified_at = COALESCE(webhook_verified_at, NOW()),
                              webhook_verified_url = %s,
                              updated_at = NOW()
                        WHERE org_id = %s AND provider = 'bitbucket'
                          AND repo_full_name = %s""",
                    (match.get("uuid"), webhook_url, org_id, repo_full_name),
                )
                conn.commit()
        return jsonify({"verified": True}), 200
    except Exception:
        logger.exception("Error verifying Bitbucket webhook")
        return jsonify({"error": "Failed to verify webhook"}), 500


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
