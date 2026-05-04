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
import logging
from typing import Any
import requests
from celery_config import celery_app

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
    from utils.db.connection_pool import db_pool
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            from utils.auth.stateless_auth import set_rls_context
            if not set_rls_context(cur, conn, user_id, log_prefix="[GitHubMetadata]"):
                return
            cur.execute(
                """UPDATE github_connected_repos
                   SET metadata_summary = %s, metadata_status = %s, updated_at = NOW()
                   WHERE user_id = %s AND repo_full_name = %s""",
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
    _update_metadata(user_id, repo_full_name, None, "generating")

    try:
        try:
            auth = get_auth_for_user_repo(user_id, repo_full_name)
        except NoGitHubAuthError as exc:
            logger.error(
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

        summary = response.content.strip() if response.content else "No summary generated"
        _update_metadata(user_id, repo_full_name, summary, "ready")
        logger.info(f"Metadata generated for {repo_full_name} via {auth.method}")

    except Exception as e:
        logger.error(f"Metadata generation failed for {repo_full_name}: {e}", exc_info=True)
        try:
            self.retry(countdown=30)
        except self.MaxRetriesExceededError:
            _update_metadata(user_id, repo_full_name, None, "error")
