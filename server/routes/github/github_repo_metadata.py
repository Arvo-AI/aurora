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
from typing import Any, List, Union
import requests
from celery_config import celery_app

logger = logging.getLogger(__name__)


def _extract_text_from_response(content: Union[str, List[Any]]) -> str:
    """Extract text from a LangChain AIMessage content payload."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        text_parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") in ("thinking", "reasoning"):
                    continue
                text = part.get("text", "")
                if text:
                    text_parts.append(str(text))
            elif isinstance(part, str):
                text_parts.append(part)
        return "".join(text_parts).strip()
    return str(content).strip()

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

        summary = _extract_text_from_response(response.content) if response.content else "No summary generated"
        if not summary:
            summary = "No summary generated"
        _update_metadata(user_id, repo_full_name, summary, "ready")
        logger.info(f"Metadata generated for {repo_full_name} via {auth.method}")

    except Exception as e:
        logger.exception(f"Metadata generation failed for {repo_full_name}: {e}")
        try:
            self.retry(countdown=30)
        except self.MaxRetriesExceededError:
            _update_metadata(user_id, repo_full_name, None, "error")


def _import_installation_repos(self, user_id: str, installation_id: int):
    """Auto-import a GitHub App installation's repos into ``connected_repos``.

    Runs after a user installs/links the App so they do not have to re-select,
    inside Aurora, the repos they already granted on GitHub (the redundant
    second selection). Idempotent: each repo is UPSERTed with its
    ``installation_id`` set and ``change_gating_enabled`` left at its default
    (FALSE — change gating is still opt-in per repo). Unlike the manual save
    path this NEVER deletes, so prior manual selections and other installations
    are preserved; the user can still trim the set from the repo picker.

    Metadata generation is dispatched only for genuinely new rows, and the
    LLM-usage hook inside :func:`generate_repo_metadata` still gates cost.
    """
    from utils.auth.github_app_token import (
        GitHubAppInstallationSuspended,
        get_installation_token,
    )
    from routes.github.github_user_repos import _fetch_installation_repos
    from utils.auth.stateless_auth import set_rls_context
    from utils.db.connection_pool import db_pool

    try:
        token = get_installation_token(installation_id)
    except GitHubAppInstallationSuspended:
        logger.info(
            "[RepoImport] installation suspended, skipping import installation_id=%s",
            installation_id,
        )
        return
    except Exception as exc:
        logger.warning(
            "[RepoImport] installation token mint failed installation_id=%s: %s",
            installation_id, type(exc).__name__,
        )
        try:
            self.retry(countdown=30)
        except self.MaxRetriesExceededError:
            logger.warning(
                "[RepoImport] token mint retries exhausted installation_id=%s; giving up",
                installation_id,
            )
        return

    repos = _fetch_installation_repos(token, installation_id)
    if not repos:
        logger.info(
            "[RepoImport] no repos to import installation_id=%s user=%s",
            installation_id, user_id,
        )
        return

    newly_added: list[str] = []
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            org_id = set_rls_context(cur, conn, user_id, log_prefix="[RepoImport]")
            if not org_id:
                logger.warning(
                    "[RepoImport] no org for user=%s — cannot import (RLS)", user_id,
                )
                return
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
                            metadata_status)
                       VALUES (%s, %s, 'github', %s, %s, %s, %s, %s, %s, 'pending')
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
                        installation_id, json.dumps(repo),
                    ),
                )
                if full_name not in existing:
                    existing[full_name] = user_id
                    newly_added.append(full_name)
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
    max_retries=2,
)
def import_installation_repos(self, user_id: str, installation_id: int):
    """Celery entry point for the App repo auto-import.

    Delegates to :func:`_import_installation_repos` so the body stays unit
    testable without Celery's task machinery (which is stubbed out in the
    lightweight test env, turning a decorated task into a no-op MagicMock).
    """
    return _import_installation_repos(self, user_id, installation_id)
