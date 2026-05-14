#!/usr/bin/env python3
"""Calibration backfill for the Phase 1a "PR Risk Review" pipeline.

Given an Aurora GitHub App installation_id, this script walks the
installation's connected repositories, lists recent merged pull
requests, and synthesises ``change_events`` rows + enqueues
``launch_investigation`` Celery tasks against them in **dry-run
mode**. Operators use this during the calibration window to populate
``change_investigations`` so they can inspect Aurora's severity
distribution before flipping ``change_intercept_dry_run=FALSE``.

Crucially: every row this script writes carries the same
``vendor='github'`` / ``kind='code_change'`` shape as a real webhook.
The only difference is that ``payload`` is a synthetic stub
(``{"source": "backfill", "pr_number": N}``) so the calibration data
is distinguishable from production traffic in DB queries.

Usage::

    # Backfill the most recent 100 merged PRs per connected repo
    python server/scripts/change_intercept_backfill.py \\
        --installation-id 12345 \\
        --user-id <any user from the target org>

    # Limit to a specific repo + smaller sample
    python server/scripts/change_intercept_backfill.py \\
        --installation-id 12345 \\
        --user-id <user-id> \\
        --repo acme/widgets \\
        --limit 20

    # Re-run the same PRs (will dedup at the change_events UNIQUE)
    python server/scripts/change_intercept_backfill.py \\
        --installation-id 12345 \\
        --user-id <user-id> \\
        --force

Output is a one-line-per-PR audit log + a final summary. The actual
investigation results land in ``change_investigations`` (dry-run)
after the Celery workers chew through the enqueued tasks. Inspect
them via::

    SELECT verdict, COUNT(*) FROM change_investigations
     WHERE dry_run = TRUE GROUP BY verdict;

    SELECT severity, confidence, COUNT(*)
      FROM change_investigations,
           jsonb_to_recordset(findings)
             AS f(severity text, confidence text)
     WHERE dry_run = TRUE
     GROUP BY 1, 2 ORDER BY 1, 2;
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

import requests

# Aurora expects to import from ``server/`` as the package root. Allow the
# script to run from any working directory.
import pathlib

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("change_intercept_backfill")


_API_BASE = "https://api.github.com"
_TIMEOUT = 30


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--installation-id",
        type=int,
        required=True,
        help="GitHub App installation id to backfill against.",
    )
    p.add_argument(
        "--user-id",
        required=True,
        help="Aurora user id from the target org (used to set RLS context).",
    )
    p.add_argument(
        "--repo",
        default=None,
        help="Restrict to a single ``owner/repo``. Default: all connected repos.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Max PRs to backfill per repo (default 100).",
    )
    p.add_argument(
        "--state",
        default="closed",
        choices=("open", "closed", "all"),
        help="PR state filter (default 'closed' — focus on shipped changes).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Re-enqueue even when a change_events row already exists for the PR.",
    )
    p.add_argument(
        "--dry-list",
        action="store_true",
        help="Just list the PRs that would be backfilled; do not write or enqueue.",
    )
    return p.parse_args()


def _list_installation_repos(token: str) -> list[dict[str, Any]]:
    """List every repo accessible to the installation token."""
    url = f"{_API_BASE}/installation/repositories"
    repos: list[dict[str, Any]] = []
    params: dict[str, Any] = {"per_page": 100}
    while url:
        response = requests.get(
            url,
            headers=_auth_headers(token),
            params=params,
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        body = response.json()
        repos.extend(body.get("repositories") or [])
        url = _next_link(response.headers.get("Link", ""))
        params = {}
    return repos


def _list_repo_prs(
    token: str, repo_full_name: str, *, state: str, limit: int
) -> list[dict[str, Any]]:
    """List recent PRs in ``repo_full_name``, newest first, up to ``limit``."""
    out: list[dict[str, Any]] = []
    url = f"{_API_BASE}/repos/{repo_full_name}/pulls"
    params: dict[str, Any] = {
        "state": state,
        "sort": "updated",
        "direction": "desc",
        "per_page": 100,
    }
    while url and len(out) < limit:
        response = requests.get(
            url,
            headers=_auth_headers(token),
            params=params,
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        body = response.json()
        out.extend(body)
        if len(out) >= limit:
            break
        url = _next_link(response.headers.get("Link", ""))
        params = {}
    return out[:limit]


def _auth_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _next_link(link_header: str) -> str | None:
    import re

    if not link_header:
        return None
    for chunk in link_header.split(","):
        chunk = chunk.strip()
        m = re.match(r'<([^>]+)>;\s*rel="next"', chunk)
        if m:
            return m.group(1)
    return None


def _build_synthetic_event(
    installation_id: int,
    repo_full_name: str,
    pr: dict[str, Any],
) -> dict[str, Any]:
    """Build a NormalizedChangeEvent-shaped dict for the synthetic backfill row."""
    head = pr.get("head") or {}
    base = pr.get("base") or {}
    user = pr.get("user") or {}
    pr_number = pr.get("number")
    return {
        "installation_id": installation_id,
        "pr_number": pr_number,
        "repo": repo_full_name,
        "ref": head.get("ref"),
        "base_ref": base.get("ref"),
        "commit_sha": head.get("sha"),
        "actor": user.get("login"),
        "title": pr.get("title"),
        "merged_at": pr.get("merged_at"),
    }


def _persist_and_enqueue(
    *,
    installation_id: int,
    repo_full_name: str,
    pr: dict[str, Any],
    user_id: str,
    force: bool,
) -> str:
    """Write a synthetic change_events row + enqueue launch_investigation.

    Returns one of ``persisted_and_enqueued`` / ``deduped`` /
    ``snapshot_failed`` / ``persist_failed``. Idempotency is the same
    as the production webhook path — the change_events UNIQUE
    constraint dedups by ``(org_id, vendor, external_id, commit_sha,
    kind)`` so re-runs are safe.
    """
    # Lazy import: keeps the script importable for ``--help`` without
    # bootstrapping the Celery / DB / Vault stack.
    from services.change_intercept.adapters.base import (
        NormalizedChangeEvent,
    )
    from services.change_intercept.adapters.github import GitHubChangeAdapter
    from services.change_intercept.tasks import launch_investigation
    from tasks.github_webhook_tasks import _persist_change_event
    from utils.auth.stateless_auth import get_org_id_for_user

    org_id = get_org_id_for_user(user_id)
    if not org_id:
        return f"no_org_for_user:{user_id}"

    pr_number = pr.get("number")
    if not isinstance(pr_number, int):
        return "invalid_pr"

    adapter = GitHubChangeAdapter()

    event = NormalizedChangeEvent(
        vendor="github",
        kind="code_change",
        org_id=org_id,
        installation_id=installation_id,
        external_id=str(pr_number),
        dedup_key=f"github:{repo_full_name}:{pr_number}",
        repo=repo_full_name,
        ref=(pr.get("head") or {}).get("ref"),
        base_ref=(pr.get("base") or {}).get("ref"),
        commit_sha=(pr.get("head") or {}).get("sha"),
        actor=(pr.get("user") or {}).get("login"),
        target_env=None,
        action="opened",
        raw_payload={
            "source": "backfill",
            "pr_number": pr_number,
            "title": pr.get("title"),
            "merged_at": pr.get("merged_at"),
        },
    )

    try:
        snapshot = adapter.fetch_snapshot(event)
    except Exception as exc:
        logger.warning(
            "backfill repo=%s pr=%s status=snapshot_failed error=%s",
            repo_full_name,
            pr_number,
            type(exc).__name__,
        )
        return "snapshot_failed"

    if force:
        _force_delete_existing(event)

    new_event_id = _persist_change_event(
        event,
        snapshot,
        user_id=user_id,
        delivery_id=f"backfill-{repo_full_name}-{pr_number}",
    )
    if new_event_id is None:
        return "deduped"

    launch_investigation.delay(
        change_event_id=new_event_id, user_id_for_rls=user_id
    )
    return "persisted_and_enqueued"


def _force_delete_existing(event: Any) -> None:
    """When ``--force``, drop any prior change_events row + investigations
    so the next INSERT lands a fresh dry-run entry.

    Used during calibration when an operator wants to re-run with a
    revised prompt against the same PR set."""
    from utils.auth.stateless_auth import set_rls_context
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SET myapp.current_org_id = %s;", (event.org_id,))
            cur.execute(
                "SET myapp.current_user_id = %s;",
                (f"__backfill__{event.org_id}",),
            )
            cur.execute(
                """DELETE FROM change_events
                    WHERE org_id = %s AND vendor = %s
                      AND external_id = %s AND commit_sha = %s AND kind = %s""",
                (
                    event.org_id,
                    event.vendor,
                    event.external_id,
                    event.commit_sha,
                    event.kind,
                ),
            )
            cur.execute("RESET myapp.current_org_id; RESET myapp.current_user_id;")
            conn.commit()


def main() -> int:
    args = _parse_args()

    from utils.auth.github_app_token import get_installation_token

    try:
        token = get_installation_token(args.installation_id)
    except Exception as exc:
        logger.error(
            "Failed to mint installation token for installation_id=%s: %s",
            args.installation_id,
            type(exc).__name__,
        )
        return 1

    if args.repo:
        repos = [{"full_name": args.repo}]
    else:
        repos = _list_installation_repos(token)
    logger.info("backfill discovered repos=%d", len(repos))

    counts: dict[str, int] = {}
    total_prs = 0
    for repo in repos:
        repo_full_name = repo.get("full_name")
        if not repo_full_name:
            continue
        try:
            prs = _list_repo_prs(
                token, repo_full_name, state=args.state, limit=args.limit
            )
        except Exception as exc:
            logger.warning(
                "backfill repo=%s status=list_failed error=%s",
                repo_full_name,
                type(exc).__name__,
            )
            counts["list_failed"] = counts.get("list_failed", 0) + 1
            continue
        logger.info("backfill repo=%s found_prs=%d", repo_full_name, len(prs))

        for pr in prs:
            total_prs += 1
            if args.dry_list:
                logger.info(
                    "DRY-LIST repo=%s pr=%s title=%s",
                    repo_full_name,
                    pr.get("number"),
                    (pr.get("title") or "")[:80],
                )
                counts["dry_listed"] = counts.get("dry_listed", 0) + 1
                continue
            outcome = _persist_and_enqueue(
                installation_id=args.installation_id,
                repo_full_name=repo_full_name,
                pr=pr,
                user_id=args.user_id,
                force=args.force,
            )
            counts[outcome] = counts.get(outcome, 0) + 1
            logger.info(
                "backfill repo=%s pr=%s status=%s",
                repo_full_name,
                pr.get("number"),
                outcome,
            )

    logger.info(
        "backfill done total_prs=%d outcomes=%s",
        total_prs,
        json.dumps(counts),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
