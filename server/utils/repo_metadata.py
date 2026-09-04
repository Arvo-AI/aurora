"""
Shared repo metadata generation for any VCS provider.

Fetches README + top-level file listing from the provider API,
then generates an LLM summary stored in the connected_repos table.
"""
import base64
import logging
import requests
from typing import Optional
from urllib.parse import quote

from celery_config import celery_app

logger = logging.getLogger(__name__)

METADATA_PROMPT = (
    "Write a 2-3 sentence summary of this code repository. "
    "State what it does, what services/infrastructure it contains, and key technologies. "
    "Infer from file names if no README is available. "
    "Output ONLY the summary. No notes, caveats, warnings, or markdown headers.\n\n"
    "{context}"
)

API_TIMEOUT = 15


# ---------------------------------------------------------------------------
# Provider-specific fetch functions
# ---------------------------------------------------------------------------

def _fetch_github_readme(token: str, owner: str, repo: str) -> str:
    resp = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/readme",
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"},
        timeout=API_TIMEOUT,
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


def _fetch_github_listing(token: str, owner: str, repo: str) -> str:
    resp = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/contents",
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"},
        timeout=API_TIMEOUT,
    )
    if resp.status_code != 200:
        return "(could not list files)"
    items = resp.json()
    if not isinstance(items, list):
        return "(could not list files)"
    return "\n".join(f"{'dir' if i.get('type') == 'dir' else 'file'}: {i.get('name')}" for i in items)


def _fetch_gitlab_readme(base_url: str, token: str, project_path: str) -> str:
    encoded = quote(project_path, safe="")
    for filename in ("README.md", "README.rst", "README.txt", "README"):
        encoded_file = quote(filename, safe="")
        resp = requests.get(
            f"{base_url}/api/v4/projects/{encoded}/repository/files/{encoded_file}/raw",
            headers={"PRIVATE-TOKEN": token},
            params={"ref": "HEAD"},
            timeout=API_TIMEOUT,
        )
        if resp.status_code == 200:
            return resp.text[:4000]
    return ""


def _fetch_gitlab_listing(base_url: str, token: str, project_path: str) -> str:
    encoded = quote(project_path, safe="")
    resp = requests.get(
        f"{base_url}/api/v4/projects/{encoded}/repository/tree",
        headers={"PRIVATE-TOKEN": token},
        params={"per_page": 100},
        timeout=API_TIMEOUT,
    )
    if resp.status_code != 200:
        return "(could not list files)"
    items = resp.json()
    if not isinstance(items, list):
        return "(could not list files)"
    return "\n".join(f"{'dir' if i.get('type') == 'tree' else 'file'}: {i.get('name')}" for i in items)


_README_NAMES = ("README.md", "README.rst", "README.txt", "README")
_EMPTY_SUMMARY = "No files in this repository yet."


def _parse_bitbucket_listing(result) -> str:
    if isinstance(result, dict) and "values" in result:
        items = result["values"]
        return "\n".join(
            f"{'dir' if i.get('type') == 'commit_directory' else 'file'}: {i.get('path', i.get('name', ''))}"
            for i in items[:100]
        )
    if isinstance(result, list):
        return "\n".join(
            f"{'dir' if i.get('type') == 'commit_directory' else 'file'}: {i.get('path', i.get('name', ''))}"
            for i in result[:100]
        )
    return "(could not list files)"


def _fetch_bitbucket_context(
    access_token: str, auth_type: str, workspace: str, repo_slug: str, email: Optional[str] = None
) -> tuple[str, str]:
    """README + listing in one pass. Empty repo → ("", ""). Fetch failure → ("", "(could not list files)")."""
    from connectors.bitbucket_connector.api_client import BitbucketAPIClient

    client = BitbucketAPIClient(access_token, auth_type=auth_type, email=email)
    repo = client.get_repository(workspace, repo_slug)
    if not isinstance(repo, dict) or repo.get("error"):
        return "", "(could not list files)"
    if repo.get("size") == 0:
        return "", ""
    ref = (repo.get("mainbranch") or {}).get("name") or "HEAD"
    commit = client._resolve_commit(workspace, repo_slug, ref)
    listing_raw = client.get_directory_tree(workspace, repo_slug, "", commit=commit, list_files=True)
    if isinstance(listing_raw, dict) and listing_raw.get("status") == 404:
        return "", ""
    if isinstance(listing_raw, dict) and listing_raw.get("error"):
        logger.warning(
            f"Bitbucket directory listing error for {workspace}/{repo_slug}: {listing_raw.get('status')}"
        )
        return "", "(could not list files)"
    listing = _parse_bitbucket_listing(listing_raw)
    if listing == "(could not list files)":
        return "", "(could not list files)"
    listed_names = {
        line.split(": ", 1)[1].rsplit("/", 1)[-1].lower()
        for line in listing.splitlines()
        if ": " in line
    }
    readme = ""
    for filename in _README_NAMES:
        if filename.lower() not in listed_names:
            continue
        result = client.get_file_contents(workspace, repo_slug, filename, commit=commit)
        if isinstance(result, dict):
            if result.get("error"):
                continue
            content = result.get("content")
            if isinstance(content, str) and content:
                readme = content[:4000]
                break
        elif isinstance(result, str) and result:
            readme = result[:4000]
            break
    return readme, listing


# ---------------------------------------------------------------------------
# Shared logic
# ---------------------------------------------------------------------------

def _get_credentials(user_id: str, provider: str) -> Optional[dict]:
    from utils.auth.token_management import get_token_data
    result = get_token_data(user_id, provider)
    return result if result else None


def _update_metadata(user_id: str, provider: str, repo_full_name: str, summary: Optional[str], status: str):
    from utils.db.connection_pool import db_pool
    from utils.auth.stateless_auth import set_rls_context

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            org_id = set_rls_context(cur, conn, user_id, log_prefix=f"[{provider.title()}Metadata]")
            if not org_id:
                return
            cur.execute(
                """UPDATE connected_repos
                   SET metadata_summary = %s, metadata_status = %s, updated_at = NOW()
                   WHERE provider = %s AND repo_full_name = %s""",
                (summary, status, provider, repo_full_name),
            )
            conn.commit()


def _fetch_repo_context(provider: str, creds: dict, repo_full_name: str) -> tuple[str, str]:
    """Fetch README and file listing for a repo. Returns (readme, file_listing)."""
    if provider == "github":
        token = creds["access_token"]
        parts = repo_full_name.split("/")
        if len(parts) != 2:
            return "", "(invalid repo format)"
        owner, repo = parts
        return _fetch_github_readme(token, owner, repo), _fetch_github_listing(token, owner, repo)

    elif provider == "gitlab":
        token = creds["access_token"]
        base_url = creds.get("base_url", "https://gitlab.com").rstrip("/")
        return _fetch_gitlab_readme(base_url, token, repo_full_name), _fetch_gitlab_listing(base_url, token, repo_full_name)

    elif provider == "bitbucket":
        token = creds["access_token"]
        auth_type = creds.get("auth_type", "oauth")
        email = creds.get("email")
        parts = repo_full_name.split("/")
        if len(parts) != 2:
            return "", "(invalid repo format)"
        workspace, repo_slug = parts
        return _fetch_bitbucket_context(token, auth_type, workspace, repo_slug, email)

    return "", "(unsupported provider)"


def _generate_summary(user_id: str, context: str) -> str:
    from chat.backend.agent.providers import create_chat_model
    from chat.backend.agent.llm import ModelConfig
    from chat.backend.agent.utils.llm_usage_tracker import tracked_invoke
    from langchain_core.messages import HumanMessage
    from utils.hooks import get_hook
    from chat.background.summarization import _extract_text_from_response

    # Hook: check if LLM call is allowed
    from utils.auth.stateless_auth import get_org_id_for_user
    hook_allowed, hook_message = get_hook("before_llm_call")(get_org_id_for_user(user_id), user_id)
    if not hook_allowed:
        logger.warning("Hook blocked repo metadata LLM call for user=%s: %s", user_id, hook_message)
        raise RuntimeError(f"hook_blocked: {hook_message}")

    llm = create_chat_model(
        ModelConfig.INCIDENT_REPORT_SUMMARIZATION_MODEL,
        temperature=0.2,
        streaming=False,
    )
    prompt = METADATA_PROMPT.format(context=context)
    response = tracked_invoke(
        llm,
        [HumanMessage(content=prompt)],
        user_id=user_id,
        model_name=ModelConfig.INCIDENT_REPORT_SUMMARIZATION_MODEL,
        request_type="repo_metadata",
    )
    # Gemini thinking models return content as a list of thinking/text blocks;
    # the helper drops the thinking blocks and returns the text (str content passes through).
    summary = _extract_text_from_response(response.content) if response.content else ""
    return summary or "No summary generated"


# ---------------------------------------------------------------------------
# Celery task (unified for all providers)
# ---------------------------------------------------------------------------

@celery_app.task(name="utils.repo_metadata.generate_repo_metadata", bind=True, max_retries=2)
def generate_repo_metadata(self, user_id: str, provider: str, repo_full_name: str):
    """Fetch repo info from provider API and generate an LLM summary."""
    logger.info(f"Generating metadata for {provider}:{repo_full_name} (user {user_id})")
    _update_metadata(user_id, provider, repo_full_name, None, "generating")

    try:
        creds = _get_credentials(user_id, provider)
        if not creds or not creds.get("access_token"):
            logger.error(f"No {provider} credentials for user {user_id}")
            _update_metadata(user_id, provider, repo_full_name, None, "error")
            return

        readme, file_list = _fetch_repo_context(provider, creds, repo_full_name)

        if not readme and not file_list:
            _update_metadata(user_id, provider, repo_full_name, _EMPTY_SUMMARY, "ready")
            return

        # Both README and file listing failed — nothing to summarize
        if not readme and (not file_list or file_list == "(could not list files)"):
            logger.warning(f"Could not fetch any content for {provider}:{repo_full_name}")
            _update_metadata(user_id, provider, repo_full_name, None, "error")
            return

        context_parts = []
        if readme:
            context_parts.append(f"README:\n{readme}")
        context_parts.append(f"Top-level files/directories:\n{file_list}")

        summary = _generate_summary(user_id, "\n\n".join(context_parts))
        _update_metadata(user_id, provider, repo_full_name, summary, "ready")
        logger.info(f"Metadata generated for {provider}:{repo_full_name}")

    except Exception as e:
        logger.error(f"Metadata generation failed for {provider}:{repo_full_name}: {e}", exc_info=True)
        try:
            self.retry(countdown=30)
        except self.MaxRetriesExceededError:
            _update_metadata(user_id, provider, repo_full_name, None, "error")
