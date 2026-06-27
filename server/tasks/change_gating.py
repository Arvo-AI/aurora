"""Celery task for PR Change Gating: agentic pre-merge risk review.

When ``_handle_pull_request_event`` (``tasks/github_webhook_tasks.py``) sees
a qualifying ``pull_request`` webhook for an enrolled repo's default branch,
it enqueues :func:`investigate_pr`. The task:

1. Dedupes against Redis (``change_gating:posted:`` / ``change_gating:run:``
   keys) so Celery retries and double-deliveries never double-post.
2. Re-verifies enrollment + installation suspension (the user may have
   toggled the repo off while the task sat in the queue).
3. Fetches the PR, prior Aurora review, file list and diff via
   ``services.change_gating.github_adapter.GitHubPRAdapter``.
4. Runs a full agentic investigation through the existing
   ``run_background_chat`` task — SYNCHRONOUSLY via ``.apply()`` so this
   task owns the whole review lifecycle — in read-only ``mode="ask"``.
5. Parses the agent's final message as a verdict JSON and posts a GitHub
   PR review: APPROVE when SAFE, COMMENT with inline findings when RISKY.

Provider-specific calls stay behind the adapter so GitLab/Bitbucket can be
added later without rewriting the task (design doc ``pr-change-gating.md``
section 11). Deliberately NO rate limiting (design section 12).

Logging follows the structured ``key=value`` convention of
``tasks.github_webhook_tasks`` on the canonical key
``change_gating=investigate_pr``. Token values are NEVER logged.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Optional

from celery_config import celery_app

logger = logging.getLogger(__name__)

_POSTED_KEY_TTL_SECONDS = 86400
_RUN_KEY_TTL_SECONDS = 3600
_VERDICT_KEY_TTL_SECONDS = 3600
# Transient "Aurora is reviewing…" conversation comment, deleted in a finally
# block the moment the run leaves the review phase (review posted, skipped, or
# failed). Gives the PR the live signal CodeRabbit shows. The id lives only in
# a local for the duration of one attempt — a Celery retry simply posts a fresh
# one — so there is no cross-attempt state to leak. The marker aids debugging.
_PROGRESS_MARKER = "<!-- aurora-change-gating:progress -->"
_PROGRESS_BODY = (
    f"{_PROGRESS_MARKER}\n"
    "🔍 **Aurora** is reviewing this PR for incident risk. This usually takes "
    "a minute or two — findings will appear as a review when it's done."
)


def change_gating_keys(repo_full_name: str, pr_number: int, head_sha: str) -> dict[str, str]:
    """Build the Redis idempotency keys for one (repo, pr, head) triple.

    Single source of truth shared with ``_maybe_enqueue_change_gating``
    in ``tasks/github_webhook_tasks.py`` so the key shapes can never drift:

    - ``seen``   — webhook-side delivery dedupe (set by the handler)
    - ``run``    — task-side concurrency lock (holder = Celery request id)
    - ``posted`` — review successfully posted for this head
    - ``verdict``— parsed verdict cache so a transient failure AFTER the
      agent run retries the post without re-running the investigation
    """
    suffix = f"{repo_full_name}:{pr_number}:{head_sha}"
    return {
        "seen": f"change_gating:seen:{suffix}",
        "run": f"change_gating:run:{suffix}",
        "posted": f"change_gating:posted:{suffix}",
        "verdict": f"change_gating:verdict:{suffix}",
    }


class _PermanentGitHubError(Exception):
    """Raised for non-retryable (4xx) GitHub API failures.

    Converted by :func:`investigate_pr` into a ``{"status": "github_error"}``
    return so Celery does not burn retries on a permanent failure.
    """



def _classify_github_exc(exc: Exception) -> tuple[str, Optional[int]]:
    """Classify an adapter exception as ``("transient"|"permanent", status_code)``.

    Connection-level errors (requests exceptions subclass OSError) and 5xx
    responses are transient (worth a Celery retry), as are 429s and the
    secondary-rate-limit 403s GitHub sends with a rate-limit message.
    Remaining 4xx responses are permanent. Exceptions with no HTTP response
    and no connection-ish type default to permanent so a coding bug in the
    adapter doesn't loop through the retry budget.
    """
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code is not None:
        if status_code >= 500 or status_code == 429:
            return ("transient", status_code)
        if status_code == 403:
            remaining = (getattr(response, "headers", None) or {}).get(
                "X-RateLimit-Remaining"
            )
            body_text = (getattr(response, "text", None) or "").lower()
            if remaining == "0" or "rate limit" in body_text:
                return ("transient", status_code)
        return ("permanent", status_code)
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return ("transient", None)
    return ("permanent", None)


def _verify_enrollment(user_id: str, installation_id: int, repo_full_name: str) -> str:
    """Re-check suspension + enrollment; returns ``ok | suspended | not_enrolled``.

    ``github_installations`` is NOT RLS-protected; ``connected_repos`` IS
    (FORCE RLS), so the enrollment probe runs under ``set_rls_context``.
    """
    from utils.auth.stateless_auth import set_rls_context
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT suspended_at FROM github_installations WHERE installation_id = %s",
                (installation_id,),
            )
            row = cur.fetchone()
            if row is not None and row[0] is not None:
                return "suspended"

            if not set_rls_context(cur, conn, user_id, log_prefix="[ChangeGating]"):
                # Org resolution failing is a (likely transient) error, NOT
                # proof of non-enrollment — raise so the task retries instead
                # of silently skipping the review.
                raise RuntimeError(
                    f"RLS context unavailable for user {user_id} — cannot "
                    "verify change-gating enrollment"
                )
            cur.execute(
                """SELECT 1
                     FROM connected_repos
                    WHERE repo_full_name = %s
                      AND installation_id = %s
                      AND change_gating_enabled = TRUE
                    LIMIT 1""",
                (repo_full_name, installation_id),
            )
            enrolled = cur.fetchone() is not None
            cur.execute("RESET myapp.current_user_id; RESET myapp.current_org_id;")
    return "ok" if enrolled else "not_enrolled"


def _post_progress_comment(adapter, pr_number: int, log_ctx: str) -> Optional[int]:
    """Post the transient 'Aurora is reviewing…' comment; return its id.

    Best-effort: any failure returns None and the review proceeds without
    a progress indicator. The id is held in a local by the caller and
    cleared in a finally block, so there is no cross-attempt state.
    """
    try:
        comment = adapter.post_issue_comment(pr_number, _PROGRESS_BODY)
        return comment.get("id")
    except Exception as exc:
        logger.warning(
            "change_gating=investigate_pr %s status=progress_post_failed error_class=%s",
            log_ctx, type(exc).__name__,
        )
        return None


def _clear_progress_comment(adapter, comment_id: Optional[int], log_ctx: str) -> None:
    """Delete the progress comment (best-effort). No-op when never posted."""
    if comment_id is None:
        return
    try:
        adapter.delete_issue_comment(comment_id)
    except Exception as exc:
        logger.warning(
            "change_gating=investigate_pr %s status=progress_clear_failed error_class=%s",
            log_ctx, type(exc).__name__,
        )


def _live_fingerprints(comments: Optional[list], aurora_review_ids: set) -> set[str]:
    """Fingerprints of findings Aurora has ALREADY commented on.

    Built from inline comments that (a) belong to one of Aurora's own prior
    reviews — ``pull_request_review_id`` in ``aurora_review_ids``, which
    ``find_aurora_reviews`` already vetted as bot-authored + marker-bearing —
    and (b) carry the ``aurora-finding`` marker. Tying identity to a confirmed
    Aurora review (not bare ``user.type == "Bot"``) stops another bot, or a
    human pasting our marker, from suppressing a real finding. Used only to
    avoid re-posting a finding that already has a live comment — never to
    delete anything.
    """
    from services.change_gating.verdict import extract_inline_fingerprint

    fingerprints: set[str] = set()
    for comment in comments or []:
        if not isinstance(comment, dict):
            continue
        if comment.get("pull_request_review_id") not in aurora_review_ids:
            continue
        fingerprint = extract_inline_fingerprint(comment.get("body"))
        if fingerprint:
            fingerprints.add(fingerprint)
    return fingerprints


def _read_final_assistant_message(user_id: str, session_id: str) -> Optional[str]:
    """Read the final assistant message from ``chat_sessions.messages``.

    Mirrors the read-back pattern in ``chat/background/task.py``
    (``_send_response_to_slack``, ~L2216-2243): RLS context first, then a
    reversed scan for the last bot/assistant message.
    """
    from utils.auth.stateless_auth import set_rls_context
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            if not set_rls_context(cursor, conn, user_id, log_prefix="[ChangeGating:ReadBack]"):
                return None
            cursor.execute(
                "SELECT messages FROM chat_sessions WHERE id = %s",
                (session_id,),
            )
            row = cursor.fetchone()
            cursor.execute("RESET myapp.current_user_id; RESET myapp.current_org_id;")
            if not row or not row[0]:
                return None
            messages = row[0]
            if isinstance(messages, str):
                messages = json.loads(messages)
            if not isinstance(messages, list):
                return None
            for msg in reversed(messages):
                if isinstance(msg, dict) and msg.get("sender") in ("bot", "assistant"):
                    return msg.get("text") or msg.get("content")
    return None


@celery_app.task(
    bind=True,
    name="tasks.change_gating.investigate_pr",
    max_retries=2,
    default_retry_delay=60,
    time_limit=900,
    soft_time_limit=840,
)
def investigate_pr(
    self,
    user_id: str,
    installation_id: int,
    repo_full_name: str,
    pr_number: int,
    head_sha: str,
    action: str,
    delivery_id: str,
) -> dict[str, Any]:
    """Run an agentic risk review on a PR and post a GitHub review."""
    from billiard.exceptions import SoftTimeLimitExceeded
    from celery.exceptions import Retry

    start = time.monotonic()
    try:
        return _run_investigation(
            self, start, user_id, installation_id, repo_full_name,
            pr_number, head_sha, action, delivery_id,
        )
    except _PermanentGitHubError:
        return {"status": "github_error"}
    except Retry:
        raise  # task.retry() raised inside _gh — let Celery handle it
    except SoftTimeLimitExceeded:
        logger.error(
            "change_gating=investigate_pr repo=%s pr=%s head_sha=%s status=timeout",
            repo_full_name, pr_number, head_sha,
        )
        return {"status": "timeout"}
    except Exception as exc:
        # Transient infrastructure failures (DB blips during enrollment
        # checks / session creation, Redis hiccups) deserve the declared
        # retry budget rather than an immediate hard failure.
        logger.exception(
            "change_gating=investigate_pr repo=%s pr=%s head_sha=%s "
            "status=unexpected_error error_class=%s — retrying",
            repo_full_name, pr_number, head_sha, type(exc).__name__,
        )
        raise self.retry(exc=exc)


def _run_investigation(
    task,
    start: float,
    user_id: str,
    installation_id: int,
    repo_full_name: str,
    pr_number: int,
    head_sha: str,
    action: str,
    delivery_id: str,
) -> dict[str, Any]:
    """Full investigation flow; see module docstring for the step list."""
    log_ctx = (
        f"repo={repo_full_name} pr={pr_number} head_sha={head_sha} "
        f"action={action} delivery_id={delivery_id}"
    )

    def _skip(reason: str) -> dict[str, Any]:
        logger.info("change_gating=investigate_pr %s status=%s", log_ctx, reason)
        return {"status": reason}

    def _gh(phase: str, fn: Callable[[], Any]) -> Any:
        """Run a GitHub adapter call with retry/permanent classification."""
        try:
            return fn()
        except Exception as exc:
            kind, status_code = _classify_github_exc(exc)
            if kind == "transient":
                logger.warning(
                    "change_gating=investigate_pr %s phase=%s status=transient_github_error "
                    "code=%s error_class=%s — retrying",
                    log_ctx, phase, status_code, type(exc).__name__,
                )
                raise task.retry(exc=exc)
            if status_code in (403, 422) and phase == "post_review":
                logger.error(
                    "change_gating=investigate_pr %s phase=post_review status=rejected "
                    "code=%s — common causes: the GitHub App lacks the "
                    "'Pull requests: write' permission (upgrade in App settings, "
                    "org admin must accept), or APPROVE was attempted on a PR "
                    "authored by the App itself.",
                    log_ctx, status_code,
                )
            else:
                # logging.exception adds the traceback; the GitHub errors that
                # reach here are requests.HTTPError whose str is "<status> ...
                # for url: <api path>" — token-free (the token is a header, and
                # tracebacks don't dump locals), so this stays log-safe.
                logger.exception(
                    "change_gating=investigate_pr %s phase=%s status=github_error "
                    "code=%s error_class=%s",
                    log_ctx, phase, status_code, type(exc).__name__,
                )
            raise _PermanentGitHubError() from exc

    # ------------------------------------------------------------------
    # 1. Idempotency (Redis). Celery retries reuse the same request id, so
    #    a retry passes the run-lock check; a concurrent duplicate doesn't.
    #    A cached verdict (set after a completed agent run) lets retries
    #    of a late-phase failure re-post WITHOUT re-running the agent.
    # ------------------------------------------------------------------
    from utils.cache.redis_client import get_redis_client

    redis_client = get_redis_client()
    keys = change_gating_keys(repo_full_name, pr_number, head_sha)
    task_request_id = str(getattr(task.request, "id", None))
    cached_verdict: Optional[dict[str, Any]] = None
    if redis_client is not None:
        if redis_client.exists(keys["posted"]):
            return _skip("already_posted")
        if not redis_client.set(keys["run"], task_request_id, nx=True, ex=_RUN_KEY_TTL_SECONDS):
            holder = redis_client.get(keys["run"])
            if holder != task_request_id:
                return _skip("duplicate_run")
        try:
            cached_raw = redis_client.get(keys["verdict"])
            if cached_raw:
                cached_verdict = json.loads(cached_raw)
        except Exception:
            cached_verdict = None
    else:
        logger.warning(
            "change_gating=investigate_pr %s status=redis_unavailable — "
            "proceeding without idempotency keys", log_ctx,
        )

    # ------------------------------------------------------------------
    # 2. Re-verify enrollment + suspension (may have changed while queued).
    # ------------------------------------------------------------------
    enrollment = _verify_enrollment(user_id, installation_id, repo_full_name)
    if enrollment != "ok":
        return _skip(enrollment)

    # ------------------------------------------------------------------
    # 3. Fetch + re-validate the PR.
    # ------------------------------------------------------------------
    from services.change_gating.github_adapter import (
        GitHubPRAdapter,
        decode_marker,
        find_aurora_reviews,
    )

    adapter = GitHubPRAdapter(installation_id, repo_full_name)
    pr = _gh("get_pull_request", lambda: adapter.get_pull_request(pr_number))

    if ((pr.get("head") or {}).get("sha")) != head_sha:
        return _skip("stale_head")
    if pr.get("draft"):
        return _skip("draft")
    if pr.get("state") != "open":
        return _skip("not_open")
    default_branch = ((pr.get("base") or {}).get("repo") or {}).get("default_branch")
    if not default_branch or ((pr.get("base") or {}).get("ref")) != default_branch:
        return _skip("non_default_base")

    # ------------------------------------------------------------------
    # 4. Prior Aurora review (re-review context for synchronize pushes).
    # ------------------------------------------------------------------
    reviews = _gh("list_reviews", lambda: adapter.list_reviews(pr_number))
    prior_aurora_reviews = find_aurora_reviews(reviews)
    prior = prior_aurora_reviews[-1] if prior_aurora_reviews else None
    prior_findings = None
    prior_head_sha = None
    if prior:
        marker = decode_marker(prior.get("body") or "")
        if marker:
            prior_findings = marker.get("findings")
            prior_head_sha = marker.get("head_sha")

    # ------------------------------------------------------------------
    # 5. Diff context. Incremental review (CodeRabbit-style): when a prior
    #    Aurora review exists for an earlier head, review ONLY the commits
    #    pushed since then (the compare diff prior_head...head), so unchanged
    #    code is never re-examined. The first review (no prior) — and the
    #    fallback when the compare is unavailable (force-push / too large) —
    #    reviews the full PR diff. ``get_diff``/``get_compare_diff`` return
    #    None when GitHub refuses the diff media type (406 oversized).
    # ------------------------------------------------------------------
    from services.change_gating.diff_utils import (
        anchor_findings,
        parse_diff_hunks,
    )
    from services.change_gating.verdict import (
        build_review_prompt,
        extract_verdict_with_llm,
        finding_fingerprint,
        parse_verdict,
        render_inline_comment,
        render_review_body,
    )

    incremental = bool(prior_head_sha) and prior_head_sha != head_sha
    compare_files: Optional[list] = None
    if incremental:
        compare = _gh(
            "get_compare", lambda: adapter.get_compare(prior_head_sha, head_sha)
        )
        # Only a clean linear advance ("ahead") is a true incremental delta.
        # "diverged" (force-push/rebase) and "behind" (out-of-order delivery)
        # would make the three-dot compare diff against an old merge-base —
        # re-reviewing already-seen code — so those revert to a full-PR review.
        if compare and compare.get("status") == "ahead":
            compare_files = compare.get("files") or []
            diff = _gh(
                "get_compare_diff",
                lambda: adapter.get_compare_diff(prior_head_sha, head_sha),
            )
            if not (diff and diff.strip()):
                incremental = False  # diff unavailable → full-PR fallback below
        else:
            incremental = False
    if not incremental:
        diff = _gh("get_diff", lambda: adapter.get_diff(pr_number))

    # Transient progress indicator (CodeRabbit-style), shown during the slow
    # first-pass agent run. Skipped in dry-run (read-only calibration) and on
    # the fast cached-verdict retry path. Its id lives only in this local and
    # is cleared in the finally below on EVERY exit (return, skip, retry, or
    # failure) — so it can never leak; a Celery retry just posts a fresh one.
    progress_comment_id = None
    if cached_verdict is None:
        progress_comment_id = _post_progress_comment(adapter, pr_number, log_ctx)

    try:
        # --------------------------------------------------------------
        # 6-8. Agent run + verdict — skipped entirely when a prior attempt
        # of this same head already produced a verdict (cached in Redis): a
        # transient failure AFTER the investigation must not re-spend a full
        # agent run (and risk a different verdict) just to retry the post.
        # --------------------------------------------------------------
        if cached_verdict is not None and cached_verdict.get("verdict"):
            verdict = cached_verdict["verdict"]
            session_id = cached_verdict.get("session_id")
            logger.info(
                "change_gating=investigate_pr %s session_id=%s status=verdict_cache_hit",
                log_ctx, session_id,
            )
        else:
            # Incremental mode reuses the file list already returned by the
            # get_compare call (no second round-trip); full mode lists PR files.
            if incremental:
                files = compare_files or []
            else:
                files = _gh("list_files", lambda: adapter.list_files(pr_number))
            # build_review_prompt renders the diff file-by-file from each
            # file's patch (the raw diff is only a no-patch fallback) and
            # suppresses the prior-findings appendix whenever incremental=True,
            # so prior_findings is passed as-is.
            prompt = build_review_prompt(
                repo_full_name, pr, files, diff,
                prior_findings=prior_findings, incremental=incremental,
            )

            # Session + synchronous agent run. rail_text carries only the
            # externally-authored fields (prompt-injection guardrail surface).
            # Deliberately no is_background_chat_allowed call (design sec. 12).
            from chat.background.task import create_background_chat_session, run_background_chat

            trigger_metadata = {
                "source": "change_gating",
                "repo": repo_full_name,
                "pr_number": pr_number,
                "head_sha": head_sha,
                "delivery_id": delivery_id,
            }
            session_id = create_background_chat_session(
                user_id=user_id,
                title=f"PR Risk Review: {repo_full_name}#{pr_number}",
                trigger_metadata=trigger_metadata,
            )
            rail_text = (pr.get("title") or "") + "\n\n" + (pr.get("body") or "")

            # NOTE: .result, NOT .get() — inside a prefork worker Celery's
            # EagerResult.get() raises "Never call result.get() within a
            # task!"; .result returns the eager return value directly (or the
            # exception instance, which fails the dict check below safely).
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

            if not isinstance(result, dict) or result.get("status") != "completed":
                logger.error(
                    "change_gating=investigate_pr %s session_id=%s status=agent_failed agent_status=%s",
                    log_ctx,
                    session_id,
                    (result or {}).get("status") if isinstance(result, dict) else type(result).__name__,
                )
                return {"status": "agent_failed", "session_id": session_id}
            if result.get("guardrail_blocked"):
                # The input rail blocked the (attacker-controllable) PR
                # title/body. The session's final message is just the block
                # notice — there was NO investigation, so posting any verdict
                # (especially an APPROVE) would be wrong. Post nothing.
                logger.warning(
                    "change_gating=investigate_pr %s session_id=%s status=guardrail_blocked",
                    log_ctx, session_id,
                )
                return {"status": "guardrail_blocked", "session_id": session_id}

            final_text = _read_final_assistant_message(user_id, session_id)
            verdict = None
            if final_text:
                verdict = parse_verdict(final_text)
                if verdict:
                    logger.info(
                        "change_gating=investigate_pr %s verdict_source=parse_verdict",
                        log_ctx,
                    )
                else:
                    verdict = extract_verdict_with_llm(final_text)
                    if verdict:
                        logger.warning(
                            "change_gating=investigate_pr %s verdict_source=llm_extraction_fallback",
                            log_ctx,
                        )
            if not verdict:
                logger.error(
                    "change_gating=investigate_pr %s session_id=%s status=verdict_parse_failed "
                    "has_final_text=%s",
                    log_ctx, session_id, bool(final_text),
                )
                return {"status": "verdict_parse_failed", "session_id": session_id}

            # Normalize: SAFE never carries findings; RISKY without findings is
            # demoted to SAFE (nothing actionable to anchor or list).
            if verdict.get("verdict") == "SAFE":
                verdict["findings"] = []
            elif verdict.get("verdict") == "RISKY" and not verdict.get("findings"):
                logger.info(
                    "change_gating=investigate_pr %s session_id=%s status=demoted_risky_no_findings",
                    log_ctx, session_id,
                )
                verdict["verdict"] = "SAFE"
                verdict["findings"] = []

            if redis_client is not None:
                try:
                    redis_client.set(
                        keys["verdict"],
                        json.dumps({"verdict": verdict, "session_id": session_id}),
                        ex=_VERDICT_KEY_TTL_SECONDS,
                    )
                except Exception as exc:
                    logger.warning(
                        "change_gating=investigate_pr %s status=verdict_cache_set_failed "
                        "error_class=%s", log_ctx, type(exc).__name__,
                    )

        # --------------------------------------------------------------
        # 9. Race check: a newer push owns the review for the new head.
        # --------------------------------------------------------------
        pr_now = _gh("refetch_pull_request", lambda: adapter.get_pull_request(pr_number))
        if ((pr_now.get("head") or {}).get("sha")) != head_sha:
            return _skip("superseded_skip")

        # --------------------------------------------------------------
        # 10. Render the review and reconcile inline comments against what
        #     Aurora already posted (CodeRabbit-style incremental review):
        #     ALL findings go in the body table; for inline comments we POST
        #     only net-new findings and KEEP everything already there. We
        #     never delete — a fixed finding's comment stays as history
        #     (GitHub auto-marks it outdated when its line changes, and the
        #     reviewer can resolve the thread), exactly like CodeRabbit.
        # --------------------------------------------------------------
        if incremental and verdict["findings"]:
            # The compare diff carries context lines from already-reviewed
            # code; the agent sometimes re-flags an issue it sees there. Keep
            # only findings anchored to lines the new commits ADDED/MODIFIED so
            # pre-existing issues aren't re-reported as duplicates. If that
            # empties the set, the delta has no NEW risk → demote to SAFE.
            hunks = parse_diff_hunks(diff, added_only=True)
            new_line_findings = [
                f
                for f in verdict["findings"]
                if isinstance(f.get("line"), int)
                and not isinstance(f.get("line"), bool)
                and f.get("file_path") in hunks
                and f["line"] in hunks[f["file_path"]]
            ]
            if len(new_line_findings) != len(verdict["findings"]):
                logger.info(
                    "change_gating=investigate_pr %s status=incremental_context_findings_dropped "
                    "kept=%d of=%d",
                    log_ctx, len(new_line_findings), len(verdict["findings"]),
                )
                verdict = {**verdict, "findings": new_line_findings}
                if not new_line_findings:
                    verdict["verdict"] = "SAFE"
        else:
            hunks = parse_diff_hunks(diff) if verdict["findings"] else {}
        anchored, unanchored = anchor_findings(verdict["findings"], hunks)

        # Only fetch existing comments when there's something to reconcile:
        # no anchored findings (SAFE/APPROVE) or no prior Aurora reviews
        # (first review) means live_fingerprints is provably empty, so skip
        # the paginated /comments GET entirely.
        aurora_review_ids = {
            r["id"] for r in prior_aurora_reviews if r.get("id") is not None
        }
        live_fingerprints: set = set()
        if anchored and aurora_review_ids:
            existing_comments = _gh(
                "list_review_comments", lambda: adapter.list_review_comments(pr_number)
            )
            live_fingerprints = _live_fingerprints(existing_comments, aurora_review_ids)

        # Net-new inline comments: anchored findings without a live comment.
        new_findings = [
            f for f in anchored if finding_fingerprint(f) not in live_fingerprints
        ]
        comments = [
            {
                "path": f["file_path"],
                "line": f["line"],
                "side": "RIGHT",
                "body": render_inline_comment(f),
            }
            for f in new_findings
        ]
        kept = len(anchored) - len(new_findings)

        body = render_review_body(
            verdict["verdict"], verdict.get("summary", ""), verdict["findings"],
            head_sha, incremental=incremental,
        )
        # Incremental reviews never APPROVE: they assessed only the latest
        # commits, so a clean delta must not post a whole-PR green sign-off
        # while earlier findings may still be open. Only a full-PR review
        # (first pass / fallback) approves on SAFE.
        if incremental:
            event = "COMMENT"
        else:
            event = "APPROVE" if verdict["verdict"] == "SAFE" else "COMMENT"

        # --------------------------------------------------------------
        # 11. Post the new review. Inline comments = net-new findings only;
        #     prior comments are never touched (fixed findings go outdated
        #     on their own). In INCREMENTAL mode each review covers a
        #     distinct slice of commits, so prior reviews are a valid
        #     history and are NOT superseded. Only a full-PR review
        #     (first pass / fallback) supersedes prior stale whole-PR
        #     tables.
        # --------------------------------------------------------------
        _gh(
            "post_review",
            lambda: adapter.post_review(
                pr_number, commit_id=head_sha, event=event, body=body, comments=comments
            ),
        )

        if prior_aurora_reviews and not incremental:
            # Full-PR re-review replaces prior whole-PR verdicts: supersede
            # their stale tables. Per-review isolation — a transient failure
            # on one must not skip the rest (each is best-effort).
            superseded = 0
            for prior_review in prior_aurora_reviews:
                try:
                    adapter.supersede_review(
                        pr_number, prior_review, "Superseded by updated review"
                    )
                    superseded += 1
                except Exception as exc:
                    logger.warning(
                        "change_gating=investigate_pr %s status=supersede_failed "
                        "review_id=%s error_class=%s",
                        log_ctx, prior_review.get("id"), type(exc).__name__,
                    )
            logger.info(
                "change_gating=investigate_pr %s status=superseded superseded=%d prior_reviews=%d",
                log_ctx, superseded, len(prior_aurora_reviews),
            )
        elif incremental and verdict["verdict"] == "RISKY":
            # An incremental review found NEW risk: retract any stale whole-PR
            # APPROVE (from an earlier full review) so the PR does not show a
            # false "Aurora approved" green check while risk is open. COMMENT
            # reviews are valid per-slice history and are left intact.
            dismissed = 0
            for prior_review in prior_aurora_reviews:
                if prior_review.get("state") != "APPROVED":
                    continue
                try:
                    adapter.dismiss_review(
                        pr_number,
                        prior_review.get("id"),
                        "Later changes introduce incident risk — see the latest review.",
                    )
                    dismissed += 1
                except Exception as exc:
                    logger.warning(
                        "change_gating=investigate_pr %s status=dismiss_stale_approve_failed "
                        "review_id=%s error_class=%s",
                        log_ctx, prior_review.get("id"), type(exc).__name__,
                    )
            if dismissed:
                logger.info(
                    "change_gating=investigate_pr %s status=dismissed_stale_approvals dismissed=%d",
                    log_ctx, dismissed,
                )

        if redis_client is not None:
            try:
                redis_client.set(keys["posted"], "1", ex=_POSTED_KEY_TTL_SECONDS)
                redis_client.delete(keys["verdict"])
            except Exception as exc:
                logger.warning(
                    "change_gating=investigate_pr %s status=posted_key_set_failed error_class=%s",
                    log_ctx, type(exc).__name__,
                )

        # --------------------------------------------------------------
        # 12. Completion.
        # --------------------------------------------------------------
        duration_seconds = round(time.monotonic() - start, 2)
        logger.info(
            "change_gating=investigate_pr %s session_id=%s status=completed verdict=%s "
            "incremental=%s findings=%d anchored=%d unanchored=%d inline_posted=%d "
            "inline_kept=%d duration_seconds=%.2f",
            log_ctx,
            session_id,
            verdict["verdict"],
            incremental,
            len(verdict["findings"]),
            len(anchored),
            len(unanchored),
            len(comments),
            kept,
            duration_seconds,
        )
        return {
            "status": "completed",
            "verdict": verdict["verdict"],
            "incremental": incremental,
            "findings": len(verdict["findings"]),
            "session_id": session_id,
        }
    finally:
        # Always remove this attempt's progress comment, on any exit path —
        # return, skip, _PermanentGitHubError, or a retry raised by _gh.
        _clear_progress_comment(adapter, progress_comment_id, log_ctx)
        adapter.close()
