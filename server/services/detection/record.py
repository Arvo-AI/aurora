"""How a check earns, keeps and loses the right to open a case.

Nobody approves a check. Its record decides, and the record is read from what
happened after it fired:

- A fire is a **hit** when an incident on the same asset was confirmed around it.
- A fire is **false** when it cleared, or its window closed, with no such incident.

One check sees too few incidents to prove itself, so trust is pooled by
pattern: every check of the same kind in the org shares one record. A check is
promoted on its pattern's record and demoted on its own.

Slow to earn, fast to lose. The thresholds are starting values.
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple

from services.detection import STATE_PROVEN, STATE_RETIRED, STATE_SILENT

OUTCOME_HIT = "hit"
OUTCOME_FALSE = "false"

PROMOTE_MIN_HITS = 3
PROMOTE_MIN_PRECISION = 0.8
DEMOTE_AFTER_CONSECUTIVE_FALSE = 2
# How long after a fire an incident on the same asset still counts for it, and
# how long before: a check that fires minutes after a human report still saw
# the real problem.
GRADE_WINDOW_HOURS = 24
GRADE_LOOKBACK_HOURS = 1
# Evidence has to fall within the failure itself, give or take: an incident
# declared up to this long after the check went healthy again still counts,
# one declared the next day does not.
GRADE_GRACE_HOURS = 2
RETIRE_AFTER_DAYS = 90
MAX_ACTIVE_CHECKS_PER_ASSET = 8


# How a pattern's hits are counted, as SQL over detection_check_fires with the
# hit outcome as its one parameter. By incident, not by fire: a check that flaps
# three times during one real incident has been right once.
HITS_SQL = "COUNT(DISTINCT evidence_incident_id) FILTER (WHERE outcome = %s)"


@dataclass(frozen=True)
class PatternRecord:
    hits: int = 0
    false_fires: int = 0

    @property
    def precision(self) -> float:
        graded = self.hits + self.false_fires
        return self.hits / graded if graded else 0.0


def earns_promotion(pattern: PatternRecord, false_fires_this_revision: int) -> bool:
    """Whether a silent check may become proven.

    The pattern must have caught real incidents often enough, and this version
    of this check must never have fired on nothing.
    """
    return (
        false_fires_this_revision == 0
        and pattern.hits >= PROMOTE_MIN_HITS
        and pattern.precision >= PROMOTE_MIN_PRECISION
    )


def loses_proven(consecutive_false_fires: int) -> bool:
    return consecutive_false_fires >= DEMOTE_AFTER_CONSECUTIVE_FALSE


def asset_matches(asset: str, candidates: List[Optional[str]]) -> bool:
    """Whether an incident's services name the check's asset.

    Exact match after trimming and lowercasing. A looser match (substring,
    similarity) would let a check on `api` take credit for an incident on
    `api-gateway`, and credit is what promotes a check.
    """
    wanted = (asset or "").strip().lower()
    if not wanted:
        return False
    return any((c or "").strip().lower() == wanted for c in candidates)


def pattern_record(cursor, org_id: str, pattern: str) -> PatternRecord:
    # Hits are counted by incident, not by fire: a check that flaps three times
    # during one real incident has been right once.
    cursor.execute(
        "SELECT " + HITS_SQL + """,
                  COUNT(*) FILTER (WHERE outcome = %s)
           FROM detection_check_fires WHERE org_id = %s AND pattern = %s""",
        (OUTCOME_HIT, OUTCOME_FALSE, org_id, pattern),
    )
    hits, false_fires = cursor.fetchone()
    return PatternRecord(hits=hits or 0, false_fires=false_fires or 0)


def apply_outcome(cursor, fire_id: str, check_id: str, org_id: str, outcome: str,
                  evidence_incident_id: Optional[str] = None) -> Tuple[str, str]:
    """Grade one fire and move the check's state if its record now says so.

    Returns (old_state, new_state). A demotion caused by a revision restores the
    revision before it: the change that made the check noisy is undone.
    """
    cursor.execute(
        "UPDATE detection_check_fires SET outcome = %s, graded_at = NOW(), "
        "evidence_incident_id = %s WHERE id = %s AND outcome IS NULL RETURNING revision",
        (outcome, evidence_incident_id, fire_id),
    )
    graded = cursor.fetchone()
    if graded is None:
        return "", ""
    fire_revision = graded[0]

    cursor.execute(
        "SELECT state, pattern, revision FROM detection_checks WHERE id = %s FOR UPDATE",
        (check_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return "", ""
    state, pattern, revision = row

    if fire_revision != revision:
        _count_on_old_revision(cursor, check_id, outcome)
        return state, state

    revision_false, consecutive_false = _count_on_current_revision(cursor, check_id, outcome)
    if state == STATE_RETIRED:
        return state, state

    if outcome == OUTCOME_HIT:
        promoted = state == STATE_SILENT and earns_promotion(
            pattern_record(cursor, org_id, pattern), revision_false)
        new_state = STATE_PROVEN if promoted else state
    else:
        new_state = _after_false_fire(cursor, check_id, org_id, state, revision, consecutive_false)

    if new_state != state:
        cursor.execute(
            "UPDATE detection_checks SET state = %s, updated_at = NOW() WHERE id = %s",
            (new_state, check_id),
        )
    return state, new_state


def _count_on_old_revision(cursor, check_id: str, outcome: str) -> None:
    """Count a fire from an earlier version, graded after the check was revised.

    It goes into the lifetime totals and the pattern's record, but it says
    nothing about the current version: two old false fires must not demote a
    revision that has never failed.
    """
    column = "hit_count" if outcome == OUTCOME_HIT else "false_fire_count"
    cursor.execute(
        "UPDATE detection_checks SET " + column + " = " + column + " + 1 WHERE id = %s",
        (check_id,),
    )


def _after_false_fire(cursor, check_id: str, org_id: str, state: str, revision: int,
                      consecutive_false: int) -> str:
    """What a false fire on the current revision does. Returns the check's state after it."""
    if consecutive_false == DEMOTE_AFTER_CONSECUTIVE_FALSE:
        # Proven or not: a check that keeps firing on nothing is handed to the
        # agent to revise or retire. Once per streak.
        from services.detection.store import request_review
        request_review(cursor, check_id,
                       "It fired on nothing %d times in a row." % consecutive_false)
    if state != STATE_PROVEN or not loses_proven(consecutive_false):
        return state
    _restore_previous_revision(cursor, check_id, org_id, revision)
    return STATE_SILENT


def _count_on_current_revision(cursor, check_id: str, outcome: str) -> Tuple[int, int]:
    """Add one graded fire to the check's counters.

    Returns (false fires of this revision, false fires in a row). A hit ends
    the run of false fires.
    """
    if outcome == OUTCOME_HIT:
        cursor.execute(
            "UPDATE detection_checks SET hit_count = hit_count + 1, consecutive_false_fires = 0 "
            "WHERE id = %s RETURNING revision_false_fires, consecutive_false_fires",
            (check_id,),
        )
    else:
        cursor.execute(
            """UPDATE detection_checks
               SET false_fire_count = false_fire_count + 1,
                   consecutive_false_fires = consecutive_false_fires + 1,
                   revision_false_fires = revision_false_fires + 1
               WHERE id = %s RETURNING revision_false_fires, consecutive_false_fires""",
            (check_id,),
        )
    revision_false, consecutive_false = cursor.fetchone()
    return revision_false, consecutive_false


def promote_pattern_siblings(cursor, org_id: str, pattern: str) -> int:
    """Promote every silent check of *pattern* its record now covers.

    A hit on one check can carry the whole pattern over the bar, so its
    siblings are looked at too, not only the check that fired.
    """
    record = pattern_record(cursor, org_id, pattern)
    if not earns_promotion(record, 0):
        return 0
    cursor.execute(
        """UPDATE detection_checks SET state = %s, updated_at = NOW()
           WHERE org_id = %s AND pattern = %s AND state = %s AND revision_false_fires = 0""",
        (STATE_PROVEN, org_id, pattern, STATE_SILENT),
    )
    return cursor.rowcount


def _restore_previous_revision(cursor, check_id: str, org_id: str, revision: int) -> bool:
    """Put back the definition from the revision before *revision*, as a new revision.

    Only when the current revision has never produced a hit: a version that
    caught something real is kept even if it then turned noisy.
    """
    if revision <= 1:
        return False
    cursor.execute(
        "SELECT 1 FROM detection_check_fires WHERE check_id = %s AND revision = %s AND outcome = %s",
        (check_id, revision, OUTCOME_HIT),
    )
    if cursor.fetchone() is not None:
        return False
    cursor.execute(
        "SELECT definition, created_by FROM detection_check_revisions "
        "WHERE check_id = %s AND revision = %s",
        (check_id, revision - 1),
    )
    row = cursor.fetchone()
    if row is None:
        return False
    definition, created_by = row

    from services.detection.store import complete_definition, insert_revision, set_definition

    definition = complete_definition(definition)
    if definition is None:
        return False
    set_definition(cursor, check_id, definition, revision + 1)
    insert_revision(
        cursor, check_id, org_id, created_by, revision + 1, definition,
        "Restored revision %d: revision %d fired on nothing twice." % (revision - 1, revision),
        "system",
    )
    return True


_GRADE_BATCH = 200


def _independent_incident(cursor, org_id: str, asset: str, fired_at, until) -> Optional[str]:
    """An incident on *asset* between shortly before *fired_at* and *until* that no
    check opened, or None.

    Incidents Aurora opened from a check are excluded: a check's own case is
    never evidence that the check was right. So is an investigation someone
    started by hand that nobody has resolved: it may be a person looking into
    this very failure, which confirms nothing. An incident raised by an alert
    source counts as it stands.
    """
    cursor.execute(
        """SELECT i.id, i.alert_service, i.affected_services
           FROM incidents i
           WHERE i.org_id = %s
             AND i.created_at >= %s::timestamptz - %s * INTERVAL '1 hour'
             AND i.created_at <= %s::timestamptz
             AND (i.source_type <> 'chat' OR i.status = 'resolved')
             AND NOT EXISTS (
                 SELECT 1 FROM detection_check_fires f
                 WHERE f.incident_id = i.id AND f.opened_incident
             )
           ORDER BY i.created_at""",
        (org_id, fired_at, GRADE_LOOKBACK_HOURS, until),
    )
    for incident_id, service, affected in cursor.fetchall():
        if asset_matches(asset, [service, *(affected or [])]):
            return str(incident_id)
    return None


def _own_case_was_corroborated(cursor, incident_id: Optional[str], asset: str, until) -> bool:
    """Whether an alert about the same asset joined the case this fire opened.

    The correlator attaches a later alert to an open incident instead of opening
    a second one. Without this, a proven check that fires first would never be
    credited: the monitor's alert lands inside its own case, no independent
    incident ever exists, and two such "false" fires would demote a check for
    being early. Only an alert naming the same asset counts; one the correlator
    attached from a neighbouring service does not.
    """
    if not incident_id:
        return False
    # Bounded like any other evidence: an alert that arrives after the fire's
    # window has closed does not turn a false fire into a hit.
    cursor.execute(
        "SELECT alert_service FROM incident_alerts "
        "WHERE incident_id = %s AND received_at <= %s::timestamptz",
        (incident_id, until),
    )
    return asset_matches(asset, [row[0] for row in cursor.fetchall()])


def grade_fires(cursor, org_id: str) -> Tuple[int, int]:
    """Grade every fire whose outcome is now known. Returns (hits, false_fires).

    A hit is recorded as soon as the evidence exists. A fire is only called
    false once its whole window has passed with none.
    """
    # A fire's window runs from the failure to a little after it cleared, and at
    # most GRADE_WINDOW_HOURS for one that never clears. Without the bound, a
    # failure that cleared in minutes would be credited with whatever happened
    # to the same asset the next day.
    cursor.execute(
        """SELECT id, check_id, asset, pattern, fired_at, incident_id, opened_incident, until,
                  until <= NOW() AS window_closed
           FROM (
               SELECT *, LEAST(COALESCE(cleared_at, 'infinity'::timestamptz) + %s * INTERVAL '1 hour',
                               fired_at + %s * INTERVAL '1 hour') AS until
               FROM detection_check_fires
               WHERE org_id = %s AND outcome IS NULL
           ) open_fires
           ORDER BY fired_at LIMIT %s""",
        (GRADE_GRACE_HOURS, GRADE_WINDOW_HOURS, org_id, _GRADE_BATCH),
    )
    hits = false_fires = 0
    for (fire_id, check_id, asset, pattern, fired_at, incident_id, opened, until,
         window_closed) in cursor.fetchall():
        evidence = _independent_incident(cursor, org_id, asset, fired_at, until)
        if evidence is None and opened and _own_case_was_corroborated(
                cursor, incident_id, asset, until):
            evidence = str(incident_id)
        if evidence is not None:
            apply_outcome(cursor, str(fire_id), str(check_id), org_id, OUTCOME_HIT, evidence)
            promote_pattern_siblings(cursor, org_id, pattern)
            hits += 1
        elif window_closed:
            apply_outcome(cursor, str(fire_id), str(check_id), org_id, OUTCOME_FALSE)
            false_fires += 1
    return hits, false_fires


def credit_replay(cursor, check_id: str, org_id: str, revision: int, pattern: str, asset: str,
                  incident_id: str, incident_at) -> None:
    """Record that this check, replayed, would have caught *incident_id*.

    Stored as a fire at the incident's time, already cleared and graded, with
    the incident as its evidence. It counts once for the pattern like any hit:
    hits are counted by incident, so it cannot be earned twice for the same one.
    Saving the same check again for the same incident does nothing.
    """
    cursor.execute(
        "SELECT 1 FROM detection_check_fires "
        "WHERE check_id = %s AND replayed AND evidence_incident_id = %s",
        (check_id, incident_id),
    )
    if cursor.fetchone() is not None:
        return
    cursor.execute(
        """INSERT INTO detection_check_fires
           (check_id, org_id, revision, pattern, asset, observed, fired_at, cleared_at, replayed)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE) RETURNING id""",
        (check_id, org_id, revision, pattern, asset, "replay", incident_at, incident_at),
    )
    fire_id = str(cursor.fetchone()[0])
    apply_outcome(cursor, fire_id, check_id, org_id, OUTCOME_HIT, incident_id)
    promote_pattern_siblings(cursor, org_id, pattern)
