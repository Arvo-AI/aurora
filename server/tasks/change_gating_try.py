"""Celery task for the zero-connection "try a review" preview.

Runs the production risk-review pipeline over a public PR URL or a pasted
diff — see ``services.change_gating.try_review`` for the design. This module
only does the I/O the pure layer deliberately avoids: Redis job state and the
read-only background agent run.

Job state lives at ``change_gating:try:<user_id>:<job_id>`` for one hour. The
user id is part of the key, so one user can never read another's result.
Nothing is posted to GitHub and no credentials are involved.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

from celery_config import celery_app
from services.change_gating.try_review import (
    PreviewContext,
    TryReviewError,
    load_pasted_diff,
    load_public_pr,
    run_preview,
)

logger = logging.getLogger(__name__)

JOB_TTL_SECONDS = 3600


def job_key(user_id: str, job_id: str) -> str:
    return f"change_gating:try:{user_id}:{job_id}"


def write_job(user_id: str, job_id: str, state: Dict[str, Any]) -> None:
    from utils.cache.redis_client import get_redis_client

    client = get_redis_client()
    if client is None:
        logger.warning("change_gating_try=redis_unavailable job_id=%s", job_id)
        return
    client.set(job_key(user_id, job_id), json.dumps(state), ex=JOB_TTL_SECONDS)


def read_job(user_id: str, job_id: str) -> Optional[Dict[str, Any]]:
    from utils.cache.redis_client import get_redis_client

    client = get_redis_client()
    if client is None:
        return None
    raw = client.get(job_key(user_id, job_id))
    return json.loads(raw) if raw else None


def _agent_runner(user_id: str, ctx: PreviewContext, job_id: str, session_out: Dict[str, str]):
    """Build the ``run_agent(prompt, rail_text)`` callable for ``run_preview``."""

    def run_agent(prompt: str, rail_text: str) -> Optional[str]:
        from chat.background.task import create_background_chat_session, run_background_chat
        from tasks.change_gating import _read_final_assistant_message

        trigger_metadata = {
            "source": "change_gating_try",
            "provider": "preview",
            "repo": ctx.repo_label,
            "job_id": job_id,
        }
        session_id = create_background_chat_session(
            user_id=user_id,
            title=f"Try Review: {ctx.repo_label}"[:50],
            trigger_metadata=trigger_metadata,
        )
        session_out["session_id"] = session_id
        # .result, not .get(): see the note in tasks/change_gating.py (prefork
        # workers forbid result.get() inside a task).
        result = run_background_chat.apply(
            kwargs={
                "user_id": user_id,
                "session_id": session_id,
                "initial_message": prompt,
                "trigger_metadata": trigger_metadata,
                "send_notifications": False,
                "mode": "ask",  # read-only, same as production reviews
                "rail_text": rail_text,
            }
        ).result
        if not isinstance(result, dict) or result.get("status") != "completed":
            return None
        if result.get("guardrail_blocked"):
            raise TryReviewError(
                "blocked",
                "The input guardrail blocked this content, so no review was run.",
            )
        return _read_final_assistant_message(user_id, session_id)

    return run_agent


@celery_app.task(
    bind=True,
    name="tasks.change_gating_try.run_try_review",
    max_retries=0,
    time_limit=900,
    soft_time_limit=840,
)
def run_try_review(self, user_id: str, job_id: str, mode: str, payload: str) -> Dict[str, Any]:
    """Run one preview. ``mode`` is ``"url"`` (public PR) or ``"diff"`` (pasted)."""
    start = time.time()
    write_job(user_id, job_id, {"status": "running", "stage": "fetching"})
    session_out: Dict[str, str] = {}
    try:
        ctx = load_public_pr(payload) if mode == "url" else load_pasted_diff(payload)
        write_job(user_id, job_id, {"status": "running", "stage": "reviewing", "source": ctx.source})
        result = run_preview(ctx, _agent_runner(user_id, ctx, job_id, session_out))
        result["session_id"] = session_out.get("session_id")
        result["duration_seconds"] = round(time.time() - start, 1)
        write_job(user_id, job_id, {"status": "completed", "result": result})
        logger.info(
            "change_gating_try=completed job_id=%s mode=%s verdict=%s findings=%d duration_s=%.1f",
            job_id, mode, result["verdict"], len(result["findings"]), result["duration_seconds"],
        )
        return {"status": "completed"}
    except TryReviewError as exc:
        write_job(user_id, job_id, {"status": "failed", "error": {"code": exc.code, "message": exc.message}})
        logger.info("change_gating_try=failed job_id=%s mode=%s code=%s", job_id, mode, exc.code)
        return {"status": "failed", "code": exc.code}
    except Exception as exc:  # never leak internals to the user; log for operators
        logger.exception("change_gating_try=error job_id=%s mode=%s error_class=%s", job_id, mode, type(exc).__name__)
        write_job(
            user_id,
            job_id,
            {"status": "failed", "error": {"code": "internal_error", "message": "The review failed unexpectedly. Try again."}},
        )
        return {"status": "failed", "code": "internal_error"}
