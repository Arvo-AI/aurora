"""Persistence for detection checks.

Flask-free, like services/artifacts/store.py: every function takes a
caller-supplied cursor, and the caller owns the connection, the RLS context and
the commit.

A check is addressed by (org_id, name). Saving a changed definition adds a
revision and sends the check back to ``silent``, so no version of a check can
reach a person on an earlier version's record.
"""

import json
import re
import shlex
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from services.detection import (
    DEFAULT_INTERVAL_SECONDS,
    MAX_INTERVAL_SECONDS,
    MIN_INTERVAL_SECONDS,
    ORIGIN_KINDS,
    PROVIDER_ONPREM_KUBECTL,
    STATE_PROVEN,
    STATE_RETIRED,
    STATE_SILENT,
    STATUS_ERROR,
    STATUS_FAIL,
    STATUS_PASS,
)
from services.detection import window
from services.detection.assertion import Verdict, validation_error
from utils.security.read_only_classifier import describe_rejection, split_segments

# The fields that make up a check's definition. A change to any of them is a
# new revision; nothing else is.
DEFINITION_FIELDS = (
    "prediction", "asset", "pattern", "provider", "target", "command", "assertion",
    "interval_seconds", "window_seconds", "lag_seconds",
)
# Revisions written before a field existed do not carry it.
_FIELD_DEFAULTS = {"target": "", "window_seconds": 0, "lag_seconds": 0}

_MAX_NAME = 255
_MAX_ASSET = 512
_MAX_PATTERN = 128
_MAX_PROVIDER = 32
_MAX_TARGET = 255
_MAX_COMMAND = 4000
_MAX_PREDICTION = 2000
_DETAIL_MAX_CHARS = 2000

# Errors in a row before a check is handed to the agent to fix or retire.
REVIEW_AFTER_ERRORS = 5
# Checks handed over per review, so one pass stays a small piece of work.
REVIEW_BATCH = 5
_REVIEW_RUNS = 8
_REVIEW_FIRES = 5

# A claim older than this belongs to a run that died; the check is due again.
CLAIM_TTL_SECONDS = 600


class CheckRetired(Exception):
    """Raised when a save targets a check that was retired."""


class TooManyChecks(Exception):
    """Raised when an org is at its ceiling of active checks."""


# Every active check is a command run every few minutes on shared workers.
MAX_ACTIVE_CHECKS_PER_ORG = 200
# An asset is matched exactly against the service an incident names, so it has
# to be that bare name: no namespace, cluster or description around it.
_ASSET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
# Flags that keep a command open instead of answering once.
_STREAMING_FLAGS = ("-w", "--watch", "--watch-only", "-f", "--follow")


def _clean(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _text_fields(raw: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    """The name and the plain-text fields, trimmed and length-checked."""
    name = _clean(raw.get("name"))
    if not name or len(name) > _MAX_NAME:
        return None, "name must be 1-%d characters" % _MAX_NAME
    # name, asset and pattern end up in an incident title that is read by the
    # agent that posts to chat. Angle brackets are how chat platforms spell a
    # mention, and no identifier needs them.
    for field in ("name", "asset", "pattern"):
        if any(ch in _clean(raw.get(field)) for ch in "<>"):
            return None, "%s must not contain '<' or '>'" % field

    limits = (
        ("prediction", _MAX_PREDICTION),
        ("asset", _MAX_ASSET),
        ("pattern", _MAX_PATTERN),
        ("provider", _MAX_PROVIDER),
        ("command", _MAX_COMMAND),
    )
    definition: Dict[str, Any] = {"name": name}
    for field, limit in limits:
        value = _clean(raw.get(field))
        if not value or len(value) > limit:
            return None, "%s must be 1-%d characters" % (field, limit)
        definition[field] = value
    definition["provider"] = definition["provider"].lower()
    definition["pattern"] = definition["pattern"].lower()
    if not _ASSET.match(definition["asset"]):
        return None, ("asset must be the bare service or resource name an incident would carry "
                      "(for example 'checkout'), with no spaces, namespace or description")
    return definition, ""


def streaming_flag(command: str) -> str:
    """The flag that would keep *command* running instead of answering, or ''.

    `kubectl get pods -w` and `logs --follow` are reads, so the read-only
    classifier accepts them, but they never finish: each run would hold a
    worker until the timeout.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return ""
    for token in tokens:
        flag = token.split("=", 1)[0]
        if flag in _STREAMING_FLAGS:
            return flag
    return ""


def _command_problem(definition: Dict[str, Any], window_seconds: Any, lag_seconds: Any) -> str:
    """Why the command cannot be saved, or ''. Sets the definition's window and lag.

    Expects definition['interval_seconds'] to be set already: a window is
    checked against how often the check runs.
    """
    problem = window.problem(
        definition["command"], window_seconds, definition["interval_seconds"], lag_seconds,
    )
    if problem:
        return problem
    definition["window_seconds"] = window_seconds
    definition["lag_seconds"] = lag_seconds

    # Classified with its span filled in: that is the command that will run.
    rejection = describe_rejection(window.render(definition["command"], window_seconds))
    if rejection:
        return "command is not read-only: " + rejection
    flag = streaming_flag(definition["command"])
    if flag:
        return "command must answer once and exit: remove %s" % flag
    if definition["provider"] == PROVIDER_ONPREM_KUBECTL:
        return _onprem_problem(definition)
    return ""


def normalize_definition(raw: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    """Return (definition, '') or (None, why it is refused).

    The command must be one the read-only classifier recognises as a read. The
    same classifier runs again before every execution, so a row edited behind
    this function still cannot run a write.
    """
    definition, problem = _text_fields(raw)
    if problem:
        return None, problem

    # Which account, subscription or cluster the command runs against. Optional
    # for cloud providers; an on-prem cluster cannot be reached without it.
    target = _clean(raw.get("target"))
    if len(target) > _MAX_TARGET:
        return None, "target must be at most %d characters" % _MAX_TARGET
    definition["target"] = target

    interval = raw.get("interval_seconds", DEFAULT_INTERVAL_SECONDS)
    if (isinstance(interval, bool) or not isinstance(interval, int)
            or not MIN_INTERVAL_SECONDS <= interval <= MAX_INTERVAL_SECONDS):
        return None, "interval_seconds must be an integer between %d and %d" % (
            MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS)
    definition["interval_seconds"] = interval

    problem = _command_problem(
        definition, raw.get("window_seconds") or 0, raw.get("lag_seconds") or 0,
    )
    if problem:
        return None, problem

    problem = validation_error(raw.get("assertion"))
    if problem:
        return None, problem
    assertion = raw["assertion"]
    definition["assertion"] = {
        "expect": assertion["expect"], "op": assertion["op"], "value": assertion.get("value"),
    }

    return definition, ""


def has_shell_operators(command: str) -> bool:
    """Whether *command* is more than one plain program invocation."""
    segments = split_segments(command)
    return segments is None or len(segments) != 1


def _onprem_problem(definition: Dict[str, Any]) -> str:
    """Why *definition* cannot run on an on-prem cluster, or ''.

    That executor starts kubectl directly with no shell, so a pipe would reach
    kubectl as arguments and every run would error.
    """
    if not definition["target"]:
        return "target must be the cluster_id for an onprem_kubectl check"
    if has_shell_operators(definition["command"]):
        return ("an onprem_kubectl command runs without a shell: no pipes or chaining. "
                "Shape the output with -o jsonpath / --no-headers and use expect 'lines' or 'text'")
    return ""


def normalize_origin(raw: Any) -> Tuple[Optional[Dict[str, str]], str]:
    """Return ({kind, ref}, '') or (None, why it is refused)."""
    if not isinstance(raw, dict) or raw.get("kind") not in ORIGIN_KINDS:
        return None, "origin.kind must be one of: " + ", ".join(ORIGIN_KINDS)
    return {"kind": raw["kind"], "ref": _clean(raw.get("ref"))[:_MAX_ASSET]}, ""


def _definition_of(row_values: Tuple) -> Dict[str, Any]:
    return dict(zip(DEFINITION_FIELDS, row_values))


def save_check(
    cursor,
    org_id: str,
    user_id: str,
    definition: Dict[str, Any],
    *,
    origin: Dict[str, str],
    reason: str,
    source: str,
    vouched_by: Optional[str] = None,
) -> Tuple[str, int, bool]:
    """Create a check, or revise the one with the same name.

    *vouched_by* is the person who asked for this check, when one did (see
    vouch_check). The version they asked for starts ``proven``.

    Returns (check_id, revision, changed). An identical definition is a no-op:
    a model that re-saves the same check every run must not grow the history.
    Raises CheckRetired when the name belongs to a retired check, so a check
    someone asked Aurora to stop is not quietly brought back.
    """
    row = _lock_by_name(cursor, org_id, definition["name"])
    if row is None:
        check_id = _insert_new(cursor, org_id, user_id, definition, origin)
        if check_id:
            insert_revision(cursor, check_id, org_id, user_id, 1, definition, reason, source)
            revision, changed = 1, True
        else:
            # Another session created this name between the lookup and the
            # insert. Its row is committed now; apply the usual rules to it.
            row = _lock_by_name(cursor, org_id, definition["name"])
            if row is None:
                raise RuntimeError("detection check vanished during save")
    if row is not None:
        check_id, revision, changed = _revise(cursor, org_id, user_id, row, definition, reason, source)
    if vouched_by:
        vouch_check(cursor, org_id, check_id, vouched_by)
    return check_id, revision, changed


def _insert_new(cursor, org_id: str, user_id: str, definition: Dict[str, Any],
                origin: Dict[str, str]) -> Optional[str]:
    """Insert a check under a new name. None when another session took the name first."""
    cursor.execute(
        "SELECT COUNT(*) FROM detection_checks WHERE org_id = %s AND state <> %s",
        (org_id, STATE_RETIRED),
    )
    if cursor.fetchone()[0] >= MAX_ACTIVE_CHECKS_PER_ORG:
        raise TooManyChecks()
    values = [definition[f] for f in DEFINITION_FIELDS]
    values[DEFINITION_FIELDS.index("assertion")] = json.dumps(definition["assertion"])
    cursor.execute(
        """INSERT INTO detection_checks
           (org_id, created_by, name, origin, """ + ", ".join(DEFINITION_FIELDS) + """)
           VALUES (""" + ", ".join(["%s"] * (4 + len(DEFINITION_FIELDS))) + """)
           ON CONFLICT (org_id, name) DO NOTHING
           RETURNING id""",
        (org_id, user_id, definition["name"], json.dumps(origin), *values),
    )
    inserted = cursor.fetchone()
    return str(inserted[0]) if inserted else None


def _revise(cursor, org_id: str, user_id: str, row: Tuple, definition: Dict[str, Any],
            reason: str, source: str) -> Tuple[str, int, bool]:
    """Apply *definition* to the existing check in *row*: (check_id, revision, changed)."""
    check_id, revision, state = str(row[0]), row[1], row[2]
    if state == STATE_RETIRED:
        raise CheckRetired(row[3] or "")
    current = _definition_of(row[4:])
    if all(current[f] == definition[f] for f in DEFINITION_FIELDS):
        return check_id, revision, False
    revision += 1
    set_definition(cursor, check_id, definition, revision, owner=user_id)
    insert_revision(cursor, check_id, org_id, user_id, revision, definition, reason, source)
    return check_id, revision, True


def vouch_check(cursor, org_id: str, check_id: str, user_id: str) -> bool:
    """A person takes responsibility for a check: it opens a case when it fails.

    Aurora's own checks have to earn that from their record, because nobody
    asked for them. A check a person asked for, or one a person has looked at
    and decided to trust, does not have to wait for three incidents. It can
    still lose it the usual way: two fires in a row on nothing send it back to
    silent (record.loses_proven).
    """
    cursor.execute(
        """UPDATE detection_checks
           SET state = %s, vouched_by = %s, consecutive_false_fires = 0, updated_at = NOW()
           WHERE id = %s AND org_id = %s AND state = %s""",
        (STATE_PROVEN, user_id, check_id, org_id, STATE_SILENT),
    )
    return cursor.rowcount > 0


def _lock_by_name(cursor, org_id: str, name: str):
    cursor.execute(
        """SELECT id, revision, state, retired_reason, """ + ", ".join(DEFINITION_FIELDS) + """
           FROM detection_checks WHERE org_id = %s AND name = %s FOR UPDATE""",
        (org_id, name),
    )
    return cursor.fetchone()


def complete_definition(definition: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """A stored revision's definition with later-added fields defaulted, or None if unusable."""
    completed = {**_FIELD_DEFAULTS, **definition}
    return completed if all(f in completed for f in DEFINITION_FIELDS) else None


def set_definition(cursor, check_id: str, definition: Dict[str, Any], revision: int,
                   owner: Optional[str] = None) -> None:
    """Write *definition* onto the check as *revision*.

    Every change of definition sends the check back to ``silent`` and clears the
    per-revision counters: a new version earns its own record.

    *owner* is who saved the revision. Scheduled runs execute as the check's
    owner, and the command was gated under whoever saved it, so the two have to
    be the same person or a revision could pass the gate and then fail on every
    run. A restore by the system passes no owner and keeps the current one.

    A fire still open from the previous version is closed here. Left open, it
    would stop the new version from ever starting a fire of its own.
    """
    values = [definition[f] for f in DEFINITION_FIELDS]
    values[DEFINITION_FIELDS.index("assertion")] = json.dumps(definition["assertion"])
    assignments = ", ".join("%s = %%s" % f for f in DEFINITION_FIELDS)
    cursor.execute(
        "UPDATE detection_checks SET " + assignments + """,
               revision = %s, state = %s, revision_false_fires = 0,
               consecutive_false_fires = 0, created_by = COALESCE(%s, created_by),
               error_streak = 0, review_due_at = NULL, review_reason = NULL, vouched_by = NULL,
               updated_at = NOW()
           WHERE id = %s""",
        (*values, revision, STATE_SILENT, owner, check_id),
    )
    cursor.execute(
        "UPDATE detection_check_fires SET cleared_at = NOW() "
        "WHERE check_id = %s AND cleared_at IS NULL AND revision < %s",
        (check_id, revision),
    )


def insert_revision(cursor, check_id, org_id, user_id, revision, definition, reason, source) -> None:
    cursor.execute(
        """INSERT INTO detection_check_revisions
           (check_id, org_id, created_by, revision, definition, reason, source)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (
            check_id, org_id, user_id, revision,
            json.dumps({f: definition[f] for f in DEFINITION_FIELDS}),
            _clean(reason)[:_DETAIL_MAX_CHARS], source,
        ),
    )


def retire_check_by_id(cursor, org_id: str, check_id: str, reason: str) -> bool:
    """Stop running the check with this id. Its record and revisions are kept."""
    cursor.execute(
        """UPDATE detection_checks
           SET state = %s, retired_reason = %s, updated_at = NOW()
           WHERE org_id = %s AND id = %s AND state <> %s""",
        (STATE_RETIRED, _clean(reason)[:_DETAIL_MAX_CHARS], org_id, check_id, STATE_RETIRED),
    )
    return cursor.rowcount > 0


def retire_check(cursor, org_id: str, name: str, reason: str) -> bool:
    """Stop running a check. Its record and revisions are kept."""
    cursor.execute(
        """UPDATE detection_checks
           SET state = %s, retired_reason = %s, updated_at = NOW()
           WHERE org_id = %s AND name = %s AND state <> %s""",
        (STATE_RETIRED, _clean(reason)[:_DETAIL_MAX_CHARS], org_id, _clean(name), STATE_RETIRED),
    )
    return cursor.rowcount > 0


_LIST_COLUMNS = (
    "id", "name", "state", "revision", "origin", "prediction", "asset", "pattern", "provider",
    "target", "command", "window_seconds", "lag_seconds", "assertion", "interval_seconds", "run_count", "fail_count", "error_count",
    "consecutive_errors", "hit_count", "false_fire_count", "last_status", "last_observed", "last_run_at", "retired_reason",
    "vouched_by", "review_reason", "paused_reason",
)


_SELECT_LISTED = "SELECT " + ", ".join(_LIST_COLUMNS)


def list_checks(cursor, org_id: str, include_retired: bool = False) -> List[Dict[str, Any]]:
    """Every check with its record, newest first."""
    cursor.execute(
        _SELECT_LISTED + """
           FROM detection_checks
           WHERE org_id = %s AND (%s OR state <> %s)
           ORDER BY created_at DESC""",
        (org_id, include_retired, STATE_RETIRED),
    )
    return [_listed(row) for row in cursor.fetchall()]


def _listed(row: Tuple) -> Dict[str, Any]:
    check = dict(zip(_LIST_COLUMNS, row))
    check["id"] = str(check["id"])
    if check["last_run_at"] is not None:
        check["last_run_at"] = check["last_run_at"].isoformat()
    return check


def request_review(cursor, check_id: str, reason: str) -> None:
    """Mark a check as needing a look: it was demoted, or it keeps erroring.

    Nothing reads a silent check's failures, so without this a check that
    stopped working would sit there until the cleanup retires it months later.
    """
    cursor.execute(
        "UPDATE detection_checks SET review_due_at = NOW(), review_reason = %s "
        "WHERE id = %s AND state <> %s",
        (reason[:500], check_id, STATE_RETIRED),
    )


def orgs_review_is_due(cursor, org_id: str) -> int:
    cursor.execute(
        "SELECT COUNT(*) FROM detection_checks "
        "WHERE org_id = %s AND review_due_at IS NOT NULL AND state <> %s",
        (org_id, STATE_RETIRED),
    )
    return cursor.fetchone()[0]


def take_checks_for_review(cursor, org_id: str, limit: int = REVIEW_BATCH) -> List[Dict[str, Any]]:
    """Hand over the checks waiting longest for review, with what a reviewer needs.

    Each comes with why it is here, its recent runs and its recent fires.
    Handing one over clears its mark: it comes back only if it is demoted or
    starts a new run of errors again, so a check nobody can fix is not
    reviewed every day.
    """
    cursor.execute(
        _SELECT_LISTED + """
           FROM detection_checks
           WHERE org_id = %s AND review_due_at IS NOT NULL AND state <> %s
           ORDER BY review_due_at LIMIT %s FOR UPDATE SKIP LOCKED""",
        (org_id, STATE_RETIRED, limit),
    )
    checks = [_listed(row) for row in cursor.fetchall()]
    for check in checks:
        cursor.execute(
            """SELECT status, COALESCE(observed, ''), COALESCE(detail, ''), created_at
               FROM detection_check_runs WHERE check_id = %s
               ORDER BY created_at DESC LIMIT %s""",
            (check["id"], _REVIEW_RUNS),
        )
        check["recent_runs"] = [
            {"status": r[0], "observed": r[1], "detail": r[2], "at": r[3].isoformat()}
            for r in cursor.fetchall()
        ]
        cursor.execute(
            """SELECT fired_at, cleared_at, outcome, COALESCE(observed, '')
               FROM detection_check_fires WHERE check_id = %s
               ORDER BY fired_at DESC LIMIT %s""",
            (check["id"], _REVIEW_FIRES),
        )
        check["recent_fires"] = [
            {"fired_at": r[0].isoformat(), "cleared_at": r[1].isoformat() if r[1] else None,
             "outcome": r[2], "observed": r[3]}
            for r in cursor.fetchall()
        ]
    if checks:
        cursor.execute(
            "UPDATE detection_checks SET review_due_at = NULL WHERE id = ANY(%s::uuid[])",
            ([c["id"] for c in checks],),
        )
    return checks


def claim_due_checks(cursor, org_id: str, limit: int) -> List[Tuple[str, str, str]]:
    """Claim up to *limit* due checks and return [(check_id, created_by, claim)].

    *claim* identifies this claim. The run hands it back to record_result, which
    only releases the claim it was given: a run that sat in the queue past the
    claim's lifetime must not release the claim of the run that replaced it.

    The claim is what keeps two overlapping ticks, or a tick that outruns a slow
    command, from running one check twice. SKIP LOCKED lets concurrent ticks
    take different rows instead of queueing on the same ones.
    """
    cursor.execute(
        """UPDATE detection_checks SET claimed_at = NOW()
           WHERE id IN (
               SELECT id FROM detection_checks
               WHERE org_id = %s AND state <> %s AND paused_at IS NULL
                 AND (last_run_at IS NULL
                      OR last_run_at <= NOW() - interval_seconds * INTERVAL '1 second')
                 AND (claimed_at IS NULL
                      OR claimed_at <= NOW() - %s * INTERVAL '1 second')
               ORDER BY last_run_at NULLS FIRST
               LIMIT %s
               FOR UPDATE SKIP LOCKED
           )
           RETURNING id, created_by, claimed_at""",
        (org_id, STATE_RETIRED, CLAIM_TTL_SECONDS, limit),
    )
    return [(str(row[0]), row[1], row[2].isoformat()) for row in cursor.fetchall()]


def get_runnable_check(cursor, check_id: str) -> Optional[Dict[str, Any]]:
    """The fields a run needs, or None when the check is gone or retired."""
    cursor.execute(
        """SELECT provider, command, assertion, target, revision, created_by, window_seconds,
                  lag_seconds
           FROM detection_checks
           WHERE id = %s AND state <> %s AND paused_at IS NULL""",
        (check_id, STATE_RETIRED),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return {
        "provider": row[0], "command": row[1], "assertion": row[2], "target": row[3],
        "revision": row[4], "created_by": row[5], "window_seconds": row[6],
        "lag_seconds": row[7],
    }


@dataclass(frozen=True)
class Recorded:
    """What one recorded run changed."""
    state: str = ""
    fire_id: Optional[str] = None      # set when this run started a new fire
    consecutive_errors: int = 0


def record_result(cursor, check_id: str, org_id: str, verdict: Verdict, duration_ms: int, *,
                  revision: Optional[int] = None, claim: Optional[str] = None) -> Recorded:
    """Fold one run into the check's record and release its claim.

    A row goes into detection_check_runs only when the run is worth reading
    later: a failure, an error, or a change of status. A healthy check run every
    five minutes would otherwise write 288 identical rows a day. The counters on
    the check carry the totals.

    A fire is the moment a check starts failing. It stays open while the check
    keeps failing or erroring, and clears on the next pass, so a check that
    fails for an hour is one fire, not twelve.

    *revision* is the version that was executed. If the check was revised while
    the command ran, the verdict belongs to a definition that no longer exists
    and is dropped rather than counted against the new one.

    *claim* is the claim the run was started under. A run whose claim has been
    taken over records nothing.
    """
    cursor.execute(
        """SELECT last_status, revision, state, pattern, asset, consecutive_errors, claimed_at,
                  error_streak
           FROM detection_checks WHERE id = %s FOR UPDATE""",
        (check_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return Recorded()
    (previous, current_revision, state, pattern, asset, consecutive_errors, held,
     error_streak) = row
    if claim is not None and (held is None or held.isoformat() != claim):
        # This run's claim expired and another run took the check. Whatever this
        # one saw is older than what that run records: writing it now could put
        # a stale failure over a newer pass and start a fire for it.
        return Recorded()
    if revision is not None and revision != current_revision:
        _release_claim(cursor, check_id, claim)
        return Recorded()
    revision = current_revision
    is_error = verdict.status == STATUS_ERROR
    # Counts runs that could not execute, which is what "Aurora has lost access"
    # is read from. A run that executed and returned something unreadable is an
    # error in the totals but resets this.
    consecutive_errors = consecutive_errors + 1 if not verdict.ran else 0
    # Every error in a row, readable or not. A check whose output stopped being
    # a number errors on every run without ever looking like lost access.
    error_streak = error_streak + 1 if is_error else 0

    cursor.execute(
        """UPDATE detection_checks
           SET run_count = run_count + 1,
               fail_count = fail_count + %s,
               error_count = error_count + %s,
               consecutive_errors = %s, error_streak = %s,
               last_status = %s, last_observed = %s, last_run_at = NOW()
           WHERE id = %s""",
        (
            int(verdict.status == STATUS_FAIL), int(is_error), consecutive_errors, error_streak,
            verdict.status, verdict.observed, check_id,
        ),
    )
    if error_streak == REVIEW_AFTER_ERRORS:
        # Once per streak: on the run that reaches the threshold, not every run after.
        request_review(cursor, check_id, "It has errored on %d runs in a row: %s"
                       % (error_streak, verdict.detail[:200] or "no detail"))
    _release_claim(cursor, check_id, claim)

    fire_id = _track_fire(cursor, check_id, org_id, revision, (pattern, asset), verdict)

    if not (verdict.status == STATUS_PASS and previous == STATUS_PASS):
        cursor.execute(
            """INSERT INTO detection_check_runs
               (check_id, org_id, revision, status, observed, detail, duration_ms)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (check_id, org_id, revision, verdict.status, verdict.observed,
             verdict.detail[:_DETAIL_MAX_CHARS], duration_ms),
        )
    return Recorded(state=state, fire_id=fire_id, consecutive_errors=consecutive_errors)


def _track_fire(cursor, check_id: str, org_id: str, revision: int, labels: Tuple[str, str],
                verdict: Verdict) -> Optional[str]:
    """Clear the open fire on a pass, start one on a failure. Returns a new fire's id.

    An error does neither: a check that cannot see has not recovered.
    *labels* is the check's (pattern, asset), copied onto the fire.
    """
    if verdict.status == STATUS_PASS:
        cursor.execute(
            "UPDATE detection_check_fires SET cleared_at = NOW() "
            "WHERE check_id = %s AND cleared_at IS NULL",
            (check_id,),
        )
        return None
    if verdict.status != STATUS_FAIL:
        return None
    cursor.execute(
        "SELECT 1 FROM detection_check_fires "
        "WHERE check_id = %s AND revision = %s AND cleared_at IS NULL",
        (check_id, revision),
    )
    if cursor.fetchone() is not None:
        return None
    cursor.execute(
        """INSERT INTO detection_check_fires
           (check_id, org_id, revision, pattern, asset, observed)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
        (check_id, org_id, revision, *labels, verdict.observed),
    )
    return str(cursor.fetchone()[0])


def _release_claim(cursor, check_id: str, claim: Optional[str]) -> None:
    """Release *claim* if it is still the one held.

    A caller with no claim to name releases nothing: a run queued before claims
    were passed along must not clear a claim it never held. That check then
    waits out the claim's lifetime, which costs one late run.
    """
    if claim is None:
        return
    cursor.execute(
        "UPDATE detection_checks SET claimed_at = NULL "
        "WHERE id = %s AND claimed_at = %s::timestamptz",
        (check_id, claim),
    )


_CASE_COLUMNS = (
    "name", "state", "revision", "origin", "prediction", "asset", "provider", "target", "command",
    "assertion", "window_seconds", "lag_seconds", "created_by",
)


def get_check_for_case(cursor, check_id: str) -> Optional[Dict[str, Any]]:
    """What a case's opening message says about the check that fired."""
    cursor.execute(
        "SELECT " + ", ".join(_CASE_COLUMNS) + " FROM detection_checks WHERE id = %s", (check_id,),
    )
    row = cursor.fetchone()
    return dict(zip(_CASE_COLUMNS, row)) if row else None


def link_fire_to_case(cursor, fire_id: str, incident_id: str, opened: bool) -> None:
    """Record the case a fire belongs to. *opened* is False when it joined one."""
    cursor.execute(
        "UPDATE detection_check_fires SET incident_id = %s, opened_incident = %s WHERE id = %s",
        (incident_id, opened, fire_id),
    )


def fires_awaiting_a_case(cursor, org_id: str, limit: int) -> List[Tuple[str, str, str, str]]:
    """Open fires on proven checks that have no case yet: [(fire_id, check_id, observed, created_by)].

    Covers a fire whose case could not be opened when it started (rate limit,
    dispatch failure) and a check promoted while it was already failing.
    """
    cursor.execute(
        """SELECT f.id, f.check_id, COALESCE(f.observed, ''), c.created_by
           FROM detection_check_fires f
           JOIN detection_checks c ON c.id = f.check_id
           WHERE f.org_id = %s AND c.state = %s AND f.revision = c.revision
             AND f.incident_id IS NULL AND f.cleared_at IS NULL AND f.outcome IS NULL
           ORDER BY f.fired_at LIMIT %s""",
        (org_id, STATE_PROVEN, limit),
    )
    return [(str(r[0]), str(r[1]), r[2], r[3]) for r in cursor.fetchall()]


CASE_CLAIM_TTL_SECONDS = 600


def reserve_case(cursor, fire_id: str) -> bool:
    """Take the right to give this fire a case. False when it no longer needs one.

    Checked here, under the write: the fire may have cleared, been graded or
    been given a case since it was selected, and two workers may be looking at
    it at once. A reservation expires, so a worker that died holding one does
    not leave the fire without a case.
    """
    cursor.execute(
        """UPDATE detection_check_fires SET case_claimed_at = NOW()
           WHERE id = %s AND incident_id IS NULL AND cleared_at IS NULL AND outcome IS NULL
             AND (case_claimed_at IS NULL
                  OR case_claimed_at <= NOW() - %s * INTERVAL '1 second')""",
        (fire_id, CASE_CLAIM_TTL_SECONDS),
    )
    return cursor.rowcount > 0


def record_replay(cursor, check_id: str, revision: int, replay: Dict[str, Any]) -> None:
    """Keep what the replay found beside the revision it tested."""
    cursor.execute(
        "UPDATE detection_check_revisions SET replay = %s WHERE check_id = %s AND revision = %s",
        (json.dumps(replay), check_id, revision),
    )
