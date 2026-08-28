"""Fold logic for root-cause recurrence detection (dedup layer 1).

Pure psycopg2 — no agent imports. The correlation agent (recurrence_agent.py)
decides *whether* an incident is a recurrence; this module owns the pointer
write (``incidents.recurrence_of_incident_id``) and the verdict audit row.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from services.correlation.alert_correlator import bump_incident_alert_stats
from services.correlation.recurrence_config import (
    GROUP_IDLE_HOURS,
    REJECT_ALREADY_FOLDED,
    REJECT_CONTENTION,
    REJECT_ERROR,
    REJECT_INVALID_ID,
    REJECT_MERGED_CHILD,
    REJECT_MERGED_TARGET,
    REJECT_MUTUAL_FOLD_LOST,
    REJECT_SELF_REFERENCE,
    REJECT_STALE_GROUP,
)
from utils.auth.stateless_auth import set_rls_context
from utils.db.connection_pool import db_pool
from utils.validation import is_valid_uuid

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[RECURRENCE]"

# Root-resolution retries when the target root is itself folded concurrently.
_RESOLVE_ATTEMPTS = 3


class RecurrenceVerdict(BaseModel):
    """Structured verdict returned by the correlation agent's terminating tool."""

    recurrence_of: Optional[str] = Field(
        default=None,
        description=(
            "Incident id this incident is a recurrence of, or null if it is new. "
            "Must be an id actually seen in tool output."
        ),
    )
    reasoning: str = Field(
        description="One short paragraph: the specific evidence for the decision."
    )


@dataclass
class FoldResult:
    folded: bool
    reject_reason: Optional[str] = None
    root_id: Optional[str] = None


def _advisory_lock_key(incident_id: str) -> int:
    return int.from_bytes(
        hashlib.sha256(str(incident_id).encode()).digest()[:7],
        byteorder="big",
        signed=False,
    ) & 0x7FFFFFFFFFFFFFFF  # pg_advisory_xact_lock takes a bigint


def acquire_pair_locks(cursor, id_a: str, id_b: str) -> None:
    """Take pg_advisory_xact_lock on both ids, in sorted-key order.

    A single root-side lock cannot serialize mutual folds (B->A and A->B lock
    different keys); the sorted pair makes both transactions contend on the
    same first key.
    """
    for key in sorted({_advisory_lock_key(id_a), _advisory_lock_key(id_b)}):
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", (key,))


def insert_verdict(
    cursor,
    *,
    incident_id: str,
    user_id: str,
    org_id: Optional[str],
    decision_point: str,
    mode: str,
    claimed_recurrence_of: Optional[str],
    accepted_recurrence_of: Optional[str],
    reasoning: Optional[str],
    correlator_score: Optional[float],
    folded: bool,
    reject_reason: Optional[str],
    elapsed_ms: Optional[int],
    model: Optional[str],
) -> None:
    """Insert one verdict row on an open cursor. ON CONFLICT DO NOTHING keeps
    Celery retries idempotent (unique on incident_id + decision_point)."""
    cursor.execute(
        """INSERT INTO recurrence_verdicts
           (incident_id, user_id, org_id, decision_point, mode,
            claimed_recurrence_of, accepted_recurrence_of, reasoning,
            correlator_score, folded, reject_reason, elapsed_ms, model)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (incident_id, decision_point) DO NOTHING""",
        (
            incident_id,
            user_id,
            org_id,
            decision_point,
            mode,
            claimed_recurrence_of,
            accepted_recurrence_of,
            reasoning,
            correlator_score,
            folded,
            reject_reason,
            elapsed_ms,
            model,
        ),
    )


def persist_verdict(user_id: str, **kwargs: Any) -> bool:
    """Insert a verdict row on its own connection (shadow mode / reject paths).

    Never raises; returns False when the row could not be written.
    """
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                org_id = set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX)
                if not org_id:
                    logger.warning(
                        "%s[RLS-MISS] No org for user %s; verdict row for incident %s dropped",
                        _LOG_PREFIX, user_id, kwargs.get("incident_id"),
                    )
                    return False
                insert_verdict(cursor, user_id=user_id, org_id=org_id, **kwargs)
                conn.commit()
                return True
    except Exception:
        logger.exception(
            "%s Failed to persist verdict for incident %s",
            _LOG_PREFIX, kwargs.get("incident_id"),
        )
        return False


def get_existing_verdict(
    incident_id: str, user_id: str, decision_point: str = "after"
) -> Optional[Dict[str, Any]]:
    """Return the existing verdict row for (incident, decision_point), if any.

    Used to make Celery task retries idempotent: a re-run sees the row and
    skips the whole check (no double fold, no double token spend).
    """
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                org_id = set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX)
                if not org_id:
                    logger.warning(
                        "%s[RLS-MISS] No org for user %s; verdict lookup for %s skipped",
                        _LOG_PREFIX, user_id, incident_id,
                    )
                    return None
                cursor.execute(
                    """SELECT folded, reject_reason, accepted_recurrence_of, mode
                       FROM recurrence_verdicts
                       WHERE incident_id = %s AND decision_point = %s""",
                    (incident_id, decision_point),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                return {
                    "folded": row[0],
                    "reject_reason": row[1],
                    "accepted_recurrence_of": str(row[2]) if row[2] else None,
                    "mode": row[3],
                }
    except Exception:
        logger.exception(
            "%s Failed to look up existing verdict for incident %s",
            _LOG_PREFIX, incident_id,
        )
        return None


def _fetch_incident(cursor, incident_id: str) -> Optional[Dict[str, Any]]:
    cursor.execute(
        """SELECT id, status, recurrence_of_incident_id, started_at
           FROM incidents WHERE id = %s""",
        (incident_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return {
        "id": str(row[0]),
        "status": row[1],
        "recurrence_of": str(row[2]) if row[2] else None,
        "started_at": row[3],
    }


def fold_incident(
    *,
    incident_id: str,
    user_id: str,
    claimed_recurrence_of: str,
    reasoning: str,
    mode: str,
    decision_point: str = "after",
    correlator_score: Optional[float] = None,
    elapsed_ms: Optional[int] = None,
    model: Optional[str] = None,
) -> FoldResult:
    """Fold *incident_id* into the group anchored by *claimed_recurrence_of*.

    Live mode only. Validates the claim server-side, resolves the group root
    (pointer depth is always 1), serializes concurrent folds with a sorted
    pair of advisory locks, enforces the 24h group-idle window, then writes
    pointer + anchor alert row + verdict + lifecycle event in one transaction.

    Never raises. Every reject path still persists the verdict row so each
    completed check yields exactly one row.
    """
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                org_id = set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX)
                if not org_id:
                    # Without org RLS every query on incidents silently returns
                    # 0 rows — the doc's called-out silent-failure risk. Reject
                    # loudly and grep-ably; the verdict row cannot be written
                    # either (RLS would drop the insert).
                    logger.warning(
                        "%s[RLS-MISS] No org for user %s; cannot fold incident %s",
                        _LOG_PREFIX, user_id, incident_id,
                    )
                    return FoldResult(folded=False, reject_reason=REJECT_ERROR)
                return _fold_with_rls(
                    cursor,
                    conn,
                    incident_id=incident_id,
                    user_id=user_id,
                    org_id=org_id,
                    claimed_recurrence_of=claimed_recurrence_of,
                    reasoning=reasoning,
                    mode=mode,
                    decision_point=decision_point,
                    correlator_score=correlator_score,
                    elapsed_ms=elapsed_ms,
                    model=model,
                )
    except Exception:
        logger.exception(
            "%s Fold failed for incident %s; degrading to standalone incident",
            _LOG_PREFIX, incident_id,
        )
        # Best-effort verdict row on a fresh connection so the
        # (incident_id, decision_point) idempotency key exists — otherwise a
        # Celery retry of the summarization task re-runs the whole agent and
        # re-attempts the fold.
        persist_verdict(
            user_id,
            incident_id=incident_id,
            decision_point=decision_point,
            mode=mode,
            claimed_recurrence_of=claimed_recurrence_of,
            accepted_recurrence_of=None,
            reasoning=reasoning,
            correlator_score=correlator_score,
            folded=False,
            reject_reason=REJECT_ERROR,
            elapsed_ms=elapsed_ms,
            model=model,
        )
        return FoldResult(folded=False, reject_reason=REJECT_ERROR)


def _fold_with_rls(
    cursor,
    conn,
    *,
    incident_id: str,
    user_id: str,
    org_id: str,
    claimed_recurrence_of: str,
    reasoning: str,
    mode: str,
    decision_point: str,
    correlator_score: Optional[float],
    elapsed_ms: Optional[int],
    model: Optional[str],
) -> FoldResult:
    def _reject(reason: str, root_id: Optional[str] = None) -> FoldResult:
        # Discard any uncommitted fold writes (e.g. the mutual-fold pointer
        # NULLing) so a reject never commits partial state. Advisory locks
        # release with the rollback; nothing below needs them.
        try:
            conn.rollback()
        except Exception:
            logger.exception("%s Rollback failed during reject", _LOG_PREFIX)
        try:
            insert_verdict(
                cursor,
                incident_id=incident_id,
                user_id=user_id,
                org_id=org_id,
                decision_point=decision_point,
                mode=mode,
                claimed_recurrence_of=claimed_recurrence_of,
                accepted_recurrence_of=None,
                reasoning=reasoning,
                correlator_score=correlator_score,
                folded=False,
                reject_reason=reason,
                elapsed_ms=elapsed_ms,
                model=model,
            )
            conn.commit()
        except Exception:
            logger.exception(
                "%s Failed to record reject verdict (%s) for incident %s",
                _LOG_PREFIX, reason, incident_id,
            )
            conn.rollback()
        logger.info(
            "%s Fold rejected for incident %s -> %s: %s",
            _LOG_PREFIX, incident_id, claimed_recurrence_of, reason,
        )
        return FoldResult(folded=False, reject_reason=reason, root_id=root_id)

    # --- 1. Validate + resolve root, 2. lock pair, 3. re-read inside lock ---
    root_id = claimed_recurrence_of
    root = None
    for _ in range(_RESOLVE_ATTEMPTS):
        if not is_valid_uuid(root_id):
            return _reject(REJECT_INVALID_ID)
        root = _fetch_incident(cursor, root_id)
        if root is None:
            return _reject(REJECT_INVALID_ID)
        if root["status"] == "merged":
            return _reject(REJECT_MERGED_TARGET)
        if root["recurrence_of"]:
            # Follow the pointer once — folds only ever target roots, so
            # pointer depth is always 1.
            root_id = root["recurrence_of"]
            root = _fetch_incident(cursor, root_id)
            if root is None:
                return _reject(REJECT_INVALID_ID)
            if root["status"] == "merged":
                return _reject(REJECT_MERGED_TARGET)
        if root["id"] == str(incident_id):
            return _reject(REJECT_SELF_REFERENCE)

        acquire_pair_locks(cursor, str(incident_id), root["id"])

        child = _fetch_incident(cursor, incident_id)
        if child is None:
            return _reject(REJECT_INVALID_ID)
        if child["recurrence_of"]:
            return _reject(REJECT_ALREADY_FOLDED, root_id=child["recurrence_of"])
        if child["status"] == "merged":
            # Guard carried over from the removed manual-merge route: a child
            # already merged elsewhere must not also join a recurrence group
            # (two contradictory parents).
            return _reject(REJECT_MERGED_CHILD)
        root = _fetch_incident(cursor, root["id"])
        if root is None:
            return _reject(REJECT_INVALID_ID)
        if root["status"] == "merged":
            return _reject(REJECT_MERGED_TARGET)

        if root["recurrence_of"] == str(incident_id):
            # Mutual fold: a concurrent check folded the root onto us.
            # Tie-break on earlier started_at — the older incident anchors.
            if (
                root["started_at"] is not None
                and child["started_at"] is not None
                and root["started_at"] <= child["started_at"]
            ):
                cursor.execute(
                    """UPDATE incidents
                       SET recurrence_of_incident_id = NULL
                       WHERE id = %s AND recurrence_of_incident_id = %s""",
                    (root["id"], incident_id),
                )
                # Correct the losing fold's audit rows so recurrence_verdicts
                # and lifecycle events do not permanently contradict the
                # pointer we are about to rewrite.
                cursor.execute(
                    """UPDATE recurrence_verdicts
                       SET folded = FALSE, reject_reason = %s
                       WHERE incident_id = %s AND folded = TRUE
                         AND accepted_recurrence_of = %s""",
                    (REJECT_MUTUAL_FOLD_LOST, root["id"], incident_id),
                )
                cursor.execute(
                    """INSERT INTO incident_lifecycle_events
                       (incident_id, user_id, org_id, event_type, new_value, metadata)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (
                        root["id"],
                        user_id,
                        org_id,
                        "recurrence_unfolded",
                        str(incident_id),
                        json.dumps({"reason": "mutual_fold_tie_break"}),
                    ),
                )
                # Cosmetic artifact accepted (see plan): the losing fold's
                # 'recurrence' alert row stays on the rewritten anchor; the
                # pointer is authoritative for layers 2/3.
                logger.warning(
                    "%s Mutual fold detected: unfolded root %s from child %s "
                    "(root started earlier); its 'recurrence' alert row on %s is stale",
                    _LOG_PREFIX, root["id"], incident_id, incident_id,
                )
                break
            return _reject(REJECT_MUTUAL_FOLD_LOST, root_id=root["id"])
        if root["recurrence_of"]:
            # Root was folded elsewhere while we resolved — chase the new root.
            root_id = root["recurrence_of"]
            continue
        break
    else:
        return _reject(REJECT_CONTENTION)

    root_id = root["id"]

    # --- 4. Eligibility: group idle window (status deliberately ignored) ---
    cursor.execute(
        """SELECT MAX(COALESCE(alert_fired_at, started_at))
                  < NOW() - make_interval(hours => %s)
           FROM incidents
           WHERE id = %s OR recurrence_of_incident_id = %s""",
        (GROUP_IDLE_HOURS, root_id, root_id),
    )
    row = cursor.fetchone()
    if row and row[0] is True:
        return _reject(REJECT_STALE_GROUP, root_id=root_id)

    # --- 5. Writes, one transaction ---
    # (a) pointer, rowcount-guarded against a concurrent fold
    cursor.execute(
        """UPDATE incidents
           SET recurrence_of_incident_id = %s, updated_at = CURRENT_TIMESTAMP
           WHERE id = %s AND recurrence_of_incident_id IS NULL""",
        (root_id, incident_id),
    )
    if cursor.rowcount != 1:
        return _reject(REJECT_CONTENTION, root_id=root_id)

    # (b) copy the child's primary alert onto the anchor as a 'recurrence' row.
    # incident_alerts has no unique constraint, so guard with an existence check.
    cursor.execute(
        """SELECT source_type, source_alert_id, alert_title, alert_service,
                  alert_severity, alert_metadata
           FROM incident_alerts
           WHERE incident_id = %s AND correlation_strategy = 'primary'
           LIMIT 1""",
        (incident_id,),
    )
    primary = cursor.fetchone()
    alert_service = None
    alert_row_inserted = False
    if primary:
        alert_service = primary[3]
        cursor.execute(
            """SELECT 1 FROM incident_alerts
               WHERE incident_id = %s AND correlation_strategy = 'recurrence'
                 AND correlation_details->>'source_incident_id' = %s
               LIMIT 1""",
            (root_id, str(incident_id)),
        )
        if cursor.fetchone() is None:
            alert_row_inserted = True
            cursor.execute(
                """INSERT INTO incident_alerts
                   (user_id, org_id, incident_id, source_type, source_alert_id,
                    alert_title, alert_service, alert_severity,
                    correlation_strategy, correlation_score,
                    correlation_details, alert_metadata)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    user_id,
                    org_id,
                    root_id,
                    primary[0],
                    primary[1],
                    primary[2],
                    alert_service,
                    primary[4],
                    "recurrence",
                    correlator_score,
                    json.dumps(
                        {"reasoning": reasoning, "source_incident_id": str(incident_id)}
                    ),
                    json.dumps(primary[5]) if primary[5] else "{}",
                ),
            )
    else:
        logger.warning(
            "%s Child %s has no primary alert row; anchor %s gets no recurrence alert row",
            _LOG_PREFIX, incident_id, root_id,
        )

    # (c) anchor stats (NULL-guarded, shared with handle_correlated_alert).
    # Bump only when an alert row was actually added so correlated_alert_count
    # stays in sync with the anchor's alerts list — chat-triggered children
    # have no primary alert row and must not inflate the count.
    if alert_row_inserted:
        bump_incident_alert_stats(cursor, root_id, alert_service)

    # (d) verdict row
    insert_verdict(
        cursor,
        incident_id=incident_id,
        user_id=user_id,
        org_id=org_id,
        decision_point=decision_point,
        mode=mode,
        claimed_recurrence_of=claimed_recurrence_of,
        accepted_recurrence_of=root_id,
        reasoning=reasoning,
        correlator_score=correlator_score,
        folded=True,
        reject_reason=None,
        elapsed_ms=elapsed_ms,
        model=model,
    )

    # (e) child lifecycle event
    cursor.execute(
        """INSERT INTO incident_lifecycle_events
           (incident_id, user_id, org_id, event_type, new_value, metadata)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (
            incident_id,
            user_id,
            org_id,
            "recurrence_folded",
            root_id,
            json.dumps({"recurrence_of_incident_id": root_id}),
        ),
    )

    conn.commit()  # advisory locks auto-release here

    logger.info(
        "%s Folded incident %s into %s (mode=%s)",
        _LOG_PREFIX, incident_id, root_id, mode,
    )

    # --- 6. Post-commit, non-fatal ---
    try:
        from routes.incidents_sse import broadcast_incident_update_to_user_connections

        broadcast_incident_update_to_user_connections(
            user_id,
            {
                "type": "recurrence_folded",
                "incident_id": str(incident_id),
                "recurrence_of_incident_id": str(root_id),
            },
            org_id=org_id,
        )
    except Exception as e:
        logger.warning("%s Failed to broadcast recurrence_folded SSE: %s", _LOG_PREFIX, e)

    return FoldResult(folded=True, root_id=root_id)
