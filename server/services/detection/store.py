"""Persistence for detection checks.

Flask-free, like services/artifacts/store.py: every function takes a
caller-supplied cursor, and the caller owns the connection, the RLS context and
the commit.

A check is addressed by (org_id, name). Saving a changed definition adds a
revision and sends the check back to ``silent``, so no version of a check can
reach a person on an earlier version's record.
"""

import json
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
from services.detection.assertion import Verdict, validation_error
from utils.security.read_only_classifier import describe_rejection, split_segments

# The fields that make up a check's definition. A change to any of them is a
# new revision; nothing else is.
DEFINITION_FIELDS = (
    "prediction", "asset", "pattern", "provider", "target", "command", "assertion",
    "interval_seconds",
)

_MAX_NAME = 255
_MAX_ASSET = 512
_MAX_PATTERN = 128
_MAX_PROVIDER = 32
_MAX_TARGET = 255
_MAX_COMMAND = 4000
_MAX_PREDICTION = 2000
_DETAIL_MAX_CHARS = 2000

# A claim older than this belongs to a run that died; the check is due again.
CLAIM_TTL_SECONDS = 600


class CheckRetired(Exception):
    """Raised when a save targets a check that was retired."""


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
    return definition, ""


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

    rejection = describe_rejection(definition["command"])
    if rejection:
        return None, "command is not read-only: " + rejection
    if definition["provider"] == PROVIDER_ONPREM_KUBECTL:
        problem = _onprem_problem(definition)
        if problem:
            return None, problem

    problem = validation_error(raw.get("assertion"))
    if problem:
        return None, problem
    assertion = raw["assertion"]
    definition["assertion"] = {
        "expect": assertion["expect"], "op": assertion["op"], "value": assertion.get("value"),
    }

    interval = raw.get("interval_seconds", DEFAULT_INTERVAL_SECONDS)
    if (isinstance(interval, bool) or not isinstance(interval, int)
            or not MIN_INTERVAL_SECONDS <= interval <= MAX_INTERVAL_SECONDS):
        return None, "interval_seconds must be an integer between %d and %d" % (
            MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS)
    definition["interval_seconds"] = interval
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
) -> Tuple[str, int, bool]:
    """Create a check, or revise the one with the same name.

    Returns (check_id, revision, changed). An identical definition is a no-op:
    a model that re-saves the same check every run must not grow the history.
    Raises CheckRetired when the name belongs to a retired check, so a check
    someone asked Aurora to stop is not quietly brought back.
    """
    row = _lock_by_name(cursor, org_id, definition["name"])
    if row is None:
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
        if inserted:
            check_id = str(inserted[0])
            insert_revision(cursor, check_id, org_id, user_id, 1, definition, reason, source)
            return check_id, 1, True
        # Another session created this name between the lookup and the insert.
        # Its row is committed now; apply the usual rules to it.
        row = _lock_by_name(cursor, org_id, definition["name"])
        if row is None:
            raise RuntimeError("detection check vanished during save")

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


def _lock_by_name(cursor, org_id: str, name: str):
    cursor.execute(
        """SELECT id, revision, state, retired_reason, """ + ", ".join(DEFINITION_FIELDS) + """
           FROM detection_checks WHERE org_id = %s AND name = %s FOR UPDATE""",
        (org_id, name),
    )
    return cursor.fetchone()


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
    "target", "command", "assertion", "interval_seconds", "run_count", "fail_count", "error_count",
    "consecutive_errors", "hit_count", "false_fire_count", "last_status", "last_observed", "last_run_at", "retired_reason",
)


def list_checks(cursor, org_id: str, include_retired: bool = False) -> List[Dict[str, Any]]:
    """Every check with its record, newest first."""
    cursor.execute(
        "SELECT " + ", ".join(_LIST_COLUMNS) + """
           FROM detection_checks
           WHERE org_id = %s AND (%s OR state <> %s)
           ORDER BY created_at DESC""",
        (org_id, include_retired, STATE_RETIRED),
    )
    checks = []
    for row in cursor.fetchall():
        check = dict(zip(_LIST_COLUMNS, row))
        check["id"] = str(check["id"])
        if check["last_run_at"] is not None:
            check["last_run_at"] = check["last_run_at"].isoformat()
        checks.append(check)
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
               WHERE org_id = %s AND state <> %s
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
        """SELECT provider, command, assertion, target, revision, created_by
           FROM detection_checks
           WHERE id = %s AND state <> %s""",
        (check_id, STATE_RETIRED),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return {
        "provider": row[0], "command": row[1], "assertion": row[2], "target": row[3],
        "revision": row[4], "created_by": row[5],
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
        """SELECT last_status, revision, state, pattern, asset, consecutive_errors, claimed_at
           FROM detection_checks WHERE id = %s FOR UPDATE""",
        (check_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return Recorded()
    previous, current_revision, state, pattern, asset, consecutive_errors, held = row
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
    consecutive_errors = consecutive_errors + 1 if is_error else 0

    cursor.execute(
        """UPDATE detection_checks
           SET run_count = run_count + 1,
               fail_count = fail_count + %s,
               error_count = error_count + %s,
               consecutive_errors = %s,
               last_status = %s, last_observed = %s, last_run_at = NOW()
           WHERE id = %s""",
        (
            int(verdict.status == STATUS_FAIL), int(is_error), consecutive_errors,
            verdict.status, verdict.observed, check_id,
        ),
    )
    _release_claim(cursor, check_id, claim)

    fire_id = None
    if verdict.status == STATUS_PASS:
        cursor.execute(
            "UPDATE detection_check_fires SET cleared_at = NOW() "
            "WHERE check_id = %s AND cleared_at IS NULL",
            (check_id,),
        )
    elif verdict.status == STATUS_FAIL:
        cursor.execute(
            "SELECT 1 FROM detection_check_fires "
            "WHERE check_id = %s AND revision = %s AND cleared_at IS NULL",
            (check_id, revision),
        )
        if cursor.fetchone() is None:
            cursor.execute(
                """INSERT INTO detection_check_fires
                   (check_id, org_id, revision, pattern, asset, observed)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                (check_id, org_id, revision, pattern, asset, verdict.observed),
            )
            fire_id = str(cursor.fetchone()[0])

    if not (verdict.status == STATUS_PASS and previous == STATUS_PASS):
        cursor.execute(
            """INSERT INTO detection_check_runs
               (check_id, org_id, revision, status, observed, detail, duration_ms)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (check_id, org_id, revision, verdict.status, verdict.observed,
             verdict.detail[:_DETAIL_MAX_CHARS], duration_ms),
        )
    return Recorded(state=state, fire_id=fire_id, consecutive_errors=consecutive_errors)


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
    "name", "state", "revision", "origin", "prediction", "asset", "provider", "command",
    "assertion", "created_by",
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
