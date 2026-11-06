"""Celery task: replay Incident Prevention over a repo's reverted PRs.

Enqueued when a user turns Incident Prevention on for a repo (and on demand
from the replay route), so the first thing they see is a verdict on their own
history instead of an empty page until the next PR. See
``services.change_gating.replay`` for why reverts are the signal.

Each case runs the SAME read-only review agent a live PR gets
(``tasks.change_gating.run_review_agent``, ``mode="ask"``, the PR-review tool
set via ``source="change_gating"``), with a replay note that pins the review
to the PR as it stood before merge. Nothing is ever written to GitHub.

The report is written to Redis after every case, so the UI can show the first
verdict about two minutes in rather than waiting for all ten.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from celery_config import celery_app
from services.change_gating.replay import DEFAULT_CONTROLS, DEFAULT_REVERTED

logger = logging.getLogger(__name__)

REPORT_TTL_SECONDS = 30 * 86400
_LOCK_TTL_SECONDS = 3 * 3600


def replay_keys(org_id: Optional[str], repo_full_name: str) -> dict[str, str]:
    """Redis keys for one org's replay of one repo.

    Org-scoped: two orgs can enroll the same repo, and one org must never
    read the other's verdicts.
    """
    suffix = f"{org_id or 'no-org'}:github:{repo_full_name}"
    return {
        "report": f"change_gating:replay:report:{suffix}",
        "lock": f"change_gating:replay:lock:{suffix}",
    }


def load_report(org_id: Optional[str], repo_full_name: str) -> Optional[dict[str, Any]]:
    from utils.cache.redis_client import get_redis_client

    redis_client = get_redis_client()
    if redis_client is None:
        return None
    raw = redis_client.get(replay_keys(org_id, repo_full_name)["report"])
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def enqueue_replay(user_id: str, org_id: Optional[str], installation_id: int, repo_full_name: str) -> bool:
    """Enqueue a replay unless one is already running for this org+repo.

    Returns True when a new run was queued. The lock is taken here (not only
    in the task) so a double-click or a toggle off/on cannot queue two runs.
    """
    from utils.cache.redis_client import get_redis_client

    redis_client = get_redis_client()
    keys = replay_keys(org_id, repo_full_name)
    if redis_client is not None:
        if not redis_client.set(keys["lock"], "queued", nx=True, ex=_LOCK_TTL_SECONDS):
            return False
        redis_client.set(
            keys["report"],
            json.dumps({"repo_full_name": repo_full_name, "status": "queued"}),
            ex=REPORT_TTL_SECONDS,
        )
    replay_reverted_prs.delay(user_id, org_id, installation_id, repo_full_name)
    return True


def _save(redis_client, key: str, report: dict[str, Any]) -> None:
    if redis_client is None:
        return
    try:
        redis_client.set(key, json.dumps(report), ex=REPORT_TTL_SECONDS)
    except Exception as exc:
        logger.warning(
            "change_gating=replay status=report_save_failed error_class=%s",
            type(exc).__name__,
        )


@celery_app.task(
    bind=True,
    name="tasks.change_gating_replay.replay_reverted_prs",
    max_retries=0,
    time_limit=_LOCK_TTL_SECONDS,
    soft_time_limit=_LOCK_TTL_SECONDS - 120,
)
def replay_reverted_prs(
    self,
    user_id: str,
    org_id: Optional[str],
    installation_id: int,
    repo_full_name: str,
    reverted_limit: int = DEFAULT_REVERTED,
    control_limit: int = DEFAULT_CONTROLS,
) -> dict[str, Any]:
    """Replay the review over recent reverted + control PRs; store the report."""
    return _replay(user_id, org_id, installation_id, repo_full_name, reverted_limit, control_limit)


def _replay(
    user_id: str,
    org_id: Optional[str],
    installation_id: int,
    repo_full_name: str,
    reverted_limit: int,
    control_limit: int,
) -> dict[str, Any]:
    from services.change_gating.github_adapter import GitHubPRAdapter
    from services.change_gating.replay import run_replay
    from utils.cache.redis_client import get_redis_client

    redis_client = get_redis_client()
    keys = replay_keys(org_id, repo_full_name)
    adapter = GitHubPRAdapter(installation_id, repo_full_name)
    try:
        report = run_replay(
            adapter,
            repo_full_name,
            review=_agent_reviewer(user_id, repo_full_name),
            reverted_limit=reverted_limit,
            control_limit=control_limit,
            on_progress=lambda partial: _save(redis_client, keys["report"], partial),
        )
    except Exception as exc:
        logger.exception(
            "change_gating=replay repo=%s status=failed error_class=%s",
            repo_full_name, type(exc).__name__,
        )
        report = {
            "repo_full_name": repo_full_name,
            "status": "failed",
            "error": type(exc).__name__,
        }
    finally:
        adapter.close()
        if redis_client is not None:
            try:
                redis_client.delete(keys["lock"])
            except Exception:
                pass
    _save(redis_client, keys["report"], report)
    scores = report.get("scores") or {}
    logger.info(
        "change_gating=replay repo=%s status=%s reverted_flagged=%s/%s "
        "controls_flagged=%s/%s errors=%s duration_seconds=%s",
        repo_full_name, report.get("status"),
        scores.get("reverted_flagged"), scores.get("reverted_reviewed"),
        scores.get("controls_flagged"), scores.get("controls_reviewed"),
        scores.get("errors"), report.get("duration_seconds"),
    )
    return {"status": report.get("status"), "scores": scores}


def _agent_reviewer(user_id: str, repo_full_name: str):
    """A ``review(pr, files, diff) -> verdict`` backed by the live review agent."""
    from services.change_gating.replay import build_replay_prompt
    from tasks.change_gating import run_review_agent

    def review(pr: dict[str, Any], files: list, diff: Optional[str]) -> dict[str, Any]:
        number = pr.get("number")
        agent = run_review_agent(
            user_id,
            build_replay_prompt(repo_full_name, pr, files, diff, tools_available=True),
            rail_text=(pr.get("title") or "") + "\n\n" + (pr.get("body") or ""),
            title=f"Revert replay: {repo_full_name}#{number}",
            trigger_metadata={
                "source": "change_gating",
                "replay": True,
                "provider": "github",
                "repo": repo_full_name,
                "pr_number": number,
                "head_sha": (pr.get("head") or {}).get("sha"),
            },
            log_key="replay",
            log_ctx=f"repo={repo_full_name} pr={number}",
        )
        if agent["status"] != "completed":
            raise RuntimeError(agent["status"])
        return agent["verdict"]

    return review
