"""Agent tools for detection memory: save, list and retire checks.

A check is a read-only command plus an assertion on its output. Aurora runs it
on a schedule with no model (services/detection/runner.py). Saving one changes
nothing in the customer's systems and notifies no one.
"""

import json
import logging
from contextlib import contextmanager
from typing import Optional, Union

from pydantic import BaseModel, Field

from services.detection import DEFAULT_INTERVAL_SECONDS, MIN_INTERVAL_SECONDS, ORIGIN_KINDS
from services.detection.execution import gated_form
from services.detection.store import (
    CheckRetired,
    list_checks,
    normalize_definition,
    normalize_origin,
    retire_check,
    save_check,
)
from utils.auth.command_gate import gate_command
from utils.auth.stateless_auth import set_rls_context
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

_NO_USER_CTX = json.dumps({"error": "No user context available."})
_NO_ORG_CTX = json.dumps({"error": "No organization context available."})


class SaveDetectionCheckArgs(BaseModel):
    name: str = Field(description="Short unique name. Saving an existing name revises that check.")
    prediction: str = Field(description="One sentence: the failure this check detects.")
    asset: str = Field(description="The service, table, queue or resource it watches.")
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
            "A read-only command, as you would pass to cloud_exec or on_prem_kubectl. Shape its "
            "output to one number or a short text. Cloud providers may pipe into `grep -c`, `jq` "
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
    value: Optional[Union[float, str]] = Field(
        default=None, description="What the output is compared with. Omit for empty / not_empty."
    )
    origin_kind: str = Field(description="Why this check exists: " + ", ".join(ORIGIN_KINDS) + ".")
    origin_ref: str = Field(
        default="", description="The incident id, or the person who asked, that this check came from."
    )
    reason: str = Field(default="", description="Why it is being created or changed.")
    interval_seconds: int = Field(
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
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            org_id = set_rls_context(cursor, conn, user_id, log_prefix="[DetectionTool:%s]" % operation)
            if not org_id:
                raise ValueError("no_org")
            yield cursor, conn, org_id


def save_detection_check(
    name: str,
    prediction: str,
    asset: str,
    pattern: str,
    provider: str,
    command: str,
    expect: str,
    op: str,
    origin_kind: str,
    value: Optional[Union[float, str]] = None,
    target: str = "",
    origin_ref: str = "",
    reason: str = "",
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    user_id: Optional[str] = None,
    **_kwargs,
) -> str:
    """Create a check, or revise the one with the same name."""
    if not user_id:
        return _NO_USER_CTX

    definition, problem = normalize_definition({
        "name": name, "prediction": prediction, "asset": asset, "pattern": pattern,
        "provider": provider, "target": target, "command": command,
        "interval_seconds": interval_seconds,
        "assertion": {"expect": expect, "op": op, "value": value},
    })
    if problem:
        return json.dumps({"error": problem})
    origin, problem = normalize_origin({"kind": origin_kind, "ref": origin_ref})
    if problem:
        return json.dumps({"error": problem})

    # The full gate, LLM judge included, runs here: this is the one moment a
    # conversation exists to judge the command against. Scheduled runs then skip
    # only the judge, and only for this exact command.
    gate = gate_command(
        user_id=user_id, tool_name="save_detection_check",
        command=gated_form(definition["provider"], definition["command"]),
    )
    if not gate.allowed:
        return json.dumps({"error": gate.block_reason, "code": gate.code})

    try:
        with _connection(user_id, "save") as (cursor, conn, org_id):
            # Caught inside the block: an expected refusal must not reach the
            # pool's handler, which logs any exception as a connection error.
            try:
                check_id, revision, changed = save_check(
                    cursor, org_id, user_id, definition,
                    origin=origin, reason=reason, source="agent",
                )
            except CheckRetired as retired:
                conn.rollback()
                return json.dumps({
                    "error": "A check with this name was retired and is not brought back.",
                    "retired_reason": str(retired),
                })
            conn.commit()
    except ValueError:
        return _NO_ORG_CTX
    except Exception:
        logger.exception("[DetectionTool] Failed to save check")
        return json.dumps({"error": "Failed to save the check."})

    return json.dumps({
        "status": "saved" if changed else "unchanged",
        "check_id": check_id,
        "revision": revision,
        "state": "silent",
        "note": "It runs on its schedule and records results. A silent check notifies no one.",
    })


def list_detection_checks(
    include_retired: bool = False,
    user_id: Optional[str] = None,
    **_kwargs,
) -> str:
    """List the org's checks with their records."""
    if not user_id:
        return _NO_USER_CTX
    try:
        with _connection(user_id, "list") as (cursor, _conn, org_id):
            checks = list_checks(cursor, org_id, include_retired=include_retired)
    except ValueError:
        return _NO_ORG_CTX
    except Exception:
        logger.exception("[DetectionTool] Failed to list checks")
        return json.dumps({"error": "Failed to list checks."})
    return json.dumps({"count": len(checks), "checks": checks})


def retire_detection_check(
    name: str,
    reason: str,
    user_id: Optional[str] = None,
    **_kwargs,
) -> str:
    """Stop running a check. Its record and revisions are kept."""
    if not user_id:
        return _NO_USER_CTX
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
