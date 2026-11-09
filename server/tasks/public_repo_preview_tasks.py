"""Background execution for the public GitHub reliability preview."""

from __future__ import annotations

import json
import logging
import time

from celery_config import celery_app

from services.public_repo_preview import (
    PublicRepoPreviewError,
    build_reliability_preview_prompt,
    build_reliability_preview_rail_text,
    fetch_public_repository,
)
from utils.log_sanitizer import hash_for_log, sanitize

logger = logging.getLogger(__name__)

_GENERIC_FAILURE = "Aurora couldn't complete this preview. Please try again."


def persist_preview_failure(session_id: str, user_id: str, message: str) -> None:
    """Append a safe visible error and move the preview session out of progress."""
    from utils.auth.stateless_auth import set_rls_context
    from utils.db.connection_pool import db_pool

    safe_message = message or _GENERIC_FAILURE
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                if not set_rls_context(
                    cursor, conn, user_id, log_prefix="[PublicRepoPreview]"
                ):
                    return
                cursor.execute(
                    """
                    UPDATE chat_sessions
                       SET messages = COALESCE(messages, '[]'::jsonb) || %s::jsonb,
                           status = 'error',
                           updated_at = NOW()
                     WHERE id = %s AND user_id = %s AND is_active = true
                    """,
                    (
                        json.dumps([{"sender": "bot", "text": safe_message}]),
                        session_id,
                        user_id,
                    ),
                )
            conn.commit()
    except Exception:
        # Persistence failure must not leak the original repository/API error
        # or cause a retry loop that duplicates the visible message.
        logger.warning(
            "preview=failure_persist_failed session=%s",
            hash_for_log(session_id),
        )


@celery_app.task(name="tasks.public_repo_preview.run_public_repo_preview")
def run_public_repo_preview(
    user_id: str,
    session_id: str,
    repository_url: str,
    repository: str,
    submitted_at: str,
) -> dict:
    """Fetch a bounded public snapshot and run Aurora's existing read-only agent."""
    started = time.monotonic()
    session_hash = hash_for_log(session_id)

    try:
        snapshot = fetch_public_repository(repository_url)
        prompt = build_reliability_preview_prompt(snapshot)
        rail_text = build_reliability_preview_rail_text(snapshot)
    except PublicRepoPreviewError as exc:
        persist_preview_failure(session_id, user_id, exc.public_message)
        duration_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "preview=failed phase=fetch repository=%s session=%s "
            "duration_ms=%d error_class=%s",
            sanitize(repository),
            session_hash,
            duration_ms,
            type(exc).__name__,
        )
        return {
            "status": "failed",
            "session_id": session_id,
            "repository": repository,
        }
    except Exception as exc:
        persist_preview_failure(session_id, user_id, _GENERIC_FAILURE)
        duration_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "preview=failed phase=prepare repository=%s session=%s "
            "duration_ms=%d error_class=%s",
            sanitize(repository),
            session_hash,
            duration_ms,
            type(exc).__name__,
        )
        return {
            "status": "failed",
            "session_id": session_id,
            "repository": repository,
        }

    trigger_metadata = {
        "source": "public_repo_preview",
        "initial_message_is_scaffold": True,
        "repository": snapshot.get("full_name") or repository,
        "submitted_at": submitted_at,
        "revision": snapshot.get("revision"),
    }

    from chat.background.task import run_background_chat

    try:
        result = run_background_chat.apply(
            kwargs={
                "user_id": user_id,
                "session_id": session_id,
                "initial_message": prompt,
                "trigger_metadata": trigger_metadata,
                "send_notifications": False,
                "mode": "ask",
                "rail_text": rail_text,
            }
        ).result
    except Exception as exc:
        result = exc

    duration_ms = int((time.monotonic() - started) * 1000)
    if (
        isinstance(result, dict)
        and result.get("status") == "completed"
        and not result.get("guardrail_blocked")
    ):
        logger.info(
            "preview=completed repository=%s session=%s duration_ms=%d",
            sanitize(repository),
            session_hash,
            duration_ms,
        )
        return {
            "status": "completed",
            "session_id": session_id,
            "repository": repository,
        }

    # A task-level or agent failure should never leave an empty/stuck session.
    # The visible message is deliberately generic; result/exception details can
    # contain repository content or provider responses and are never logged.
    persist_preview_failure(session_id, user_id, _GENERIC_FAILURE)
    logger.info(
        "preview=failed phase=agent repository=%s session=%s "
        "duration_ms=%d error_class=%s",
        sanitize(repository),
        session_hash,
        duration_ms,
        type(result).__name__,
    )
    return {
        "status": "failed",
        "session_id": session_id,
        "repository": repository,
    }
