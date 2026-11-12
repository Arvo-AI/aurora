"""
Celery task to generate LLM-powered metadata summaries for connected GitHub repos.
Fetches README + top-level directory listing via GitHub REST API, summarizes with LLM.

Auth selection is delegated to :mod:`utils.auth.github_auth_router` so
the task transparently uses the GitHub App installation token when the
repo was added via the App install flow and the legacy OAuth token
otherwise. ``NoGitHubAuthError`` is mapped to a clean ``error`` status on
the row (no retry — re-auth is a user action, not a transient failure).
"""
import base64
import json
import logging
from typing import Any
import requests
from celery_config import celery_app
from chat.backend.agent.utils.message_content import extract_text_from_content

logger = logging.getLogger(__name__)


METADATA_PROMPT = (
    "Write a 2-3 sentence summary of this GitHub repository. "
    "State what it does, what services/infrastructure it contains, and key technologies. "
    "Infer from file names if no README is available. "
    "Output ONLY the summary. No notes, caveats, warnings, or markdown headers.\n\n"
    "{context}"
)


def _fetch_readme(auth_headers: dict[str, Any], owner: str, repo: str) -> str:
    resp = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/readme",
        headers={**auth_headers, "Accept": "application/vnd.github.v3+json"},
        timeout=15,
    )
    if resp.status_code != 200:
        return ""
    content = resp.json().get("content", "")
    try:
        decoded = base64.b64decode(content).decode("utf-8", errors="replace")
        return decoded[:4000]
    except Exception as e:
        logger.warning(f"Failed to decode README for {owner}/{repo}: {e}")
        return ""


def _fetch_top_level_listing(auth_headers: dict[str, Any], owner: str, repo: str) -> str:
    resp = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/contents",
        headers={**auth_headers, "Accept": "application/vnd.github.v3+json"},
        timeout=15,
    )
    if resp.status_code != 200:
        return "(could not list files)"
    items = resp.json()
    if not isinstance(items, list):
        return "(could not list files)"
    return "\n".join(f"{'dir' if i.get('type') == 'dir' else 'file'}: {i.get('name')}" for i in items)


def _update_metadata(user_id: str, repo_full_name: str, summary: str, status: str):
    """Persist a generation-task metadata write with CAS protection."""
    from utils.db.connection_pool import db_pool
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            from utils.auth.stateless_auth import set_rls_context
            if not set_rls_context(cur, conn, user_id, log_prefix="[GitHubMetadata]"):
                return
            if summary is None:
                cur.execute(
                    """UPDATE connected_repos
                       SET metadata_status = %s, updated_at = NOW()
                       WHERE user_id = %s
                         AND provider = 'github'
                         AND repo_full_name = %s
                         AND metadata_status IN ('pending', 'generating')""",
                    (status, user_id, repo_full_name),
                )
            else:
                cur.execute(
                    """UPDATE connected_repos
                       SET metadata_summary = %s, metadata_status = %s, updated_at = NOW()
                       WHERE user_id = %s
                         AND provider = 'github'
                         AND repo_full_name = %s
                         AND metadata_status IN ('pending', 'generating')""",
                    (summary, status, user_id, repo_full_name),
                )
            conn.commit()


@celery_app.task(name="routes.github.github_repo_metadata.generate_repo_metadata", bind=True, max_retries=2)
def generate_repo_metadata(self, user_id: str, repo_full_name: str):
    """Fetch repo info from GitHub API and generate an LLM summary."""
    from utils.auth.github_auth_router import (
        NoGitHubAuthError,
        get_auth_for_user_repo,
        make_auth_header,
    )

    logger.info(f"Generating metadata for {repo_full_name} (user {user_id})")

    # Hook: check if LLM call is allowed
    from utils.hooks import get_hook
    from utils.auth.stateless_auth import get_org_id_for_user
    _hook_org_id = get_org_id_for_user(user_id) if user_id else None
    hook_allowed, hook_message = get_hook("before_llm_call")(_hook_org_id, user_id)
    if not hook_allowed:
        logger.warning("Hook blocked for user %s: %s", user_id, hook_message)
        _update_metadata(user_id, repo_full_name, None, "limit_reached")
        return

    _update_metadata(user_id, repo_full_name, None, "generating")

    try:
        try:
            auth = get_auth_for_user_repo(user_id, repo_full_name)
        except NoGitHubAuthError as exc:
            logger.exception(
                "No GitHub auth available for user %s repo %s: %s",
                user_id, repo_full_name, exc,
            )
            _update_metadata(user_id, repo_full_name, None, "error")
            return
        auth_headers = make_auth_header(auth)

        parts = repo_full_name.split("/")
        if len(parts) != 2:
            logger.error(f"Invalid repo format '{repo_full_name}' for user {user_id}, expected owner/repo")
            _update_metadata(user_id, repo_full_name, None, "error")
            return
        owner, repo = parts

        readme = _fetch_readme(auth_headers, owner, repo)
        file_list = _fetch_top_level_listing(auth_headers, owner, repo)

        if not readme and file_list == "(could not list files)":
            logger.warning(f"Could not fetch any content for {repo_full_name}, skipping LLM summary")
            _update_metadata(user_id, repo_full_name, None, "error")
            return

        context_parts = []
        if readme:
            context_parts.append(f"README:\n{readme}")
        context_parts.append(f"Top-level files/directories:\n{file_list}")

        from chat.backend.agent.providers import create_chat_model
        from chat.backend.agent.llm import ModelConfig
        from chat.backend.agent.utils.llm_usage_tracker import tracked_invoke
        from langchain_core.messages import HumanMessage

        llm = create_chat_model(
            ModelConfig.INCIDENT_REPORT_SUMMARIZATION_MODEL,
            temperature=0.2,
            streaming=False,
        )

        prompt = METADATA_PROMPT.format(context="\n\n".join(context_parts))
        response = tracked_invoke(
            llm,
            [HumanMessage(content=prompt)],
            user_id=user_id,
            model_name=ModelConfig.INCIDENT_REPORT_SUMMARIZATION_MODEL,
            request_type="github_repo_metadata",
        )

        summary = extract_text_from_content(response.content).strip() or "No summary generated"
        _update_metadata(user_id, repo_full_name, summary, "ready")
        logger.info(f"Metadata generated for {repo_full_name} via {auth.method}")

    except Exception as e:
        logger.exception(f"Metadata generation failed for {repo_full_name}: {e}")
        try:
            self.retry(countdown=30)
        except self.MaxRetriesExceededError:
            _update_metadata(user_id, repo_full_name, None, "error")


# Backoff for a failed import: 1, 2, 4 ... capped at 30 min. Eight retries
# span about 2.5 hours, longer than the GitHub incidents that caused this.
_IMPORT_MAX_RETRIES = 8
_IMPORT_RETRY_CAP_SEC = 30 * 60


def _retry_import(task, installation_id: int, what: str) -> None:
    attempt = getattr(getattr(task, "request", None), "retries", 0) or 0
    countdown = min(60 * (2 ** attempt), _IMPORT_RETRY_CAP_SEC)
    try:
        task.retry(countdown=countdown, max_retries=_IMPORT_MAX_RETRIES)
    except task.MaxRetriesExceededError:
        logger.error(
            "[RepoImport] %s retries exhausted installation_id=%s; the hourly "
            "sync will pick it up",
            what, installation_id,
        )


def _repos_imported_at(cur, installation_id: int):
    """When the installation's first full import landed, or None."""
    cur.execute(
        """SELECT MAX(repos_imported_at)
             FROM user_github_installations
            WHERE installation_id = %s
              AND disconnected_at IS NULL""",
        (installation_id,),
    )
    row = cur.fetchone()
    return row[0] if row else None


def _import_not_wanted(installation_id: int, once_only: bool, require_enrollment: bool) -> bool:
    """True when the import should not run (see ``_import_installation_repos``)."""
    from utils.db.connection_pool import db_pool

    if once_only:
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            if _repos_imported_at(cur, installation_id) is not None:
                logger.info(
                    "[RepoImport] already imported, skipping full import installation_id=%s",
                    installation_id,
                )
                return True
    if require_enrollment:
        from tasks.github_webhook_tasks import auto_enroll_owner

        if auto_enroll_owner(installation_id) is None:
            logger.info(
                "[RepoImport] review turned off on a repo, leaving installation_id=%s",
                installation_id,
            )
            return True
    return False


def _apply_exclusions(
    cur, org_id: str, repos: list[dict], regranted: bool,
    grant_delivery_id: str | None = None,
) -> list[dict]:
    """Drop repos the org removed in the picker.

    When GitHub granted these repos again (``regranted``), exclusions older
    than the grant's webhook are cleared and the repos come back; one
    recorded after it still wins. Both times come from the database clock;
    without a known delivery the exclusion stands.
    """
    names = [r.get("full_name") for r in repos if r.get("full_name")]
    if not names:
        return repos
    if regranted:
        cur.execute(
            """DELETE FROM github_repo_exclusions
                WHERE org_id = %s AND repo_full_name = ANY(%s)
                  AND excluded_at < COALESCE(
                      (SELECT received_at FROM webhook_deliveries WHERE delivery_id = %s),
                      '-infinity'::timestamp)""",
            (org_id, names, grant_delivery_id),
        )
    cur.execute(
        """SELECT repo_full_name FROM github_repo_exclusions
            WHERE org_id = %s AND repo_full_name = ANY(%s)""",
        (org_id, names),
    )
    excluded = {row[0] for row in cur.fetchall()}
    return [r for r in repos if r.get("full_name") not in excluded]


def _disconnect_installation(installation_id: int) -> None:
    from utils.db.connection_pool import db_pool

    try:
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE user_github_installations
                      SET disconnected_at = NOW(),
                          repos_imported_at = NULL,
                          repos_import_legacy = FALSE
                    WHERE installation_id = %s
                      AND disconnected_at IS NULL""",
                (installation_id,),
            )
            conn.commit()
    except Exception:
        logger.warning(
            "[RepoImport] disconnect failed installation_id=%s", installation_id, exc_info=True,
        )


def _import_installation_repos(
    self,
    user_id: str,
    installation_id: int,
    enroll_change_gating: bool = False,
    only_repos: list[str] | None = None,
    created_after: str | None = None,
    force: bool = False,
    require_enrollment: bool = False,
    reconcile: bool = False,
    grant_delivery_id: str | None = None,
):
    """Auto-import a GitHub App installation's repos into ``connected_repos``.

    Runs after a user installs/links the App so they do not have to re-select,
    inside Aurora, the repos they already granted on GitHub. Each repo is
    UPSERTed with its ``installation_id`` set; new rows get
    ``change_gating_enabled = enroll_change_gating`` and existing rows keep
    theirs. This NEVER deletes.

    A full import (no ``only_repos`` / ``created_after``) runs once per
    link: after it lands, ``user_github_installations.repos_imported_at`` is
    set and later full imports are skipped unless ``force``. That keeps a
    returning user's login, a relink or a claim from bringing back repos
    they removed. New repos after that arrive through ``only_repos`` (an
    ``installation_repositories.added`` webhook) or ``created_after`` (the
    hourly sync).

    Repos the org removed from the picker (``github_repo_exclusions``) are
    skipped, except ``only_repos``: granting a repo on GitHub again is an
    explicit choice, so it clears the exclusion.

    ``reconcile`` (the hourly sync) imports every granted repo that is not
    connected and not excluded, without the once-per-link guard, and drops
    rows for repos GitHub no longer grants. It is
    safe once the link's removals are all recorded, i.e. not for links that
    predate the exclusions table (``repos_import_legacy``).

    ``grant_delivery_id`` is the ``installation_repositories.added`` webhook
    that granted ``only_repos``. A picker removal recorded after that
    webhook arrived wins: the queued grant is older.

    ``require_enrollment`` re-checks ``auto_enroll_owner`` when the task
    runs, so review turned off on a repo after the task was queued is
    respected.

    Metadata generation is dispatched only for genuinely new rows, and the
    LLM-usage hook inside :func:`generate_repo_metadata` still gates cost.
    """
    import time

    from utils.auth.github_app_token import (
        GitHubAppInstallationNotFound,
        GitHubAppInstallationSuspended,
        get_installation_token,
    )
    from routes.github.github_user_repos import (
        InstallationRepoListError,
        _fetch_installation_repos,
    )
    from utils.auth.stateless_auth import set_rls_context
    from utils.db.connection_pool import db_pool

    full_import = only_repos is None and created_after is None and not reconcile
    if _import_not_wanted(installation_id, full_import and not force, require_enrollment):
        return

    try:
        token = get_installation_token(installation_id)
    except GitHubAppInstallationSuspended:
        logger.info(
            "[RepoImport] installation suspended, skipping import installation_id=%s",
            installation_id,
        )
        return
    except GitHubAppInstallationNotFound:
        # Uninstalled on GitHub. Drop the links so the sync stops picking it
        # and auth falls back to OAuth (the token helper's 404 contract).
        logger.info(
            "[RepoImport] installation gone, disconnecting installation_id=%s",
            installation_id,
        )
        _disconnect_installation(installation_id)
        return
    except Exception as exc:
        logger.warning(
            "[RepoImport] installation token mint failed installation_id=%s: %s",
            installation_id, type(exc).__name__,
        )
        _retry_import(self, installation_id, "token mint")
        return

    listed_at = time.monotonic()
    try:
        repos = _fetch_installation_repos(token, installation_id, strict=True)
    except InstallationRepoListError as exc:
        # A GitHub outage is not "this install has no repos". Without the
        # retry a signup during an incident is left with nothing enrolled.
        logger.warning(
            "[RepoImport] repo listing failed installation_id=%s: %s",
            installation_id, exc,
        )
        _retry_import(self, installation_id, "repo listing")
        return
    if only_repos is not None:
        wanted = set(only_repos)
        repos = [r for r in repos if r.get("full_name") in wanted]
    if created_after is not None:
        # GitHub's created_at is ISO 8601 UTC ("...Z"); same-format strings
        # compare correctly as text.
        repos = [r for r in repos if str(r.get("created_at") or "") > created_after]

    newly_added: list[str] = []
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            org_id = set_rls_context(cur, conn, user_id, log_prefix="[RepoImport]")
            if not org_id:
                logger.warning(
                    "[RepoImport] no org for user=%s — cannot import (RLS)", user_id,
                )
                return
            if reconcile:
                # The listing is complete (strict), so it is GitHub's truth:
                # a repo no longer granted (its removed webhook lost, or it
                # raced this import) stops being shown and reviewed.
                cur.execute(
                    """DELETE FROM connected_repos
                        WHERE installation_id = %s
                          AND NOT (repo_full_name = ANY(%s))""",
                    (installation_id, [r.get("full_name") for r in repos if r.get("full_name")]),
                )
            repos = _apply_exclusions(
                cur, org_id, repos,
                regranted=only_repos is not None,
                grant_delivery_id=grant_delivery_id,
            )
            # Org-wide existing map (RLS scopes the SELECT to this org) ->
            # {repo_full_name: owner_user_id}. Mirrors save_repo_selections:
            # a repo already connected by another org member keeps its original
            # owner on the UPSERT key, so we never create a second row for the
            # same repo (which would double change-gating triggers + metadata).
            cur.execute(
                "SELECT repo_full_name, user_id FROM connected_repos "
                "WHERE provider = 'github'",
            )
            existing = {r[0]: r[1] for r in cur.fetchall()}
            for repo in repos:
                full_name = repo.get("full_name")
                if not full_name:
                    continue
                owner_id = existing.get(full_name, user_id)
                cur.execute(
                    """INSERT INTO connected_repos
                           (user_id, org_id, provider, repo_full_name, repo_id,
                            default_branch, is_private, installation_id, repo_data,
                            metadata_status, change_gating_enabled)
                       VALUES (%s, %s, 'github', %s, %s, %s, %s, %s, %s, 'pending', %s)
                       ON CONFLICT (user_id, provider, repo_full_name) DO UPDATE SET
                           repo_data = EXCLUDED.repo_data,
                           default_branch = EXCLUDED.default_branch,
                           is_private = EXCLUDED.is_private,
                           installation_id = COALESCE(EXCLUDED.installation_id,
                                                      connected_repos.installation_id),
                           updated_at = NOW()""",
                    (
                        owner_id, org_id, full_name, repo.get("id"),
                        repo.get("default_branch"), repo.get("private", False),
                        installation_id, json.dumps(repo), enroll_change_gating,
                    ),
                )
                if full_name not in existing:
                    existing[full_name] = user_id
                    newly_added.append(full_name)
            if full_import:
                # The listing's start is the baseline: repos created after
                # it are the hourly sync's to pick up.
                cur.execute(
                    """UPDATE user_github_installations
                          SET repos_imported_at =
                              (NOW() AT TIME ZONE 'UTC') - make_interval(secs => %s),
                              repos_import_legacy = FALSE
                        WHERE installation_id = %s
                          AND disconnected_at IS NULL
                          AND repos_imported_at IS NULL""",
                    (time.monotonic() - listed_at, installation_id),
                )
            conn.commit()

    logger.info(
        "[RepoImport] imported %d repo(s) (%d new) installation_id=%s user=%s",
        len(repos), len(newly_added), installation_id, user_id,
    )
    for repo_name in newly_added:
        try:
            generate_repo_metadata.delay(user_id, repo_name)
        except Exception as exc:
            logger.warning(
                "[RepoImport] metadata enqueue failed for %s: %s", repo_name, exc,
            )


@celery_app.task(
    name="routes.github.github_repo_metadata.import_installation_repos",
    bind=True,
    max_retries=_IMPORT_MAX_RETRIES,
)
def import_installation_repos(
    self,
    user_id: str,
    installation_id: int,
    enroll_change_gating: bool = False,
    only_repos: list[str] | None = None,
    created_after: str | None = None,
    force: bool = False,
    require_enrollment: bool = False,
    reconcile: bool = False,
    grant_delivery_id: str | None = None,
):
    """Celery entry point for the App repo auto-import.

    Delegates to :func:`_import_installation_repos` so the body stays unit
    testable without Celery's task machinery (which is stubbed out in the
    lightweight test env, turning a decorated task into a no-op MagicMock).
    """
    return _import_installation_repos(
        self, user_id, installation_id, enroll_change_gating,
        only_repos=only_repos, created_after=created_after,
        force=force, require_enrollment=require_enrollment,
        reconcile=reconcile, grant_delivery_id=grant_delivery_id,
    )


def _iso_utc(dt) -> str:
    from datetime import timezone

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@celery_app.task(name="routes.github.github_repo_metadata.sync_installation_repos")
def sync_installation_repos():
    """Celery entry point; see :func:`_sync_installation_repos`."""
    return _sync_installation_repos()


def _sync_installation_repos():
    """Hourly safety net: enroll repos that a webhook or an import missed.

    For every linked, unsuspended installation that qualifies for
    auto-enrollment (see ``auto_enroll_owner``):

    * the first full import never landed (GitHub was down at signup, the
      worker was lost): queue it again;
    * otherwise: reconcile, i.e. import every granted repo that is neither
      connected nor removed in the picker. That also catches a repo granted
      on GitHub whose ``added`` webhook was lost;
    * links from before removals were recorded (``repos_import_legacy``):
      only repos created on GitHub after the marker, since a repo missing
      from those may have been removed on purpose.

    Installations where someone turned review off on a repo are skipped.
    """
    from tasks.github_webhook_tasks import auto_enroll_owner
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT gi.installation_id, MAX(ugi.repos_imported_at),
                      BOOL_OR(ugi.repos_import_legacy)
                 FROM github_installations gi
                 JOIN user_github_installations ugi
                   ON ugi.installation_id = gi.installation_id
                WHERE ugi.disconnected_at IS NULL
                  AND gi.suspended_at IS NULL
                GROUP BY gi.installation_id
                ORDER BY gi.installation_id"""
        )
        installations = cur.fetchall()

    queued = 0
    for installation_id, imported_at, legacy in installations:
        try:
            owner = auto_enroll_owner(installation_id)
            if not owner:
                continue
            import_installation_repos.delay(
                owner, installation_id, enroll_change_gating=True,
                created_after=_iso_utc(imported_at) if imported_at and legacy else None,
                reconcile=bool(imported_at) and not legacy,
                require_enrollment=True,
            )
            queued += 1
        except Exception:
            logger.warning(
                "[RepoSync] skipped installation_id=%s", installation_id, exc_info=True,
            )
    logger.info(
        "[RepoSync] queued %d of %d linked installation(s)", queued, len(installations),
    )


@celery_app.task(name="routes.github.github_repo_metadata.backfill_installation_repos")
def backfill_installation_repos(
    installation_ids: list[int],
    enable_review: bool = False,
):
    """Celery entry point; see :func:`_backfill_installation_repos`."""
    return _backfill_installation_repos(installation_ids, enable_review)


def _backfill_installation_repos(installation_ids: list[int], enable_review: bool = False):
    """One-off repair for named installations, run by hand after a deploy.

    For each installation: with ``enable_review``, turn review on for every
    connected repo, overriding anyone who turned it off (for installs that
    predate review-on-by-default and never had a review), then run a full
    import even if one already landed, so repos a failed or partial import
    missed come in enrolled. Repos the org removed in the picker since the
    exclusions table existed stay out. The ids are explicit because an old
    installation with nothing connected may have been emptied on purpose.
    """
    from utils.auth.stateless_auth import set_rls_context
    from utils.db.connection_pool import db_pool

    queued = 0
    for installation_id in installation_ids:
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT ugi.user_id
                     FROM user_github_installations ugi
                     JOIN github_installations gi
                       ON gi.installation_id = ugi.installation_id
                    WHERE ugi.installation_id = %s
                      AND ugi.disconnected_at IS NULL
                      AND gi.suspended_at IS NULL
                    ORDER BY ugi.linked_at ASC, ugi.user_id ASC
                    LIMIT 1""",
                (installation_id,),
            )
            row = cur.fetchone()
            if not row:
                logger.warning(
                    "[RepoBackfill] no active link installation_id=%s", installation_id,
                )
                continue
            owner = row[0]
            if enable_review:
                if not set_rls_context(cur, conn, owner, log_prefix="[RepoBackfill]"):
                    continue
                cur.execute(
                    """UPDATE connected_repos
                          SET change_gating_enabled = TRUE, updated_at = NOW()
                        WHERE installation_id = %s
                          AND change_gating_enabled IS DISTINCT FROM TRUE""",
                    (installation_id,),
                )
                logger.info(
                    "[RepoBackfill] review turned on for %d repo(s) installation_id=%s",
                    cur.rowcount, installation_id,
                )
                conn.commit()
        import_installation_repos.delay(
            owner, installation_id, enroll_change_gating=True, force=True,
        )
        queued += 1
    logger.info(
        "[RepoBackfill] queued %d of %d installation(s)", queued, len(installation_ids),
    )
