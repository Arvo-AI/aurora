"""Keep the check store from only ever growing.

A monitoring repo accumulates rules because nobody is sure one is safe to
delete. Here the record says which ones are, and this pass removes them with no
model and no person: duplicates, checks that have only ever been wrong, and the
weakest checks on an asset that has too many.

Retiring keeps the row, its revisions and its fires, so a pattern's record is
not lost with the check. Proven checks are never retired here.
"""

from typing import Dict

from services.detection import STATE_RETIRED, STATE_SILENT
from services.detection.record import MAX_ACTIVE_CHECKS_PER_ASSET, RETIRE_AFTER_DAYS

_RETIRE = """UPDATE detection_checks
             SET state = %s, retired_reason = %s, updated_at = NOW()
             WHERE id = ANY(%s::uuid[])"""

# Best record first: hits, then fewest false fires, then the oldest check.
_STRONGEST_FIRST = "hit_count DESC, false_fire_count ASC, created_at ASC"


def _retire(cursor, check_ids, reason: str) -> int:
    if not check_ids:
        return 0
    cursor.execute(_RETIRE, (STATE_RETIRED, reason, [str(i) for i in check_ids]))
    return cursor.rowcount


def retire_duplicates(cursor, org_id: str) -> int:
    """Two checks that run the same command with the same assertion are one check."""
    cursor.execute(
        """SELECT id FROM (
               SELECT id, state, ROW_NUMBER() OVER (
                   PARTITION BY provider, command, assertion
                   ORDER BY (state = 'proven') DESC, """ + _STRONGEST_FIRST + """
               ) AS place
               FROM detection_checks WHERE org_id = %s AND state <> %s
           ) ranked WHERE place > 1 AND state = %s""",
        (org_id, STATE_RETIRED, STATE_SILENT),
    )
    return _retire(cursor, [r[0] for r in cursor.fetchall()],
                   "Duplicate of another check with the same command and condition.")


def retire_only_ever_wrong(cursor, org_id: str) -> int:
    """A silent check that has fired on nothing, never on something, for a long time.

    A check that has never fired is left alone: it costs one query per run and
    may be exactly right the day its failure happens.
    """
    cursor.execute(
        """SELECT id FROM detection_checks
           WHERE org_id = %s AND state = %s AND hit_count = 0 AND false_fire_count > 0
             AND updated_at <= NOW() - %s * INTERVAL '1 day'""",
        (org_id, STATE_SILENT, RETIRE_AFTER_DAYS),
    )
    return _retire(cursor, [r[0] for r in cursor.fetchall()],
                   "Fired only on nothing for %d days." % RETIRE_AFTER_DAYS)


def retire_over_ceiling(cursor, org_id: str) -> int:
    """Past the per-asset ceiling, the weakest silent checks leave."""
    cursor.execute(
        """SELECT id FROM (
               SELECT id, state, ROW_NUMBER() OVER (
                   PARTITION BY lower(asset)
                   ORDER BY (state = 'proven') DESC, """ + _STRONGEST_FIRST + """
               ) AS place
               FROM detection_checks WHERE org_id = %s AND state <> %s
           ) ranked WHERE place > %s AND state = %s""",
        (org_id, STATE_RETIRED, MAX_ACTIVE_CHECKS_PER_ASSET, STATE_SILENT),
    )
    return _retire(cursor, [r[0] for r in cursor.fetchall()],
                   "Asset had more than %d checks; this one had the weakest record."
                   % MAX_ACTIVE_CHECKS_PER_ASSET)


def consolidate(cursor, org_id: str) -> Dict[str, int]:
    """Run every pass for one org. Order matters: duplicates leave before the
    ceiling is counted, so a copy does not push out a distinct check."""
    return {
        "duplicates": retire_duplicates(cursor, org_id),
        "only_ever_wrong": retire_only_ever_wrong(cursor, org_id),
        "over_ceiling": retire_over_ceiling(cursor, org_id),
    }
