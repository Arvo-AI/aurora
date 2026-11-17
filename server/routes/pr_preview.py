"""No-install PR preview: Aurora's pre-merge review on any public GitHub PR.

Seeing a pre-merge review today means installing the GitHub App, passing
GitHub's permission screen and waiting for the next pull request. These
routes let a signed-in user paste a public PR link instead and watch the
same read-only review run in a chat session. Nothing is posted to GitHub.

POST /api/pr-preview               {"url": "https://github.com/o/r/pull/1"}
    -> 202 {"session_id", "repo", "pr_number", "title"}
GET  /api/pr-preview/<session_id>  -> {"status", "verdict", "repo", "pr_number"}
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from flask import Blueprint, jsonify, request

from services.change_gating.public_github import PublicGitHubReader, parse_pr_url
from services.change_gating.verdict import final_assistant_text, parse_verdict
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request, set_rls_context
from utils.db.connection_pool import db_pool
from utils.flags.feature_flags import is_incident_prevention_enabled

logger = logging.getLogger(__name__)

pr_preview_bp = Blueprint("pr_preview", __name__, url_prefix="/api/pr-preview")

_BAD_URL = "Paste a GitHub pull request link, like https://github.com/owner/repo/pull/123."
_NOT_FOUND = (
    "Pull request not found. Only public repositories can be reviewed "
    "without installing the GitHub App."
)
_GITHUB_LIMIT = "GitHub's limit for anonymous requests was reached. Try again in a few minutes."
_AURORA_LIMIT = "Too many investigations started recently. Try again in a few minutes."
_DISABLED = "PR review (Incident Prevention) is turned off on this deployment."
_MONTHLY_LIMIT_REACHED = (
    "You've used this month's free PR reviews. Connect GitHub to review every pull "
    "request in your repositories, or book a call with the team."
)

# Each preview is a full agent run paid by the deployment's LLM key, so on top
# of the shared background-chat limit (5 per 5 minutes) one user gets at most
# this many per calendar month (UTC).
MONTHLY_PREVIEW_LIMIT = 10
# Longer than any month, so a counter always outlives the month it counts.
_MONTHLY_KEY_TTL_SECONDS = 32 * 24 * 60 * 60


def _take_monthly_preview(user_id: str) -> bool:
    """Count one preview for ``user_id``; False once this month's limit is used up.

    Fails open when Redis is unavailable: the per-user background-chat limit
    and the ``before_llm_call`` hook still apply to the run itself.
    """
    from utils.cache.redis_client import get_redis_client

    redis_client = get_redis_client()
    if redis_client is None:
        return True
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    key = f"pr_preview:monthly:{user_id}:{month}"
    try:
        count = redis_client.incr(key)
        if count == 1:
            redis_client.expire(key, _MONTHLY_KEY_TTL_SECONDS)
    except Exception as exc:
        logger.warning("[PRPreview] monthly limit check failed: error_class=%s", type(exc).__name__)
        return True
    return count <= MONTHLY_PREVIEW_LIMIT


def _github_failure(exc: Exception):
    """Map a failed PR lookup to a response the user can act on."""
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code == 404:
        return jsonify({"error": _NOT_FOUND}), 404
    if status_code == 429 or (
        status_code == 403
        and (
            (getattr(response, "headers", None) or {}).get("X-RateLimit-Remaining") == "0"
            or "rate limit" in (getattr(response, "text", None) or "").lower()
        )
    ):
        return jsonify({"error": _GITHUB_LIMIT}), 429
    if status_code in (401, 403, 451):
        return jsonify({"error": _NOT_FOUND}), 404
    logger.warning(
        "[PRPreview] GitHub lookup failed: code=%s error_class=%s",
        status_code, type(exc).__name__,
    )
    return jsonify({"error": "Could not reach GitHub. Try again in a moment."}), 502


def _display_verdict(final_text: Optional[str]) -> Optional[Dict[str, Any]]:
    """Parse the agent's final message the way the installed flow does.

    Same normalization as ``tasks.change_gating._run_investigation_core``:
    SAFE never carries findings, and RISKY without findings becomes SAFE.
    """
    verdict = parse_verdict(final_text)
    if verdict and (verdict["verdict"] == "SAFE" or not verdict["findings"]):
        verdict = {**verdict, "verdict": "SAFE", "findings": []}
    return verdict


@pr_preview_bp.route("", methods=["POST"])
@require_permission("chat", "write")
def start_preview(user_id):
    """Validate the link, check the PR is public, then start the review."""
    if not is_incident_prevention_enabled():
        return jsonify({"error": _DISABLED}), 404
    parsed = parse_pr_url((request.get_json(silent=True) or {}).get("url"))
    if not parsed:
        return jsonify({"error": _BAD_URL}), 400
    repo_full_name, pr_number = parsed

    reader = PublicGitHubReader(repo_full_name)
    try:
        pr = reader.get_pull_request(pr_number)
    except Exception as exc:
        return _github_failure(exc)
    finally:
        reader.close()

    from chat.background.task import (
        create_background_chat_session,
        is_background_chat_allowed,
    )
    from tasks.change_gating import preview_public_pr

    # Only links that resolve to a real public PR count toward the monthly limit.
    if not _take_monthly_preview(user_id):
        return jsonify({"error": _MONTHLY_LIMIT_REACHED, "limit_reached": True}), 429
    if not is_background_chat_allowed(user_id):
        return jsonify({"error": _AURORA_LIMIT}), 429

    try:
        session_id = create_background_chat_session(
            user_id=user_id,
            title=f"PR Risk Review (preview): {repo_full_name}#{pr_number}",
            trigger_metadata={
                "source": "change_gating",
                "provider": "github",
                "preview": True,
                "repo": repo_full_name,
                "pr_number": pr_number,
            },
        )
    except Exception:
        logger.exception("[PRPreview] Failed to create session for %s#%s", repo_full_name, pr_number)
        return jsonify({"error": "Failed to start the review"}), 500
    preview_public_pr.delay(
        user_id=user_id,
        session_id=session_id,
        repo_full_name=repo_full_name,
        pr_number=pr_number,
    )
    logger.info(
        "[PRPreview] started repo=%s pr=%s session_id=%s",
        repo_full_name, pr_number, session_id,
    )
    return jsonify({
        "session_id": session_id,
        "repo": repo_full_name,
        "pr_number": pr_number,
        "title": pr.get("title"),
    }), 202


@pr_preview_bp.route("/<session_id>", methods=["GET"])
@require_permission("chat", "read")
def preview_status(user_id, session_id):
    """Report the review's progress and, once it finishes, its verdict."""
    try:
        uuid.UUID(session_id)
    except ValueError:
        return jsonify({"error": "Preview not found"}), 404

    org_id = get_org_id_from_request()
    try:
        with db_pool.get_user_connection() as conn:
            cursor = conn.cursor()
            set_rls_context(cursor, conn, user_id, org_id=org_id, log_prefix="[PRPreview]")
            cursor.execute(
                "SELECT status, messages, ui_state FROM chat_sessions WHERE id = %s AND user_id = %s",
                (session_id, user_id),
            )
            row = cursor.fetchone()
    except Exception:
        logger.exception("[PRPreview] Failed to load session %s", session_id)
        return jsonify({"error": "Failed to load preview"}), 500

    if not row:
        return jsonify({"error": "Preview not found"}), 404
    status, messages, ui_state = row
    if isinstance(ui_state, str):
        ui_state = json.loads(ui_state)
    trigger = (ui_state or {}).get("triggerMetadata") or {}
    if not trigger.get("preview"):
        return jsonify({"error": "Preview not found"}), 404

    verdict = None
    if status == "completed":
        try:
            verdict = _display_verdict(final_assistant_text(messages))
        except ValueError:  # malformed messages JSON
            verdict = None

    return jsonify({
        "status": status or "in_progress",
        "verdict": verdict,
        "repo": trigger.get("repo"),
        "pr_number": trigger.get("pr_number"),
    })
