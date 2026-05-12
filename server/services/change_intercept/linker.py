"""Nightly linker populating ``risk_outcomes``.

Closes the feedback loop on the Phase 1a PR Risk Review: for each
recent incident, scan its text fields for GitHub PR URL references,
match them to ``change_investigations`` rows, and INSERT
``risk_outcomes`` linking the two.

The matcher is intentionally simple in Phase 1a — a regex over the
incident's ``alert_title`` / ``alert_service`` / ``alert_environment``
/ ``aurora_summary`` plus any attached postmortem ``content``. False
negatives are expected (most postmortems won't carry a PR URL); the
goal is to start a precision-first feedback loop the calibration
phase reads from.

Future iterations can layer on:
    - Service → repo mapping via the discovery graph.
    - Time-window correlation between PR merge time and incident start.
    - LLM-based postmortem classification for causal-PR identification.

The linker is idempotent: ``risk_outcomes`` has the
``change_investigation_id`` as its PK, so re-runs ``ON CONFLICT DO
NOTHING``. Each successful match writes a row with
``feedback_source='nightly_linker'`` so future feedback channels
(manual labelling UI, customer-supplied truth files) can be
distinguished.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# Matches ``[anything/]github.com/<owner>/<repo>/pull/<n>`` with the
# usernames + repo names GitHub actually permits: alphanumeric +
# ``-`` ``_`` ``.``. The leading ``https?://[^/]*`` segment is
# optional so we also catch bare ``github.com/foo/bar/pull/42`` refs
# in older postmortems. Trailing ``/files``, ``#issuecomment-...``,
# ``?...`` are tolerated by anchoring on ``(\d+)``.
_PR_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/"
    r"(?P<owner>[A-Za-z0-9._-]+)/(?P<repo>[A-Za-z0-9._-]+)/pull/(?P<num>\d+)",
    re.IGNORECASE,
)


# How far back to look. The scheduled run fires daily so a 36h window
# leaves a 12h safety margin for incidents that linger in
# ``investigating`` status before being summarised.
_DEFAULT_LOOKBACK_HOURS: int = 36


def run_linker(*, lookback_hours: int = _DEFAULT_LOOKBACK_HOURS) -> dict[str, Any]:
    """Scan recent incidents for PR URL references and link them to
    ``change_investigations`` via ``risk_outcomes``.

    Pure function (no Celery decorator) so the linker logic is unit-
    testable in isolation and so this module stays import-cheap for
    the test runner. The Celery task wrapper lives in
    :mod:`services.change_intercept.tasks` and just calls this with
    its default args.

    Args:
        lookback_hours: how far back to look. Defaults to 36h so the
            nightly run picks up incidents that resolved between the
            previous run and now without double-counting older ones.

    Returns:
        Status dict: ``{scanned, candidates_extracted, linked,
        already_linked, no_match}``. Used by ops dashboards to track
        coverage.
    """
    scanned, extracted, linked, already_linked, no_match = 0, 0, 0, 0, 0

    candidates = _scan_recent_incidents(lookback_hours)
    scanned = len(candidates)
    for incident in candidates:
        references = _extract_pr_references(incident)
        extracted += len(references)
        for ref in references:
            outcome = _link_one_reference(
                org_id=incident["org_id"],
                incident_id=incident["id"],
                dedup_key=ref["dedup_key"],
            )
            if outcome == "linked":
                linked += 1
            elif outcome == "already_linked":
                already_linked += 1
            else:
                no_match += 1

    logger.info(
        "change_intercept_linker=run_done scanned=%d extracted=%d "
        "linked=%d already_linked=%d no_match=%d",
        scanned,
        extracted,
        linked,
        already_linked,
        no_match,
    )
    return {
        "scanned": scanned,
        "candidates_extracted": extracted,
        "linked": linked,
        "already_linked": already_linked,
        "no_match": no_match,
    }


# ─── Pure-function helpers ──────────────────────────────────────────


def extract_pr_references_from_text(text: str) -> list[dict[str, str]]:
    """Return the unique PR refs found in ``text``.

    Each ref is a dict with ``owner``, ``repo``, ``num``, ``dedup_key``.
    Order is insertion (first occurrence) so callers see references
    in the order they appear in the source.
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for match in _PR_URL_RE.finditer(text):
        owner = match.group("owner")
        repo = match.group("repo")
        num = match.group("num")
        dedup_key = f"github:{owner}/{repo}:{num}"
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        out.append(
            {"owner": owner, "repo": repo, "num": num, "dedup_key": dedup_key}
        )
    return out


def _extract_pr_references(incident: dict[str, Any]) -> list[dict[str, str]]:
    """Concatenate every text-bearing field of the incident and scan."""
    parts: list[str] = []
    for key in ("alert_title", "alert_service", "alert_environment", "aurora_summary"):
        value = incident.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
    postmortem_content = incident.get("postmortem_content")
    if isinstance(postmortem_content, str) and postmortem_content:
        parts.append(postmortem_content)
    return extract_pr_references_from_text("\n".join(parts))


# ─── DB I/O ─────────────────────────────────────────────────────────


def _scan_recent_incidents(lookback_hours: int) -> list[dict[str, Any]]:
    """Return incident rows joined with their postmortem ``content``.

    Runs cross-org (no RLS set) using the admin connection — the
    nightly linker is a system-level job and needs to see incidents
    across every customer. Each row also carries ``org_id`` so the
    per-org INSERT below can apply RLS correctly.
    """
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT i.id, i.org_id, i.alert_title, i.alert_service,
                          i.alert_environment, i.aurora_summary,
                          p.content
                     FROM incidents i
                     LEFT JOIN postmortems p ON p.incident_id = i.id
                    WHERE i.started_at >= NOW() - (%s::int * INTERVAL '1 hour')
                      AND i.org_id IS NOT NULL""",
                (lookback_hours,),
            )
            rows = cur.fetchall()
    return [
        {
            "id": row[0],
            "org_id": row[1],
            "alert_title": row[2],
            "alert_service": row[3],
            "alert_environment": row[4],
            "aurora_summary": row[5],
            "postmortem_content": row[6],
        }
        for row in rows
    ]


def _link_one_reference(
    *,
    org_id: str,
    incident_id: str,
    dedup_key: str,
) -> str:
    """INSERT a single ``risk_outcomes`` row for one
    ``(org_id, dedup_key)`` match. Returns ``linked`` / ``already_linked``
    / ``no_match`` so the caller can tally the run."""
    from utils.auth.stateless_auth import get_org_id_for_user  # noqa: F401 — reserved
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            # The linker runs cross-org so it sets RLS vars directly
            # rather than going through a user lookup — the org comes
            # from the incident row, and there's no requesting user.
            cur.execute("SET myapp.current_org_id = %s;", (org_id,))
            cur.execute("SET myapp.current_user_id = %s;", (f"__linker__{org_id}",))

            # Find the most recent investigation for this PR.
            cur.execute(
                """SELECT ci.id FROM change_investigations ci
                     JOIN change_events ce ON ce.id = ci.change_event_id
                    WHERE ce.org_id = %s AND ce.dedup_key = %s
                 ORDER BY ci.investigated_at DESC
                    LIMIT 1""",
                (org_id, dedup_key),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute("RESET myapp.current_org_id; RESET myapp.current_user_id;")
                return "no_match"

            change_investigation_id = row[0]
            cur.execute(
                """INSERT INTO risk_outcomes (
                       change_investigation_id, org_id, caused_incident_id,
                       feedback_source
                   ) VALUES (%s, %s, %s, 'nightly_linker')
                   ON CONFLICT (change_investigation_id) DO NOTHING""",
                (change_investigation_id, org_id, incident_id),
            )
            rows_inserted = cur.rowcount
            cur.execute("RESET myapp.current_org_id; RESET myapp.current_user_id;")
            conn.commit()
    return "linked" if rows_inserted == 1 else "already_linked"
