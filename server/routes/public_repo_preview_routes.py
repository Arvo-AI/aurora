"""Low-friction reliability preview for a public GitHub repository."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from services.public_repo_preview import PublicRepoPreviewError, parse_public_github_url
from utils.auth.rbac_decorators import require_permission
from utils.log_sanitizer import hash_for_log, sanitize
from utils.web.limiter_ext import limiter

logger = logging.getLogger(__name__)

public_repo_preview_bp = Blueprint("public_repo_preview", __name__)


@public_repo_preview_bp.route("/api/public-repo-preview", methods=["POST"])
@limiter.limit("5 per minute;20 per hour")
@require_permission("chat", "write")
def start_public_repo_preview(user_id: str):
    return _start_public_repo_preview(user_id)


def _start_public_repo_preview(user_id: str):
    """Create a visible chat session and enqueue a public-repository preview."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON body"}), 400

    repository_url = data.get("repository_url")
    if not isinstance(repository_url, str) or not repository_url.strip():
        return jsonify({"error": "repository_url is required"}), 400

    try:
        owner, repo = parse_public_github_url(repository_url)
    except PublicRepoPreviewError as exc:
        return jsonify({"error": exc.public_message}), exc.status_code

    repository = f"{owner}/{repo}"
    canonical_url = f"https://github.com/{repository}"
    submitted_at = datetime.now(timezone.utc).isoformat()
    trigger_metadata = {
        "source": "public_repo_preview",
        "repository": repository,
        "submitted_at": submitted_at,
    }

    from chat.background.task import create_background_chat_session

    try:
        session_id = create_background_chat_session(
            user_id=user_id,
            title=f"Reliability preview: {repository}",
            question=f"Analyze public repository: {canonical_url}",
            trigger_metadata=trigger_metadata,
        )
    except Exception as exc:
        logger.warning(
            "preview=failed phase=create_session repository=%s error_class=%s",
            sanitize(repository),
            type(exc).__name__,
        )
        return jsonify({"error": "Could not start the repository preview"}), 500

    logger.info(
        "preview=submitted repository=%s session=%s submitted_at=%s",
        sanitize(repository),
        hash_for_log(session_id),
        submitted_at,
    )

    from tasks import public_repo_preview_tasks

    try:
        public_repo_preview_tasks.run_public_repo_preview.delay(
            user_id=user_id,
            session_id=session_id,
            repository_url=canonical_url,
            repository=repository,
            submitted_at=submitted_at,
        )
    except Exception as exc:
        # The session is already visible. Make the dispatch failure visible too,
        # instead of leaving an in-progress row that can never finish.
        public_repo_preview_tasks.persist_preview_failure(
            session_id,
            user_id,
            "Aurora couldn't start this preview. Please try again.",
        )
        logger.warning(
            "preview=failed phase=enqueue repository=%s session=%s "
            "duration_ms=0 error_class=%s",
            sanitize(repository),
            hash_for_log(session_id),
            type(exc).__name__,
        )
        return jsonify({"error": "Could not start the repository preview"}), 503

    return jsonify({
        "session_id": session_id,
        "repository": repository,
        "status": "in_progress",
    }), 202
