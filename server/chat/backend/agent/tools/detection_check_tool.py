"""Agent tools for detection memory: save, list and retire checks.

A check is a read-only command plus an assertion on its output. Aurora runs it
on a schedule with no model (services/detection/runner.py). Saving one changes
nothing in the customer's systems and notifies no one.
"""

import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, StrictFloat, StrictInt, StrictStr, ValidationError

from services.detection import (
    COVERAGE_SCAN_SOURCE,
    DEFAULT_INTERVAL_SECONDS,
    MIN_INTERVAL_SECONDS,
    ORIGIN_KINDS,
    checks_enabled,
)
from services.detection import replay, window
from services.detection.execution import gated_form
from services.detection.record import credit_replay
from services.detection.store import (
    CheckRetired,
    TooManyChecks,
    list_checks,
    normalize_definition,
    normalize_origin,
    record_replay,
    retire_check,
    save_check,
    start_active,
    take_checks_for_review,
)
from utils.auth.command_gate import gate_command
from utils.auth.stateless_auth import set_rls_context
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

_STATE_NOTES = {
    "silent": "It runs on its schedule and records results. A silent check notifies no one.",
    "proven": "It runs on its schedule. If it starts failing it opens a case.",
}
_UNMONITORED_NOTE = (
    "Nothing else watches this, so it opens a case when it fails from the start. Two fires "
    "in a row on nothing send it back to silent."
)
_VOUCHED_NOTE = (
    "A person asked for this check, so it opens a case when it fails. Two fires in a row "
    "on nothing send it back to silent."
)
_OFF = json.dumps({"error": "Detection checks are turned off on this install."})
_NO_USER_CTX = json.dumps({"error": "No user context available."})
_NO_ORG_CTX = json.dumps({"error": "No organization context available."})


class SaveDetectionCheckArgs(BaseModel):
    name: str = Field(description="Short unique name. Saving an existing name revises that check.")
    prediction: str = Field(description="One sentence: the failure this check detects.")
    asset: str = Field(
        description=(
            "The bare name of the service or resource it watches, exactly as an incident or alert "
            "would name it, e.g. 'checkout' or 'orders-db'. No namespace, cluster or description: "
            "a check is credited only when an incident names the same asset."
        )
    )
    pattern: str = Field(
        description="The kind of check, reusable across assets, e.g. 'pod-crashloop', 'table-freshness'."
    )
    provider: str = Field(
        description=(
            "Where the command runs: a cloud_exec provider (gcp, aws, azure, ...), or "
            "'onprem_kubectl' for a cluster from get_connected_clusters."
        )
    )
    target: str = Field(
        default="",
        description=(
            "The cluster_id for onprem_kubectl (required). For a cloud provider, optionally the "
            "account or subscription id to run against."
        ),
    )
    command: str = Field(
        description=(
            "A read-only command, as you would pass to cloud_exec or on_prem_kubectl, that answers "
            "once and exits (no -w, --watch or --follow). Shape its output to one number or a "
            "short text, and prefer a form that always prints something: an expected number that "
            "comes back empty is treated as a failure. Cloud providers may pipe into `grep -c`, `jq` "
            "or `wc -l`. onprem_kubectl runs with no shell: no pipes; use -o jsonpath or "
            "--no-headers with expect 'lines' or 'text'."
        )
    )
    expect: str = Field(
        description="How to read the output: 'number' (one number), 'lines' (count of non-empty lines) or 'text'."
    )
    op: str = Field(
        description=(
            "The HEALTHY condition, output <op> value. number and lines: lt, lte, gt, gte, eq, ne. "
            "text: contains, not_contains, eq, ne, empty, not_empty."
        )
    )
    # Strict: lax parsing would turn `false` into the threshold 0.0 and "300" into 300.
    value: StrictFloat | StrictInt | StrictStr | None = Field(
        default=None, description="What the output is compared with. Omit for empty / not_empty."
    )
    origin_kind: str = Field(description="Why this check exists: " + ", ".join(ORIGIN_KINDS) + ".")
    origin_ref: str = Field(
        default="", description="The incident id, or the person who asked, that this check came from."
    )
    reason: str = Field(default="", description="Why it is being created or changed.")
    unmonitored: bool = Field(
        default=False,
        description=(
            "Monitoring Coverage Scan only, with origin_kind 'graph' or 'explore': true when "
            "nothing else monitors this failure on this asset (no monitor, alarm, alert rule or "
            "other check). Such a check opens a case when it fails from the start, instead of "
            "waiting to earn it, because otherwise nobody would hear about it at all."
        ),
    )
    window_seconds: StrictInt = Field(
        default=0,
        description=(
            "For a check over logs or metrics: the span it reads, in seconds, at least "
            "interval_seconds so no time goes unread. Put {from} and {to} in the command where "
            "the start and end go: RFC 3339 UTC timestamps, or {from_epoch}/{to_epoch} for epoch "
            "seconds, or {from_epoch_ms}/{to_epoch_ms} for milliseconds (aws logs takes those). "
            "Aurora fills them in on every run. With origin_kind 'incident' and both ends, Aurora "
            "first replays the check over that incident and over earlier quiet days, and refuses "
            "it if it would not have caught the incident or fires on the quiet days. Leave 0 for "
            "a check that reads the present."
        ),
    )
    lag_seconds: StrictInt = Field(
        default=0,
        description=(
            "For a windowed check: how far behind now the window ends, to allow for the source's "
            "ingestion delay (logs and metrics land late). 0 to 3600. Use it when a check for "
            "'at least one line' would otherwise fail on data that has not arrived yet."
        ),
    )
    interval_seconds: StrictInt = Field(
        default=DEFAULT_INTERVAL_SECONDS,
        description="How often to run it. Minimum %d." % MIN_INTERVAL_SECONDS,
    )


class ListDetectionChecksArgs(BaseModel):
    include_retired: bool = Field(default=False, description="Also list retired checks.")
    needs_review: bool = Field(
        default=False,
        description=(
            "Only the checks waiting for review (demoted, or erroring on every run), each with "
            "why it is here, its recent runs and its recent fires. Asking for them takes them "
            "off the waiting list, so act on each one you are given: revise it, retire it, or "
            "leave it and say why."
        ),
    )


class RetireDetectionCheckArgs(BaseModel):
    name: str = Field(description="Name of the check to stop running.")
    reason: str = Field(description="Why it is being retired.")


@contextmanager
def _connection(user_id: str, operation: str):
    """Yield (cursor, conn, org_id) with RLS set. Raises ValueError('no_org')."""
    with db_pool.get_admin_connection() as conn, conn.cursor() as cursor:
        org_id = set_rls_context(cursor, conn, user_id, log_prefix="[DetectionTool:%s]" % operation)
        if not org_id:
            raise ValueError("no_org")
        yield cursor, conn, org_id


def _may_write(user_id: str) -> bool:
    """Whether this user may create, change or stop checks (editor and above).

    A check runs commands on a schedule with the org's access, and stopping one
    removes monitoring others may rely on. Reading memory is for everyone;
    this is not.
    """
    from utils.auth.enforcer import enforce_with_reload
    from utils.auth.stateless_auth import get_org_id_for_user

    org_id = get_org_id_for_user(user_id)
    return bool(org_id) and enforce_with_reload(user_id, org_id, "detection_checks", "write")


_NOT_ALLOWED = json.dumps({"error": "Saving or retiring checks needs the editor role."})


def _asked_by_a_person(origin: dict) -> bool:
    """Whether a person, present in this conversation, asked for the check.

    Only then does the check start out trusted. A background run (an
    investigation, a scheduled action) has nobody in it, whatever origin the
    model wrote, and an unknown context counts as background.
    """
    if origin["kind"] != "person":
        return False
    from utils.cloud.cloud_utils import get_state_context

    state = get_state_context()
    return state is not None and not getattr(state, "is_background", True)


_UNMONITORED_ORIGINS = frozenset(("graph", "explore"))


def _starts_active(args, origin: dict) -> bool:
    """Whether a check the coverage scan wrote for an unwatched asset opens cases at once.

    Aurora's own checks start silent and earn trust from their record. That
    leaves an asset nothing else watches with no alert at all for weeks, which
    is the blind spot the scan exists to close. Only the scan's own session
    may claim it, whatever the model wrote, and it can still be demoted the
    usual way (two fires in a row on nothing).
    """
    if not args.unmonitored or origin["kind"] not in _UNMONITORED_ORIGINS:
        return False
    from utils.cloud.cloud_utils import get_state_context

    state = get_state_context()
    return state is not None and getattr(state, "trigger_source", None) == COVERAGE_SCAN_SOURCE


def _parse_check(fields: dict):
    """(args, definition, origin, '') from the tool's keyword arguments, or an error as JSON last."""
    try:
        args = SaveDetectionCheckArgs(
            **{name: fields[name] for name in SaveDetectionCheckArgs.model_fields if name in fields}
        )
    except ValidationError as invalid:
        first = invalid.errors()[0]
        field = ".".join(str(part) for part in first.get("loc", ()))
        return None, None, None, json.dumps(
            {"error": "Invalid check: %s: %s" % (field, first.get("msg", "bad input"))})

    definition, problem = normalize_definition({
        "name": args.name, "prediction": args.prediction, "asset": args.asset,
        "pattern": args.pattern, "provider": args.provider, "target": args.target,
        "command": args.command, "interval_seconds": args.interval_seconds,
        "window_seconds": args.window_seconds, "lag_seconds": args.lag_seconds,
        "assertion": {"expect": args.expect, "op": args.op, "value": args.value},
    })
    if problem:
        return None, None, None, json.dumps({"error": problem})
    origin, problem = normalize_origin({"kind": args.origin_kind, "ref": args.origin_ref})
    if problem:
        return None, None, None, json.dumps({"error": problem})
    return args, definition, origin, ""


def _try_replay(user_id: str, definition: dict, origin: dict):
    """(what the replay found or None, a refusal as JSON or '')."""
    try:
        replayed = _replay_before_saving(user_id, definition, origin)
    except Exception:
        # A replay that cannot run is not a reason to refuse: the check is
        # admitted silent and proves itself forward.
        logger.exception("[DetectionTool] Replay failed to run")
        return None, ""
    if replayed is None or not replay.refusal(replayed.result):
        return replayed, ""
    return replayed, json.dumps({
        "error": replay.refusal(replayed.result), "replay": replayed.record,
        "note": "At most %d replays are run per incident per day, and a check saved after a "
                "refusal is not credited with the incident. Do not adjust a threshold to fit "
                "this one incident: change the check only if it measured the wrong thing."
                % replay.MAX_ATTEMPTS_PER_INCIDENT,
    })


def _keep_replay(cursor, org_id: str, saved: tuple, definition: dict, replayed) -> None:
    """Store what the replay found beside the revision, and credit the incident it caught."""
    check_id, revision, changed = saved
    # Kept on a changed revision, and on an unchanged one that has now caught
    # an incident: the weekly fire drill finds its checks by this record, so a
    # check proven by replay must carry it.
    if changed or replayed.result.caught:
        record_replay(cursor, check_id, revision, replayed.record)
    # Not tied to the definition changing: the same check saved for a second
    # incident has caught a second incident. credit_replay is a no-op when
    # this check was already credited with this one.
    # Not after a refusal: a check reshaped until it fails over an incident
    # has been fitted to it, which is not catching it.
    if replayed.result.caught and replayed.creditable:
        credit_replay(
            cursor, check_id, org_id, revision, definition["pattern"],
            definition["asset"], replayed.incident_id, replayed.incident_at,
        )


def _state_note(state: str, vouched: bool, active: bool) -> str:
    """Why the check notifies, or does not, in its current state."""
    if state == "proven" and vouched:
        return _VOUCHED_NOTE
    if state == "proven" and active:
        return _UNMONITORED_NOTE
    return _STATE_NOTES.get(state, "")


def _saved_response(saved: tuple, state: str, vouched: bool, replayed, active: bool = False) -> str:
    check_id, revision, changed = saved
    response = {
        "status": "saved" if changed else "unchanged",
        "check_id": check_id,
        "revision": revision,
        "state": state,
        "note": _state_note(state, vouched, active),
    }
    if replayed:
        response["replay"] = replayed.record
    if replayed and replayed.result.caught and not replayed.creditable:
        response["replay_note"] = (
            "Replayed and admitted, but not credited with the incident: an earlier version "
            "written for the same incident was refused, so this one was shaped with that "
            "result in hand. It earns its record from what it catches next."
        )
    return json.dumps(response)


def _store_check(user_id: str, args, definition: dict, origin: dict, replayed) -> str:
    """Write the check and return the tool's answer."""
    with _connection(user_id, "save") as (cursor, conn, org_id):
        # Caught inside the block: an expected refusal must not reach the
        # pool's handler, which logs any exception as a connection error.
        try:
            saved = save_check(
                cursor, org_id, user_id, definition,
                origin=origin, reason=args.reason, source="agent",
                vouched_by=user_id if _asked_by_a_person(origin) else None,
            )
        except CheckRetired as retired:
            conn.rollback()
            return json.dumps({
                "error": "A check with this name was retired and is not brought back.",
                "retired_reason": str(retired),
            })
        except TooManyChecks:
            conn.rollback()
            return json.dumps({
                "error": "This organization is at its limit of active checks. Retire one first.",
            })
        if replayed:
            _keep_replay(cursor, org_id, saved, definition, replayed)
        active = bool(saved[2] and _starts_active(args, origin))
        if active:
            start_active(cursor, org_id, saved[0])
        # Read back: an unchanged save of a proven check is still proven.
        cursor.execute(
            "SELECT state, vouched_by IS NOT NULL FROM detection_checks WHERE id = %s", (saved[0],),
        )
        state, vouched = cursor.fetchone()
        conn.commit()
    return _saved_response(saved, state, vouched, replayed, active)


def save_detection_check(user_id: str | None = None, **fields) -> str:
    """Create a check, or revise the one with the same name.

    Takes the fields of SaveDetectionCheckArgs as keyword arguments.
    """
    if not user_id:
        return _NO_USER_CTX
    if not checks_enabled():
        return _OFF
    if not _may_write(user_id):
        return _NOT_ALLOWED
    args, definition, origin, refused = _parse_check(fields)
    if refused:
        return refused

    # The full gate, LLM judge included, runs here: this is the one moment a
    # conversation exists to judge the command against. Scheduled runs then skip
    # only the judge, and only for this exact command.
    gate = gate_command(
        user_id=user_id, tool_name="save_detection_check",
        command=gated_form(
            definition["provider"],
            window.render(definition["command"], definition["window_seconds"]),
        ),
    )
    if not gate.allowed:
        return json.dumps({"error": gate.block_reason, "code": gate.code})

    replayed, refused = _try_replay(user_id, definition, origin)
    if refused:
        return refused
    try:
        return _store_check(user_id, args, definition, origin, replayed)
    except ValueError:
        return _NO_ORG_CTX
    except Exception:
        logger.exception("[DetectionTool] Failed to save check")
        return json.dumps({"error": "Failed to save the check."})


@dataclass(frozen=True)
class _Replayed:
    result: replay.ReplayResult
    record: dict
    incident_id: str
    incident_at: datetime
    # False when an earlier check written for the same incident was refused.
    creditable: bool = True


def _replay_before_saving(user_id: str, definition: dict, origin: dict) -> Optional[_Replayed]:
    """Replay a check written for an incident over that incident, or None when it cannot be.

    Runs under the gate decision just made for this command: the replayed
    commands differ from it only in the timestamps Aurora fills in.
    """
    if origin["kind"] not in ("incident", "miss"):
        return None
    if not window.is_replayable(definition["command"], definition["window_seconds"]):
        return None
    incident_id = replay.incident_id_from(origin["ref"])
    if not incident_id:
        return None
    with _connection(user_id, "replay") as (cursor, conn, org_id):
        incident_at = replay.incident_time(cursor, org_id, incident_id)
        if incident_at is None:
            return None
        # Taken before the replay runs, and counted whatever happens next: a refused
        # save is exactly the attempt to count.
        attempt_id = replay.reserve_attempt(cursor, org_id, incident_id, definition["name"])
        if attempt_id is None:
            # Out of replays for this incident: admitted silent, proves itself forward.
            return None
        end = replay.replay_end(incident_at, definition["window_seconds"])
        quiet = replay.quiet_ends(cursor, org_id, end, definition["window_seconds"])
        conn.commit()
    result = replay.replay_origin(definition, user_id, end, quiet)
    with _connection(user_id, "replay") as (cursor, conn, org_id):
        refused_before = replay.close_attempt(cursor, org_id, incident_id, attempt_id,
                                              bool(replay.refusal(result)))
        conn.commit()
    return _Replayed(result, result.as_record(incident_id, end), incident_id, incident_at,
                     creditable=not refused_before)


def list_detection_checks(
    include_retired: bool = False,
    needs_review: bool = False,
    user_id: str | None = None,
    **_kwargs,
) -> str:
    """List the org's checks with their records."""
    if not user_id:
        return _NO_USER_CTX
    if not checks_enabled():
        return _OFF
    if needs_review and not _may_write(user_id):
        # Taking checks for review removes them from the waiting list.
        return _NOT_ALLOWED
    try:
        with _connection(user_id, "list") as (cursor, conn, org_id):
            if needs_review:
                checks = take_checks_for_review(cursor, org_id)
                conn.commit()
            else:
                checks = list_checks(cursor, org_id, include_retired=include_retired)
        if not _may_write(user_id):
            # What a command printed can be more than a viewer may see elsewhere.
            for check in checks:
                check.pop("last_observed", None)
    except ValueError:
        return _NO_ORG_CTX
    except Exception:
        logger.exception("[DetectionTool] Failed to list checks")
        return json.dumps({"error": "Failed to list checks."})
    return json.dumps({"count": len(checks), "checks": checks})


def retire_detection_check(
    name: str,
    reason: str,
    user_id: str | None = None,
    **_kwargs,
) -> str:
    """Stop running a check. Its record and revisions are kept."""
    if not user_id:
        return _NO_USER_CTX
    if not checks_enabled():
        return _OFF
    if not _may_write(user_id):
        return _NOT_ALLOWED
    try:
        with _connection(user_id, "retire") as (cursor, conn, org_id):
            retired = retire_check(cursor, org_id, name, reason)
            conn.commit()
    except ValueError:
        return _NO_ORG_CTX
    except Exception:
        logger.exception("[DetectionTool] Failed to retire check")
        return json.dumps({"error": "Failed to retire the check."})
    if not retired:
        return json.dumps({"error": "No active check with that name."})
    return json.dumps({"status": "retired"})
