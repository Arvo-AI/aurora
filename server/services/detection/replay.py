"""Try a check against the incident it was written for, before it counts.

A pull request asks a person whether a check looks right. A replay asks the
customer's own history whether it would have been right:

- over the span of its incident, the check must fail, or it measures the wrong
  thing and would pass through the next one too;
- over the same hours on earlier, quiet days, it must stay healthy, or it is
  noise that happened to be failing during the incident.

Only a windowed check whose command names both ends of its span can be
replayed (services/detection/window.py). Any other check, and any check whose
source no longer holds the data, is admitted silent and proves itself forward.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from services.detection import (
    STATE_PROVEN,
    STATE_SILENT,
    STATUS_DRILL_FAILED,
    STATUS_ERROR,
    STATUS_FAIL,
    STATUS_PASS,
)
from services.detection import window
from services.detection.assertion import Verdict
from services.detection.execution import judge

QUIET_DAYS_WANTED = 3
QUIET_DAYS_SEARCHED = 7
_UUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")

Runner = Callable[..., Verdict]


@dataclass(frozen=True)
class ReplayResult:
    # True: failed over its incident. False: stayed healthy. None: could not be tried.
    caught: Optional[bool]
    quiet_runs: int = 0
    quiet_fails: int = 0
    detail: str = ""

    def as_record(self, incident_id: str, replayed_to: datetime) -> Dict[str, Any]:
        return {
            "incident_id": incident_id, "replayed_to": replayed_to.isoformat(),
            "caught": self.caught, "quiet_runs": self.quiet_runs, "quiet_fails": self.quiet_fails,
            "detail": self.detail,
        }


def incident_id_from(origin_ref: str) -> Optional[str]:
    """The incident id inside an origin reference such as 'INC 3f2a…', or None."""
    match = _UUID.search(origin_ref or "")
    return match.group(0).lower() if match else None


def incident_time(cursor, org_id: str, incident_id: str) -> Optional[datetime]:
    """When the incident began, or None when it is not one of this org's."""
    cursor.execute(
        """SELECT COALESCE(alert_fired_at, started_at, created_at)::timestamptz
           FROM incidents WHERE id = %s AND org_id = %s""",
        (incident_id, org_id),
    )
    row = cursor.fetchone()
    return row[0] if row and row[0] else None


def replay_end(incident_at: datetime, window_seconds: int, now: Optional[datetime] = None) -> datetime:
    """The end of the span to replay: one window after the incident began, never in the future."""
    now = now or datetime.now(timezone.utc)
    return min(incident_at + timedelta(seconds=window_seconds), now)


def quiet_ends(cursor, org_id: str, end: datetime, window_seconds: int) -> List[datetime]:
    """The same time on earlier days whose whole replayed span had no incident.

    The span read for a candidate runs from *window_seconds* before it to the
    candidate itself, so that is what has to be incident-free, with a margin
    either side. Checking only around the end would call a two-day span quiet
    while the incident sat inside it, and a correct check would then be refused
    as noise. A day with any incident is skipped rather than guessed at.
    """
    ends = []
    for days_back in range(1, QUIET_DAYS_SEARCHED + 1):
        candidate = end - timedelta(days=days_back)
        cursor.execute(
            # The same "when it began" as incident_time(): an incident whose alert
            # fired before its row was created began at the alert.
            """SELECT 1 FROM incidents
               WHERE org_id = %s
                 AND COALESCE(alert_fired_at, started_at, created_at)::timestamptz
                     >= %s::timestamptz - %s * INTERVAL '1 second' - INTERVAL '12 hours'
                 AND COALESCE(alert_fired_at, started_at, created_at)::timestamptz
                     <= %s::timestamptz + INTERVAL '12 hours'
               LIMIT 1""",
            (org_id, candidate, window_seconds, candidate),
        )
        if cursor.fetchone() is None:
            ends.append(candidate)
        if len(ends) == QUIET_DAYS_WANTED:
            break
    return ends


def replay_origin(check: Dict[str, Any], user_id: str, end: datetime,
                  quiet: List[datetime], run: Runner = judge) -> ReplayResult:
    """Replay *check* over its incident (ending at *end*) and over *quiet* spans."""
    if not window.is_replayable(check["command"], check.get("window_seconds")):
        return ReplayResult(None, detail="the command does not name both ends of a time span")

    over_incident = run(check, user_id, "replay", at=end)
    if over_incident.status == STATUS_ERROR:
        return ReplayResult(None, detail="could not read that span: " + over_incident.detail)

    quiet_runs = quiet_fails = 0
    for quiet_end in quiet:
        verdict = run(check, user_id, "replay", at=quiet_end)
        if verdict.status == STATUS_ERROR:
            continue
        quiet_runs += 1
        quiet_fails += verdict.status == STATUS_FAIL
    if quiet_runs == 0:
        # Failing over the incident is only half of a replay. With no quiet day
        # read, nothing shows the check is ever healthy, and a check that always
        # fails would look exactly like this. Not replayed, so not credited.
        return ReplayResult(None, detail="no quiet day could be read to compare against")
    return ReplayResult(over_incident.status == STATUS_FAIL, quiet_runs, quiet_fails)


def refusal(result: ReplayResult) -> str:
    """Why a replayed check is not admitted, or '' when it is."""
    if result.caught is False:
        return ("Replayed over the incident it was written for, this check stayed healthy. "
                "It would not have caught it, so it measures the wrong thing.")
    if result.quiet_fails:
        return ("Replayed over the same hours on %d earlier day(s) with no incident, this check "
                "failed on %d. It fires on quiet history." % (result.quiet_runs, result.quiet_fails))
    return ""


def drill_targets(cursor, org_id: str) -> List[Dict[str, Any]]:
    """Proven checks whose current revision once caught its incident in replay."""
    cursor.execute(
        """SELECT c.id, c.created_by, c.name, c.provider, c.target, c.command, c.assertion,
                  c.window_seconds, c.revision, r.replay
           FROM detection_checks c
           JOIN detection_check_revisions r ON r.check_id = c.id AND r.revision = c.revision
           WHERE c.org_id = %s AND c.state = %s AND (r.replay->>'caught') = 'true'""",
        (org_id, STATE_PROVEN),
    )
    columns = ("id", "created_by", "name", "provider", "target", "command", "assertion",
               "window_seconds", "revision", "replay")
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def still_catches(target: Dict[str, Any], run: Runner = judge) -> Optional[bool]:
    """Re-run the replay that admitted the check. None when the source no longer has the span."""
    end = datetime.fromisoformat(target["replay"]["replayed_to"])
    verdict = run(target, target["created_by"], str(target["id"]), at=end)
    if verdict.status == STATUS_ERROR:
        return None
    return verdict.status != STATUS_PASS


def fail_drill(cursor, check_id: str, org_id: str, revision: int, revision_note: str) -> None:
    """A proven check that no longer catches its own incident goes back to silent.

    Matched on the revision that was replayed: if the check was revised and
    proven again while the drill ran, the stale result changes nothing.

    The revision is also marked as having fired falsely, which is what keeps
    the next hit for its pattern from promoting it straight back. A new revision
    that passes its own replay starts clean.
    """
    cursor.execute(
        """UPDATE detection_checks
           SET state = %s, revision_false_fires = revision_false_fires + 1, updated_at = NOW()
           WHERE id = %s AND state = %s AND revision = %s""",
        (STATE_SILENT, check_id, STATE_PROVEN, revision),
    )
    if cursor.rowcount == 0:
        return
    cursor.execute(
        """INSERT INTO detection_check_runs (check_id, org_id, revision, status, observed, detail)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (check_id, org_id, revision, STATUS_DRILL_FAILED, "", revision_note),
    )
