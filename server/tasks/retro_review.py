"""Celery task: run a retro review job end to end.

Flow (one job = one public repo):
1. Find merged PRs that were later reverted, plus a control set of merged
   PRs that were not. Keep only PRs that touch infra/deploy files (the risk
   Aurora's review is built for), scanning extra candidates to fill each set.
2. For each PR, rebuild the live pre-merge review prompt from what existed
   at merge time (title, description, diff) and run it through the review
   model with NO tools, so it cannot look up what happened afterwards.
3. Parse the verdict with the live review's parser and save the result
   after every PR, so the UI shows progress.
4. Score: reverted PRs called RISKY are catches; control PRs called RISKY
   are false alarms.

Nothing is posted anywhere: the job only reads GitHub and writes its own row.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from celery_config import celery_app
from services.change_gating import retro
from services.change_gating.public_github_client import (
    GitHubRateLimited,
    GitHubRepoNotFound,
    PublicGitHubClient,
)
from utils.auth.stateless_auth import set_rls_context
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

_LOG = "[RetroReview]"
# Scan up to this many candidates per wanted PR, since out-of-scope PRs
# (no infra/deploy files) are dropped. Each scanned PR costs ~2 GitHub calls
# but no model call.
SCAN_FACTOR = 3
# Statuses that use up a set's quota: the PR was in scope and we tried it.
_IN_SCOPE = ("reviewed", "error")
# Pass as a field value to set that column to the database's current time.
NOW = object()


# ----------------------------------------------------------------------
# Persistence
# ----------------------------------------------------------------------

def _update_job(job_id: str, user_id: str, **fields: Any) -> None:
    """UPDATE retro_reviews SET <fields> for one job, under RLS."""
    if not fields:
        return
    columns, values = [], []
    for key, value in fields.items():
        if value is NOW:
            columns.append(f"{key} = CURRENT_TIMESTAMP")
            continue
        if key in ("items", "summary"):
            value = json.dumps(value)
        columns.append(f"{key} = %s")
        values.append(value)
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            if not set_rls_context(cursor, conn, user_id, log_prefix=_LOG):
                raise RuntimeError("could not set RLS context")
            cursor.execute(
                f"UPDATE retro_reviews SET {', '.join(columns)} WHERE id = %s",  # noqa: S608 — column names are literals above
                (*values, job_id),
            )
        conn.commit()


def _load_job(job_id: str, user_id: str) -> Optional[Dict[str, Any]]:
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            if not set_rls_context(cursor, conn, user_id, log_prefix=_LOG):
                return None
            cursor.execute(
                "SELECT repo, max_reverted, max_controls FROM retro_reviews WHERE id = %s",
                (job_id,),
            )
            row = cursor.fetchone()
    if not row:
        return None
    return {"repo": row[0], "max_reverted": row[1], "max_controls": row[2]}


# ----------------------------------------------------------------------
# Review one PR
# ----------------------------------------------------------------------

def _message_text(message: Any) -> str:
    """LLM message content may be a string or a list of content blocks."""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
        )
    return str(content or "")


def _call_review_model(prompt: str) -> str:
    """One tool-less call to the same model the live PR review runs on."""
    from langchain_core.messages import HumanMessage

    from chat.backend.agent.llm import ModelConfig
    from chat.backend.agent.providers import create_chat_model

    llm = create_chat_model(ModelConfig.RCA_MODEL, temperature=0.0, streaming=False)
    return _message_text(llm.invoke([HumanMessage(content=prompt)]))


def _parse(text: str) -> Optional[Dict[str, Any]]:
    from services.change_gating.verdict import extract_verdict_with_llm, parse_verdict

    return parse_verdict(text) or extract_verdict_with_llm(text)


def review_pr(client: PublicGitHubClient, repo: str, item: Dict[str, Any]) -> Dict[str, Any]:
    """Fill in one item's PR metadata and verdict. Never raises for one bad PR."""
    item = dict(item)
    number = item["pr_number"]
    try:
        pr = client.get_pull_request(number)
        item.update({
            "title": pr.get("title") or "",
            "url": pr.get("html_url"),
            "merged_at": pr.get("merged_at"),
            "author": (pr.get("user") or {}).get("login"),
        })
        if not pr.get("merged_at"):
            item.update(status="skipped", reason="not merged")
            return item

        files = client.list_files(number)
        scoped = retro.infra_files(files)
        if not scoped:
            # Not the kind of change Aurora reviews: no model call, no score.
            item.update(status="out_of_scope", reason="no infra or deploy files changed")
            return item
        item["infra_files"] = scoped[:10]

        has_patches = any(f.get("patch") for f in files)
        diff = None if has_patches else client.get_diff(number)
        # Large diffs are truncated by the live review's own diff builder
        # (60k chars total, 15k per file), so the retro sees what a live
        # review of the same PR would have seen.
        prompt = retro.build_retro_prompt(repo, pr, files, diff)

        verdict = _parse(_call_review_model(prompt))
        if not verdict:
            item.update(status="error", reason="could not parse a verdict")
            return item

        verdict = retro.normalize_verdict(verdict)
        item.update(
            status="reviewed",
            verdict=verdict["verdict"],
            summary=verdict.get("summary", ""),
            findings=verdict.get("findings", []),
        )
    except GitHubRateLimited:
        raise  # the whole job stops; later PRs would fail the same way
    except Exception as exc:  # noqa: BLE001 — one PR must not sink the job
        logger.warning("%s repo=%s pr=%s failed: %s", _LOG, repo, number, type(exc).__name__)
        item.update(status="error", reason="review failed")
    return item


# ----------------------------------------------------------------------
# Task
# ----------------------------------------------------------------------

@celery_app.task(
    bind=True,
    name="tasks.retro_review.run_retro_review",
    time_limit=3600,
    soft_time_limit=3540,
)
def run_retro_review(self, job_id: str, user_id: str) -> Dict[str, Any]:
    job = _load_job(job_id, user_id)
    if not job:
        logger.error("%s job %s not found", _LOG, job_id)
        return {"status": "not_found"}

    repo = job["repo"]
    _update_job(job_id, user_id, status="running", started_at=NOW)
    client = PublicGitHubClient(repo)
    items: List[Dict[str, Any]] = []
    try:
        client.get_repo()
        reverted = retro.find_reverted(
            repo, client.search_merged_revert_prs(), job["max_reverted"] * SCAN_FACTOR
        )
        if not reverted:
            _update_job(
                job_id, user_id, status="failed", completed_at=NOW,
                error="No reverted PRs found in this repo. Try a larger or busier repository.",
            )
            return {"status": "no_reverts"}

        exclude = {i["pr_number"] for i in reverted} | {
            i["revert_pr_number"] for i in reverted
        }
        controls = retro.pick_controls(
            client.list_recent_merged_prs(), exclude, job["max_controls"] * SCAN_FACTOR
        )

        for candidates, want in ((reverted, job["max_reverted"]), (controls, job["max_controls"])):
            done = 0
            for candidate in candidates:
                if done >= want:
                    break
                items.append({**candidate, "status": "pending"})
                _update_job(job_id, user_id, items=items)
                items[-1] = review_pr(client, repo, candidate)
                if items[-1]["status"] in _IN_SCOPE:
                    done += 1
                _update_job(job_id, user_id, items=items, summary=retro.score(items))

        if not any(i["kind"] == "reverted" and i["status"] in _IN_SCOPE for i in items):
            _update_job(
                job_id, user_id, status="failed", completed_at=NOW, summary=retro.score(items),
                error="None of this repo's reverted PRs touched infra or deploy files, "
                      "which is what Aurora reviews for.",
            )
            return {"status": "no_infra_reverts"}

        _update_job(
            job_id, user_id, status="completed", completed_at=NOW,
            summary=retro.score(items),
        )
        logger.info("%s job %s repo=%s completed: %s", _LOG, job_id, repo, retro.score(items))
        return {"status": "completed"}
    except (GitHubRateLimited, GitHubRepoNotFound) as exc:
        _update_job(job_id, user_id, status="failed", completed_at=NOW,
                    error=str(exc), items=items, summary=retro.score(items))
        return {"status": "failed"}
    except Exception as exc:  # noqa: BLE001
        logger.exception("%s job %s failed", _LOG, job_id)
        _update_job(job_id, user_id, status="failed", completed_at=NOW,
                    error=f"Retro review failed ({type(exc).__name__})",
                    items=items, summary=retro.score(items))
        return {"status": "failed"}
    finally:
        client.close()
