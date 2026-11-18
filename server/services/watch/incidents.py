"""Open (or extend) an incident for a group of Aurora Watch findings and start its RCA.

Same path as the vendor webhooks: an ``incidents`` row, a primary
``incident_alerts`` row, a lifecycle event, an SSE broadcast, the quick summary,
then the background RCA. Findings that share a primary suspect land on the same
incident (deduplicated on ``source_alert_id``), so one bad change opens one
incident however many workloads it breaks.
"""

from __future__ import annotations

import json
import logging
import time
import zlib
from datetime import datetime, timezone
from typing import Any

from services.watch import report
from services.watch.store import jsonb_dumps

logger = logging.getLogger(__name__)


def source_alert_id(cluster_id: str, group_key: str) -> int:
    return zlib.crc32(f"{cluster_id}|{group_key}".encode("utf-8")) & 0x7FFFFFFF


_SEVERITIES = ["low", "medium", "high", "critical"]


def _rank(severity: Any) -> int:
    return _SEVERITIES.index(severity) + 1 if severity in _SEVERITIES else 0


def _merge_payload(existing: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    """Fold a group's findings into an incident that is already open: a finding seen
    again (it got worse) replaces its older copy, new ones and new suspects are added."""
    if not existing:
        return new
    merged = dict(existing)
    newer = {f.get("subject"): f for f in new.get("findings") or []}
    kept = [newer.pop(f.get("subject"), f) for f in existing.get("findings") or []]
    merged["findings"] = (kept + list(newer.values()))[: report.MAX_FINDINGS]

    def ident(s: dict[str, Any]) -> tuple:
        return s.get("object"), s.get("at")

    known = {ident(s) for s in existing.get("suspected_changes") or []}
    merged["suspected_changes"] = ((existing.get("suspected_changes") or []) + [
        s for s in new.get("suspected_changes") or [] if ident(s) not in known])[: report.MAX_SUSPECTS]
    if _rank(new.get("severity")) > _rank(existing.get("severity")):
        merged["severity"] = new["severity"]
    merged["updated_at"] = new.get("first_seen")
    return merged


def _extend(cur, incident_id: Any, alert_metadata: Any, payload: dict[str, Any], services: list[str]) -> None:
    """Add a group's findings, and the workloads they name, to an open incident, and
    raise its severity if the group's is higher."""
    metadata = alert_metadata if isinstance(alert_metadata, dict) else json.loads(alert_metadata or "{}")
    metadata["aurora_watch"] = _merge_payload(metadata.get("aurora_watch"), payload)
    cur.execute(
        """
        UPDATE incidents
           SET alert_metadata = %s,
               severity = CASE WHEN array_position(%s::text[], %s::text)
                                    > COALESCE(array_position(%s::text[], severity::text), 0)
                               THEN %s ELSE severity END,
               affected_services = ARRAY(SELECT DISTINCT s
                                           FROM unnest(COALESCE(affected_services, '{}') || %s::text[]) AS s
                                          ORDER BY s),
               updated_at = CURRENT_TIMESTAMP
         WHERE id = %s
        """,
        (jsonb_dumps(metadata), _SEVERITIES, payload.get("severity"), _SEVERITIES, payload.get("severity"),
         services, incident_id),
    )


def open_incident(user_id: str, org_id: str, cluster: dict[str, Any], group: dict[str, Any]) -> dict[str, Any]:
    """Create the incident (or add findings to the open one) and dispatch the RCA for a new one."""
    from utils.auth.stateless_auth import set_rls_context
    from utils.db.connection_pool import db_pool

    title = report.incident_title(group)
    service = report.incident_service(group)
    severity = group["severity"] if group["severity"] in ("critical", "high", "medium", "low") else "high"
    payload = report.rca_payload(group, cluster)
    services = sorted({f["name"] for f in group["findings"]})
    group_id = source_alert_id(cluster.get("cluster_id") or "", group["key"])
    alert_id = group_id
    now = datetime.now(timezone.utc).replace(tzinfo=None)  # incidents timestamps are naive UTC

    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        set_rls_context(cur, conn, user_id, log_prefix="[AuroraWatch:incident]")
        # The group's latest incident, whichever user Watch acted as when it opened it
        # (that may change). A recurrence has its own source_alert_id, so match the
        # group id it carries too.
        cur.execute(
            """
            SELECT id, alert_metadata, status FROM incidents
             WHERE org_id = %s AND source_type = %s
               AND (source_alert_id = %s OR alert_metadata->>'watch_group' = %s)
             ORDER BY started_at DESC LIMIT 1
            """,
            (org_id, report.SOURCE_TYPE, group_id, str(group_id)),
        )
        existing = cur.fetchone()
        if existing and existing[2] not in ("resolved", "merged"):
            _extend(cur, existing[0], existing[1], payload, services)
            conn.commit()
            logger.info("[AuroraWatch] added findings to incident %s", existing[0])
            return {"incident_id": str(existing[0]), "status": "updated"}
        if existing:
            # The same change broke something again after its incident was resolved:
            # a new occurrence, not a reopened incident with the old title and timestamps.
            alert_id = source_alert_id(cluster.get("cluster_id") or "", f"{group['key']}|{int(time.time())}")

        metadata = {"aurora_watch": payload, "cluster": payload["cluster"], "namespace": group["namespace"],
                    "watch_group": str(group_id)}
        cur.execute(
            """
            INSERT INTO incidents
              (user_id, org_id, source_type, source_alert_id, alert_title, alert_service,
               severity, status, started_at, alert_metadata, alert_fired_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'investigating', %s, %s, %s)
            ON CONFLICT (org_id, source_type, source_alert_id, user_id) DO NOTHING
            RETURNING id
            """,
            (user_id, org_id, report.SOURCE_TYPE, alert_id, title, service, severity, now,
             jsonb_dumps(metadata), now),
        )
        row = cur.fetchone()
        if row is None:  # a concurrent poll inserted it first
            cur.execute(
                "SELECT id, alert_metadata FROM incidents WHERE org_id = %s AND source_type = %s "
                "AND source_alert_id = %s AND user_id = %s",
                (org_id, report.SOURCE_TYPE, alert_id, user_id),
            )
            raced = cur.fetchone()
            _extend(cur, raced[0], raced[1], payload, services)
            conn.commit()
            return {"incident_id": str(raced[0]), "status": "updated"}
        incident_id = str(row[0])
        conn.commit()
        for sql, params in (
            ("""INSERT INTO incident_lifecycle_events (incident_id, user_id, org_id, event_type, new_value)
                VALUES (%s, %s, %s, 'created', 'investigating')""", (incident_id, user_id, org_id)),
            ("""INSERT INTO incident_alerts
                  (user_id, org_id, incident_id, source_type, source_alert_id, alert_title, alert_service,
                   alert_severity, correlation_strategy, correlation_score, alert_metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'primary', 1.0, %s)""",
             (user_id, org_id, incident_id, report.SOURCE_TYPE, alert_id, title, service, severity,
              jsonb_dumps(metadata))),
            ("UPDATE incidents SET affected_services = %s WHERE id = %s", (services, incident_id)),
        ):
            try:
                cur.execute("SAVEPOINT sp_watch")
                cur.execute(sql, params)
                cur.execute("RELEASE SAVEPOINT sp_watch")
                conn.commit()
            except Exception as exc:
                cur.execute("ROLLBACK TO SAVEPOINT sp_watch")
                logger.warning("[AuroraWatch] incident %s bookkeeping failed: %s", incident_id, type(exc).__name__)

    logger.info("[AuroraWatch] opened incident %s for org %s: %s", incident_id, org_id, title)
    _after_open(user_id, org_id, incident_id, title, service, severity, payload, group)
    return {"incident_id": incident_id, "status": "opened"}


def _after_open(user_id: str, org_id: str, incident_id: str, title: str, service: str, severity: str,
                payload: dict[str, Any], group: dict[str, Any]) -> None:
    """SSE broadcast, quick summary, and the RCA (unless the user's RCA budget is spent)."""
    try:
        from routes.incidents_sse import broadcast_incident_update_to_user_connections
        broadcast_incident_update_to_user_connections(
            user_id, {"type": "incident_update", "incident_id": incident_id, "source": report.SOURCE_TYPE},
            org_id=org_id,
        )
    except Exception as exc:
        logger.warning("[AuroraWatch] SSE broadcast failed: %s", type(exc).__name__)

    try:
        from chat.background.summarization import generate_incident_summary
        generate_incident_summary.delay(
            incident_id=incident_id, user_id=user_id, source_type=report.SOURCE_TYPE,
            alert_title=title, severity=severity, service=service,
            raw_payload=payload, alert_metadata={"aurora_watch": payload},
        )
    except Exception as exc:
        logger.warning("[AuroraWatch] summary enqueue failed: %s", type(exc).__name__)

    try:
        from chat.background.rca_prompt_builder import build_rca_prompt
        from chat.background.task import (
            create_background_chat_session,
            is_background_chat_allowed,
            run_background_chat,
        )
        if not is_background_chat_allowed(user_id):
            logger.info("[AuroraWatch] RCA for incident %s skipped: background RCA budget spent", incident_id)
            return
        prompt, _ = build_rca_prompt(report.SOURCE_TYPE, title, payload, user_id=user_id)
        trigger = {"source": report.SOURCE_TYPE, "incident_id": incident_id,
                   "cluster_id": payload["cluster"].get("id"), "group_key": group["key"]}
        session_id = create_background_chat_session(
            user_id=user_id, title=f"RCA: {title}", trigger_metadata=trigger, incident_id=incident_id,
        )
        task = run_background_chat.delay(
            user_id=user_id, session_id=session_id, initial_message=prompt,
            trigger_metadata=trigger, incident_id=incident_id,
            rail_text=report.rail_text(payload),
        )
        from utils.auth.stateless_auth import set_rls_context
        from utils.db.connection_pool import db_pool
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            set_rls_context(cur, conn, user_id, log_prefix="[AuroraWatch:rca_task]")
            cur.execute("UPDATE incidents SET rca_celery_task_id = %s WHERE id = %s", (task.id, incident_id))
            conn.commit()
    except Exception:
        logger.exception("[AuroraWatch] RCA dispatch failed for incident %s", incident_id)
