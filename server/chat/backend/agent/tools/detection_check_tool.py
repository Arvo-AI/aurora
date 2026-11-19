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

from services.detection import DEFAULT_INTERVAL_SECONDS, MIN_INTERVAL_SECONDS, ORIGIN_KINDS
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
)
from utils.auth.command_gate import gate_command
from utils.auth.stateless_auth import set_rls_context
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

_STATE_NOTES = {
    "silent": "It runs on its schedule and records results. A silent check notifies no one.",
    "proven": "It runs on its schedule. If it starts failing it opens a case.",
}
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
    window_seconds: StrictInt = Field(
        default=0,
        description=(
            "For a check over logs or metrics: the span it reads, in seconds. Put {from} and {to} "
            "in the command where the start and end timestamps go (RFC 3339, UTC). Aurora fills "
            "them in on every run. With origin_kind 'incident' and both placeholders, Aurora "
            "first replays the check over that incident and refuses it if it would not have "
            "caught it. Leave 0 for a check that reads the present."
        ),
    )
    interval_seconds: StrictInt = Field(
        default=DEFAULT_INTERVAL_SECONDS,
        description="How often to run it. Minimum %d." % MIN_INTERVAL_SECONDS,
    )


class ListDetectionChecksArgs(BaseModel):
    include_retired: bool = Field(default=False, description="Also list retired checks.")


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


def save_detection_check(user_id: str | None = None, **fields) -> str:
    """Create a check, or revise the one with the same name.

    Takes the fields of SaveDetectionCheckArgs as keyword arguments.
    """
    if not user_id:
        return _NO_USER_CTX
    if not _may_write(user_id):
        return _NOT_ALLOWED
    try:
        args = SaveDetectionCheckArgs(
            **{name: fields[name] for name in SaveDetectionCheckArgs.model_fields if name in fields}
        )
    except ValidationError as invalid:
        first = invalid.errors()[0]
        field = ".".join(str(part) for part in first.get("loc", ()))
        return json.dumps({"error": "Invalid check: %s: %s" % (field, first.get("msg", "bad input"))})

    definition, problem = normalize_definition({
        "name": args.name, "prediction": args.prediction, "asset": args.asset,
        "pattern": args.pattern, "provider": args.provider, "target": args.target,
        "command": args.command, "interval_seconds": args.interval_seconds,
        "window_seconds": args.window_seconds,
        "assertion": {"expect": args.expect, "op": args.op, "value": args.value},
    })
    if problem:
        return json.dumps({"error": problem})
    origin, problem = normalize_origin({"kind": args.origin_kind, "ref": args.origin_ref})
    if problem:
        return json.dumps({"error": problem})

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

    try:
        replayed = _replay_before_saving(user_id, definition, origin)
    except Exception:
        # A replay that cannot run is not a reason to refuse: the check is
        # admitted silent and proves itself forward.
        logger.exception("[DetectionTool] Replay failed to run")
        replayed = None
    if replayed and replay.refusal(replayed.result):
        return json.dumps({"error": replay.refusal(replayed.result), "replay": replayed.record})

    try:
        with _connection(user_id, "save") as (cursor, conn, org_id):
            # Caught inside the block: an expected refusal must not reach the
            # pool's handler, which logs any exception as a connection error.
            try:
                check_id, revision, changed = save_check(
                    cursor, org_id, user_id, definition,
                    origin=origin, reason=args.reason, source="agent",
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
                if changed:
                    record_replay(cursor, check_id, revision, replayed.record)
                # Not tied to the definition changing: the same check saved for a
                # second incident has caught a second incident. credit_replay is a
                # no-op when this check was already credited with this one.
                if replayed.result.caught:
                    credit_replay(
                        cursor, check_id, org_id, revision, definition["pattern"],
                        definition["asset"], replayed.incident_id, replayed.incident_at,
                    )
            # Read back: an unchanged save of a proven check is still proven.
            cursor.execute("SELECT state FROM detection_checks WHERE id = %s", (check_id,))
            state = cursor.fetchone()[0]
            conn.commit()
    except ValueError:
        return _NO_ORG_CTX
    except Exception:
        logger.exception("[DetectionTool] Failed to save check")
        return json.dumps({"error": "Failed to save the check."})

    response = {
        "status": "saved" if changed else "unchanged",
        "check_id": check_id,
        "revision": revision,
        "state": state,
        "note": _STATE_NOTES.get(state, ""),
    }
    if replayed:
        response["replay"] = replayed.record
    return json.dumps(response)


@dataclass(frozen=True)
class _Replayed:
    result: replay.ReplayResult
    record: dict
    incident_id: str
    incident_at: datetime


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
    with _connection(user_id, "replay") as (cursor, _conn, org_id):
        incident_at = replay.incident_time(cursor, org_id, incident_id)
        if incident_at is None:
            return None
        end = replay.replay_end(incident_at, definition["window_seconds"])
        quiet = replay.quiet_ends(cursor, org_id, end)
    result = replay.replay_origin(definition, user_id, end, quiet)
    return _Replayed(result, result.as_record(incident_id, end), incident_id, incident_at)


def list_detection_checks(
    include_retired: bool = False,
    user_id: str | None = None,
    **_kwargs,
) -> str:
    """List the org's checks with their records."""
    if not user_id:
        return _NO_USER_CTX
    try:
        with _connection(user_id, "list") as (cursor, _conn, org_id):
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
