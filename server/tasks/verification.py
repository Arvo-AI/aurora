"""Celery task for PR verification: pick up the result of running a pull request.

Change gating (``tasks/change_gating.py``) reads a pull request and predicts
what it could break. This is the other half: the repository's CI boots the pull
request in a throwaway environment and publishes what happened, and Aurora
collects it.

When ``_handle_workflow_run_event`` (``tasks/github_webhook_tasks.py``) sees a
completed ``pull_request`` workflow run on an enrolled repo, it enqueues
:func:`ingest_verification_run`, which:

1. Re-verifies enrollment (it may have changed while the task was queued).
2. Looks for an artifact named ``aurora-verification`` on the run. Most
   workflow runs have none, and the task ends there after one API call. The
   artifact is the whole contract: no workflow names are configured anywhere.
3. Downloads it (size-capped), reads ``verification.json`` and sanitises it
   (``services/verification/report.py``). The file is written by the pull
   request's own code, so it is treated as untrusted input throughout.
4. Works out which changed files the run did not exercise and whether the pull
   request edits the verification itself.
5. Stores one row in ``verification_runs``.
6. Replaces Aurora's verification comment on the pull request, unless a newer
   commit has been pushed since.

No LLM is involved at any step. A passing run needs no model to explain it,
and keeping this path deterministic is what makes the stored result usable as
evidence later (the review agent, MCP, and failure analysis all read the row).

Pull, not push: CI sends Aurora nothing. Aurora fetches the artifact with its
GitHub App installation token, so the workflow needs no Aurora credentials and
stays safe to run on pull requests from forks.

Logging follows the ``key=value`` convention of ``tasks.change_gating`` on the
canonical key ``verification=ingest_run``. Token values are never logged.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Optional

from celery_config import celery_app

logger = logging.getLogger(__name__)

_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")


class _PermanentProviderError(Exception):
    """A provider failure that retrying will not fix (most 4xx responses)."""


@celery_app.task(
    bind=True,
    name="tasks.verification.ingest_verification_run",
    max_retries=3,
    default_retry_delay=30,
    time_limit=180,
    soft_time_limit=150,
)
def ingest_verification_run(
    self,
    user_id: str,
    installation_id: int,
    repo_full_name: str,
    run_id: int,
    run_attempt: int,
    head_sha: str,
    conclusion: Optional[str],
    run_url: Optional[str],
    pr_numbers: list,
    delivery_id: str,
) -> dict[str, Any]:
    """Collect one finished workflow run's verification report, if it has one."""
    from celery.exceptions import Retry

    from services.verification.github_actions import GitHubActionsClient
    from tasks.change_gating import _verify_enrollment

    client = GitHubActionsClient(installation_id, repo_full_name)
    try:
        return _ingest_core(
            self, user_id, repo_full_name, run_id, run_attempt, head_sha,
            conclusion, run_url, pr_numbers, delivery_id,
            client=client,
            verify_enrollment=lambda: _verify_enrollment(
                user_id, installation_id, repo_full_name
            ),
        )
    except _PermanentProviderError:
        return {"status": "github_error"}
    except Retry:
        raise
    except Exception as exc:
        # Database blips and the like deserve the retry budget. The row is an
        # upsert and the comment step checks for an existing comment, so a
        # retry cannot duplicate either.
        logger.exception(
            "verification=ingest_run repo=%s run_id=%s status=unexpected_error "
            "error_class=%s - retrying",
            repo_full_name, run_id, type(exc).__name__,
        )
        raise self.retry(exc=exc)
    finally:
        client.close()


def _ingest_core(
    task,
    user_id: str,
    repo_full_name: str,
    run_id: int,
    run_attempt: int,
    head_sha: str,
    conclusion: Optional[str],
    run_url: Optional[str],
    pr_numbers: list,
    delivery_id: str,
    *,
    client,
    verify_enrollment: Callable[[], str],
) -> dict[str, Any]:
    """The ingest flow, with every collaborator passed in so it can be tested."""
    from services.verification import report as reports
    from services.verification import store
    from tasks.change_gating import _classify_provider_exc

    log_ctx = (
        f"repo={repo_full_name} run_id={run_id} run_attempt={run_attempt} "
        f"head_sha={head_sha} delivery_id={delivery_id}"
    )

    def _done(status: str, **extra: Any) -> dict[str, Any]:
        logger.info("verification=ingest_run %s status=%s", log_ctx, status)
        return {"status": status, **extra}

    def _gh(phase: str, fn: Callable[[], Any]) -> Any:
        """Run a GitHub call; retry transient failures, stop on permanent ones."""
        try:
            return fn()
        except Exception as exc:
            kind, status_code = _classify_provider_exc(exc)
            if kind == "transient":
                logger.warning(
                    "verification=ingest_run %s phase=%s status=transient_github_error "
                    "code=%s error_class=%s - retrying",
                    log_ctx, phase, status_code, type(exc).__name__,
                )
                raise task.retry(exc=exc)
            logger.error(
                "verification=ingest_run %s phase=%s status=github_error code=%s "
                "error_class=%s",
                log_ctx, phase, status_code, type(exc).__name__,
            )
            raise _PermanentProviderError() from exc

    if not _SHA_RE.match(head_sha or ""):
        return _done("invalid_head_sha")

    # 1. Enrollment may have been switched off while this sat in the queue.
    enrollment = verify_enrollment()
    if enrollment != "ok":
        return _done(enrollment)

    # 2. The artifact is the contract. No artifact: not a verification run.
    artifacts = _gh("list_artifacts", lambda: client.list_run_artifacts(run_id))
    artifact = next(
        (
            a for a in artifacts
            if a.get("name") == reports.ARTIFACT_NAME and not a.get("expired")
        ),
        None,
    )
    if artifact is None or artifact.get("id") is None:
        return _done("no_report")
    declared_size = artifact.get("size_in_bytes")
    if isinstance(declared_size, int) and declared_size > reports.MAX_ARTIFACT_BYTES:
        return _done("report_too_large")

    # 3. Download, unpack and sanitise. Every step can reject the artifact.
    archive = _gh(
        "download_artifact",
        lambda: client.download_artifact(artifact["id"], reports.MAX_ARTIFACT_BYTES),
    )
    if archive is None:
        return _done("report_too_large")
    report = reports.parse_report(reports.extract_report_bytes(archive) or b"")
    if report is None:
        return _done("invalid_report")

    # 4. Which pull request is this? Same-repo PRs arrive on the event; fork
    #    PRs do not, so fall back to asking GitHub which PRs contain the commit.
    pr_number = _first_int(pr_numbers)
    if pr_number is None:
        pulls = _gh("list_pulls_for_commit", lambda: client.list_pulls_for_commit(head_sha))
        pr_number = _first_int(
            [p.get("number") for p in pulls if isinstance(p, dict) and p.get("state") == "open"]
        )

    changed_paths: list[str] = []
    if pr_number is not None:
        files = _gh("list_files", lambda: client.list_files(pr_number))
        changed_paths = [
            f["filename"] for f in files
            if isinstance(f, dict) and isinstance(f.get("filename"), str)
        ]

    uncovered = reports.uncovered_paths(changed_paths, report["covers"])
    self_changes = reports.verification_changes(changed_paths, report["defined_by"])
    status = reports.final_status(report, conclusion)

    # 5. Store it. This is the durable result; the comment is a view of it.
    row = store.record_run(
        user_id,
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        head_sha=head_sha,
        run_id=run_id,
        run_attempt=run_attempt,
        run_url=run_url,
        status=status,
        conclusion=conclusion,
        report=report,
        uncovered_paths=uncovered,
        modifies_verification=bool(self_changes),
    )

    # 6. Comment, unless there is no PR, we already did, or the PR moved on.
    if pr_number is None:
        return _done("recorded_no_pr", verification=status)
    if row.get("comment_id"):
        return _done("already_commented", verification=status)

    pr = _gh("get_pull_request", lambda: client.get_pull_request(pr_number))
    if pr.get("state") != "open":
        return _done("recorded_pr_not_open", verification=status)
    if ((pr.get("head") or {}).get("sha")) != head_sha:
        # A newer commit was pushed while this run was in flight. Its own run
        # will report; commenting now would describe code that is already gone.
        return _done("recorded_stale_head", verification=status)

    body = reports.render_comment(
        report,
        status=status,
        head_sha=head_sha,
        run_url=run_url,
        conclusion=conclusion,
        changed_file_count=len(changed_paths),
        uncovered=uncovered,
        changes_to_verification=self_changes,
    )
    comment = _gh("post_comment", lambda: client.post_issue_comment(pr_number, body))
    comment_id = comment.get("id") if isinstance(comment, dict) else None
    if comment_id is not None:
        store.set_comment_id(user_id, row["id"], comment_id)

    # One verification comment per pull request: remove the ones for earlier
    # runs. Best effort, and only after the new one is up, so a failure here
    # leaves an extra comment rather than none.
    _remove_prior_comments(client, user_id, repo_full_name, pr_number, row["id"], log_ctx)

    logger.info(
        "verification=ingest_run %s status=completed verification=%s pr=%s "
        "checks=%d uncovered=%d modifies_verification=%s",
        log_ctx, status, pr_number, len(report["checks"]), len(uncovered), bool(self_changes),
    )
    return {"status": "completed", "verification": status, "pr_number": pr_number}


def _first_int(values: Any) -> Optional[int]:
    """First real integer in a list (booleans are not PR numbers)."""
    for value in values or []:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _remove_prior_comments(
    client, user_id: str, repo_full_name: str, pr_number: int, current_row_id: str, log_ctx: str
) -> None:
    """Delete Aurora's verification comments for earlier runs on this PR."""
    from services.verification import store

    try:
        prior_ids = store.prior_comment_ids(user_id, repo_full_name, pr_number, current_row_id)
        removed = []
        for comment_id in prior_ids:
            try:
                client.delete_issue_comment(comment_id)
                removed.append(comment_id)
            except Exception as exc:
                logger.warning(
                    "verification=ingest_run %s status=prior_comment_delete_failed "
                    "error_class=%s",
                    log_ctx, type(exc).__name__,
                )
        store.clear_comment_ids(user_id, removed)
    except Exception as exc:
        logger.warning(
            "verification=ingest_run %s status=prior_comment_cleanup_failed error_class=%s",
            log_ctx, type(exc).__name__,
        )
