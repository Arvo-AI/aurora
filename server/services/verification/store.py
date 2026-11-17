"""Reads and writes of ``verification_runs``.

The table is RLS-protected and the only caller is a Celery task, which has no
Flask request to set the RLS variables for it. Every function therefore sets
them itself from ``user_id`` and resets them before the pooled connection goes
back (same pattern as ``tasks/change_gating._verify_enrollment``).

No raw SQL against this table belongs anywhere else.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from typing import Any, Dict, Generator, List, Optional

logger = logging.getLogger(__name__)

# Clears per-connection RLS variables before a pooled connection is reused.
_RESET_RLS_SQL = "RESET myapp.current_user_id; RESET myapp.current_org_id;"


@contextmanager
def _org_cursor(user_id: str) -> Generator[tuple, None, None]:
    """Yield ``(cursor, conn, org_id)`` with RLS scoped to the user's org.

    Raises when the org cannot be resolved. That is treated as an error rather
    than "no rows": under FORCE ROW LEVEL SECURITY a query with no org set
    silently matches nothing, which would look like a successful empty result.
    """
    from utils.auth.stateless_auth import set_rls_context
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            org_id = set_rls_context(cursor, conn, user_id, log_prefix="[Verification]")
            if not org_id:
                raise RuntimeError("RLS context unavailable; cannot access verification_runs")
            try:
                yield cursor, conn, org_id
            finally:
                try:
                    cursor.execute(_RESET_RLS_SQL)
                    conn.commit()
                except Exception:  # noqa: BLE001 - never mask the original error
                    conn.rollback()


def record_run(
    user_id: str,
    *,
    repo_full_name: str,
    pr_number: Optional[int],
    head_sha: str,
    run_id: int,
    run_attempt: int,
    run_url: Optional[str],
    status: str,
    conclusion: Optional[str],
    report: Dict[str, Any],
    uncovered_paths: List[str],
    modifies_verification: bool,
) -> Dict[str, Any]:
    """Insert or refresh the row for one workflow run attempt.

    Idempotent on ``(org, repo, run_id, run_attempt)``: GitHub redelivers
    webhooks and Celery retries tasks, and neither may create a second row.
    Returns ``{"id", "comment_id"}``; a non-null ``comment_id`` means an earlier
    delivery already commented for this run.
    """
    with _org_cursor(user_id) as (cursor, conn, org_id):
        cursor.execute(
            """INSERT INTO verification_runs
                 (org_id, user_id, repo_full_name, pr_number, head_sha, run_id,
                  run_attempt, run_url, status, conclusion, report,
                  uncovered_paths, modifies_verification)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
               ON CONFLICT (org_id, repo_full_name, run_id, run_attempt) DO UPDATE
                  SET pr_number = EXCLUDED.pr_number,
                      head_sha = EXCLUDED.head_sha,
                      run_url = EXCLUDED.run_url,
                      status = EXCLUDED.status,
                      conclusion = EXCLUDED.conclusion,
                      report = EXCLUDED.report,
                      uncovered_paths = EXCLUDED.uncovered_paths,
                      modifies_verification = EXCLUDED.modifies_verification,
                      updated_at = NOW()
               RETURNING id, comment_id""",
            (
                org_id, user_id, repo_full_name, pr_number, head_sha, run_id,
                run_attempt, run_url, status, conclusion, json.dumps(report),
                json.dumps(uncovered_paths), modifies_verification,
            ),
        )
        row = cursor.fetchone()
        conn.commit()
    return {"id": str(row[0]), "comment_id": row[1]}


def prior_comment_ids(user_id: str, repo_full_name: str, pr_number: int, exclude_id: str) -> List[int]:
    """Comment ids Aurora posted for earlier runs on this pull request."""
    with _org_cursor(user_id) as (cursor, _conn, _org_id):
        cursor.execute(
            """SELECT comment_id
                 FROM verification_runs
                WHERE repo_full_name = %s
                  AND pr_number = %s
                  AND comment_id IS NOT NULL
                  AND id <> %s::uuid""",
            (repo_full_name, pr_number, exclude_id),
        )
        return [row[0] for row in cursor.fetchall() if row[0] is not None]


def set_comment_id(user_id: str, row_id: str, comment_id: int) -> None:
    """Record the comment posted for a run."""
    with _org_cursor(user_id) as (cursor, conn, _org_id):
        cursor.execute(
            "UPDATE verification_runs SET comment_id = %s, updated_at = NOW() WHERE id = %s::uuid",
            (comment_id, row_id),
        )
        conn.commit()


def clear_comment_ids(user_id: str, comment_ids: List[int]) -> None:
    """Forget comments that have been deleted from the pull request."""
    if not comment_ids:
        return
    with _org_cursor(user_id) as (cursor, conn, _org_id):
        cursor.execute(
            "UPDATE verification_runs SET comment_id = NULL, updated_at = NOW() "
            "WHERE comment_id = ANY(%s)",
            (list(comment_ids),),
        )
        conn.commit()
