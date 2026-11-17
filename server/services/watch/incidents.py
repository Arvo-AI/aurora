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

logger = logging.getLogger(__name__)


def source_alert_id(cluster_id: str, group_key: str) -> int:
    return zlib.crc32(f"{cluster_id}|{group_key}".encode("utf-8")) & 0x7FFFFFFF


def _merge_payload(existing: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    """Add newly confirmed findings to an incident that is already open."""
    if not existing:
        return new
    merged = dict(existing)
    known = {f.get("subject") for f in existing.get("findings") or []}
    merged["findings"] = (existing.get("findings") or []) + [
        f for f in new.get("findings") or [] if f.get("subject") not in known
    ]
    merged["findings"] = merged["findings"][: report.MAX_FINDINGS]
    merged["updated_at"] = new.get("first_seen")
    return merged


def _extend(cur, incident_id: Any, alert_metadata: Any, payload: dict[str, Any]) -> None:
    """Add a group's findings to an open incident's Aurora Watch payload."""
    metadata = alert_metadata if isinstance(alert_metadata, dict) else json.loads(alert_metadata or "{}")
    metadata["aurora_watch"] = _merge_payload(metadata.get("aurora_watch"), payload)
    cur.execute(
        "UPDATE incidents SET alert_metadata = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
        (json.dumps(metadata), incident_id),
    )


def open_incident(user_id: str, org_id: str, cluster: dict[str, Any], group: dict[str, Any]) -> dict[str, Any]:
    """Create the incident (or add findings to the open one) and dispatch the RCA for a new one."""
    from utils.auth.stateless_auth import set_rls_context
    from utils.db.connection_pool import db_pool

    title = report.incident_title(group)
    service = report.incident_service(group)
    severity = group["severity"] if group["severity"] in ("critical", "high", "medium", "low") else "high"
    payload = report.rca_payload(group, cluster)
    alert_id = source_alert_id(cluster.get("cluster_id") or "", group["key"])
    now = datetime.now(timezone.utc).replace(tzinfo=None)  # incidents timestamps are naive UTC

    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        set_rls_context(cur, conn, user_id, log_prefix="[AuroraWatch:incident]")
        # Any user's incident for this key counts: whoever Watch acts as may change.
        cur.execute(
            """
            SELECT id, alert_metadata, status FROM incidents
             WHERE org_id = %s AND source_type = %s AND source_alert_id = %s
             ORDER BY started_at DESC LIMIT 1
            """,
            (org_id, report.SOURCE_TYPE, alert_id),
        )
        existing = cur.fetchone()
        if existing and existing[2] not in ("resolved", "merged"):
            _extend(cur, existing[0], existing[1], payload)
            conn.commit()
            logger.info("[AuroraWatch] added findings to incident %s", existing[0])
            return {"incident_id": str(existing[0]), "status": "updated"}
        if existing:
            # The same change broke something again after its incident was resolved:
            # a new occurrence, not a reopened incident with the old title and timestamps.
            alert_id = source_alert_id(cluster.get("cluster_id") or "", f"{group['key']}|{int(time.time())}")

        metadata = {"aurora_watch": payload, "cluster": payload["cluster"], "namespace": group["namespace"]}
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
             json.dumps(metadata), now),
        )
        row = cur.fetchone()
        if row is None:  # a concurrent poll inserted it first
            cur.execute(
                "SELECT id, alert_metadata FROM incidents WHERE org_id = %s AND source_type = %s "
                "AND source_alert_id = %s AND user_id = %s",
                (org_id, report.SOURCE_TYPE, alert_id, user_id),
            )
            raced = cur.fetchone()
            _extend(cur, raced[0], raced[1], payload)
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
              json.dumps(metadata))),
            ("UPDATE incidents SET affected_services = %s WHERE id = %s",
             (sorted({f["name"] for f in group["findings"]}), incident_id)),
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
