"""
Celery task to generate LLM-powered metadata summaries for connected GitHub repos.
Fetches README + top-level directory listing via GitHub REST API, summarizes with LLM.
"""
import base64
import logging
import requests
from celery_config import celery_app

logger = logging.getLogger(__name__)

METADATA_PROMPT = (
    "You are summarizing a GitHub repository for an SRE/DevOps team. "
    "Based on the available information below, write a 2-3 sentence summary of: "
    "what this repo does, what services or infrastructure it manages, "
    "and key technologies used. Be concise and specific. "
    "If no README is provided, infer from the file and directory names.\n\n"
    "{context}"
)


def _get_github_token(user_id: str) -> str | None:
    from utils.auth.stateless_auth import get_credentials_from_db
    creds = get_credentials_from_db(user_id, "github")
    return creds.get("access_token") if creds else None


def _fetch_readme(token: str, owner: str, repo: str) -> str:
    resp = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/readme",
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"},
        timeout=15,
    )
    if resp.status_code != 200:
        return ""
    content = resp.json().get("content", "")
    try:
        decoded = base64.b64decode(content).decode("utf-8", errors="replace")
        return decoded[:4000]
    except Exception:
        return ""


def _fetch_top_level_listing(token: str, owner: str, repo: str) -> str:
    resp = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/contents",
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"},
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
    logger.info(f"Generating metadata for {repo_full_name} (user {user_id})")
    _update_metadata(user_id, repo_full_name, None, "generating")

    try:
        token = _get_github_token(user_id)
        if not token:
            _update_metadata(user_id, repo_full_name, None, "error")
            return

        parts = repo_full_name.split("/")
        if len(parts) != 2:
            _update_metadata(user_id, repo_full_name, None, "error")
            return
        owner, repo = parts

        readme = _fetch_readme(token, owner, repo)
        file_list = _fetch_top_level_listing(token, owner, repo)

        context_parts = []
        if readme:
            context_parts.append(f"README:\n{readme}")
        context_parts.append(f"Top-level files/directories:\n{file_list}")

        from chat.backend.agent.providers import create_chat_model
        from chat.backend.agent.llm import ModelConfig
        from langchain_core.messages import HumanMessage

        llm = create_chat_model(
            ModelConfig.INCIDENT_REPORT_SUMMARIZATION_MODEL,
            temperature=0.2,
            streaming=False,
        )

        prompt = METADATA_PROMPT.format(context="\n\n".join(context_parts))
        response = llm.invoke([HumanMessage(content=prompt)])

        summary = response.content.strip() if response.content else "No summary generated"
        _update_metadata(user_id, repo_full_name, summary, "ready")
        logger.info(f"Metadata generated for {repo_full_name}")

    except Exception as e:
        logger.error(f"Metadata generation failed for {repo_full_name}: {e}", exc_info=True)
        try:
            self.retry(countdown=30)
        except self.MaxRetriesExceededError:
            _update_metadata(user_id, repo_full_name, None, "error")
