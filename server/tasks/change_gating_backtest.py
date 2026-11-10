"""Celery task for the revert backtest (see ``services.change_gating.backtest``).

Each PR review runs as its own task so the UI can show results as they land.
Job state lives in Redis under a user-scoped key; the route only ever reads
keys for the caller's own user_id.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

from celery_config import celery_app

logger = logging.getLogger(__name__)

JOB_TTL_SECONDS = 3600


def backtest_job_key(user_id: str, job_id: str) -> str:
    return f"change_gating:backtest:{user_id}:{job_id}"


def save_job(user_id: str, job_id: str, state: Dict[str, Any]) -> None:
    from utils.cache.redis_client import get_redis_client

    client = get_redis_client()
    if client is None:
        raise RuntimeError("Redis unavailable")
    client.set(backtest_job_key(user_id, job_id), json.dumps(state), ex=JOB_TTL_SECONDS)


def load_job(user_id: str, job_id: str) -> Optional[Dict[str, Any]]:
    from utils.cache.redis_client import get_redis_client

    client = get_redis_client()
    raw = client.get(backtest_job_key(user_id, job_id)) if client is not None else None
    return json.loads(raw) if raw else None


@celery_app.task(name="tasks.change_gating_backtest.backtest_pr", time_limit=300, soft_time_limit=270)
def backtest_pr(user_id: str, job_id: str, repo_full_name: str, pr_number: int, kind: str) -> Dict[str, Any]:
    from services.change_gating.backtest import BacktestError, PublicGitHubClient, review_pr

    start = time.monotonic()
    base = {"repo": repo_full_name, "pr_number": pr_number, "kind": kind}
    client = PublicGitHubClient()
    try:
        result = review_pr(client, repo_full_name, pr_number)
        state = {**base, **result, "status": "completed"}
    except BacktestError as exc:
        state = {**base, "status": "failed", "error": str(exc)}
    except Exception as exc:
        logger.exception("[Backtest] review failed repo=%s pr=%s", repo_full_name, pr_number)
        state = {**base, "status": "failed", "error": f"Review failed ({type(exc).__name__})"}
    finally:
        client.close()

    duration_ms = int((time.monotonic() - start) * 1000)
    state["duration_ms"] = duration_ms
    save_job(user_id, job_id, state)
    logger.info(
        "backtest_event=review_finished user=%s repo=%s pr=%s kind=%s status=%s verdict=%s duration_ms=%d",
        user_id, repo_full_name, pr_number, kind, state["status"], state.get("verdict"), duration_ms,
    )
    return {"status": state["status"], "verdict": state.get("verdict")}
