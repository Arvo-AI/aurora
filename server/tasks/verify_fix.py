"""Celery task: prove a fix suggestion works before a PR is opened.

Takes an approved ``incident_suggestions`` fix, applies its content to a
checkout of the base ref and runs a repro command on both versions
(``services.fix_verification``). The verdict is cached in Redis so the UI /
PR flow can read it, and is never allowed to crash the caller: any failure
degrades to ``INCONCLUSIVE``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from celery_config import celery_app
from services.fix_verification import (
    Verdict,
    VerificationResult,
    format_comment,
    verify_patch,
)

logger = logging.getLogger(__name__)

_RESULT_TTL_SECONDS = 7 * 86400


def result_key(suggestion_id: int) -> str:
    return f"fix_verification:result:{suggestion_id}"


def run_verification(
    suggestion: dict[str, Any],
    repo_path: str,
    repro_cmd: str,
    *,
    base_ref: str = "HEAD",
    use_edited_content: bool = True,
    extra_files: Optional[dict] = None,
    runner: str = "docker",
    timeout: int = 120,
) -> dict[str, Any]:
    """Pure core (no Celery, no DB): returns a JSON-serialisable result dict."""
    content = (
        suggestion.get("user_edited_content") if use_edited_content else None
    ) or suggestion.get("suggested_content")
    file_path = suggestion.get("file_path")
    if not content or not file_path:
        return {"verdict": Verdict.INCONCLUSIVE.value,
                "summary": "suggestion has no file content or path"}
    try:
        res: VerificationResult = verify_patch(
            repo_path, base_ref, file_path, content, repro_cmd,
            extra_files=extra_files, runner=runner, timeout=timeout,
        )
    except Exception as exc:  # never break the fix flow
        logger.exception("fix_verification failed suggestion=%s", suggestion.get("id"))
        return {"verdict": Verdict.INCONCLUSIVE.value, "summary": f"verification error: {exc}"}
    out = res.to_dict()
    out["comment"] = format_comment(res)
    return out


@celery_app.task(
    bind=True,
    name="tasks.verify_fix.verify_suggestion",
    max_retries=0,
    soft_time_limit=900,
)
def verify_suggestion(
    self,
    suggestion_id: int,
    user_id: str,
    repo_path: str,
    repro_cmd: str,
    base_ref: str = "HEAD",
    runner: str = "docker",
    timeout: int = 120,
) -> dict[str, Any]:
    from chat.backend.agent.tools.apply_fix_utils import get_fix_suggestion

    suggestion = get_fix_suggestion(suggestion_id, user_id)
    if not suggestion:
        return {"verdict": Verdict.INCONCLUSIVE.value,
                "summary": "suggestion not found or access denied"}
    result = run_verification(
        suggestion, repo_path, repro_cmd,
        base_ref=base_ref, runner=runner, timeout=timeout,
    )
    try:
        from utils.cache.redis_client import get_redis_client

        client = get_redis_client()
        if client is not None:
            client.set(result_key(suggestion_id), json.dumps(result), ex=_RESULT_TTL_SECONDS)
    except Exception:
        logger.warning("fix_verification could not cache result suggestion=%s", suggestion_id)
    logger.info("fix_verification=verify_suggestion suggestion_id=%s verdict=%s",
                suggestion_id, result["verdict"])
    return result