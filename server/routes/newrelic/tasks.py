"""Celery tasks for New Relic integration.

Handles:
- Processing incoming webhook payloads (issue notifications)
- Polling NerdGraph for active issues
- Creating incidents and triggering RCA
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from celery_config import celery_app
from chat.background.rca_prompt_builder import build_rca_prompt, inject_aurora_learn_context
from services.correlation.alert_correlator import AlertCorrelator
from services.correlation import handle_correlated_alert

logger = logging.getLogger(__name__)


def _extract_severity_from_priority(priority: str) -> str:
    mapping = {
        "CRITICAL": "critical",
        "HIGH": "high",
        "MEDIUM": "medium",
        "LOW": "low",
    }
    return mapping.get(priority.upper(), "unknown") if priority else "unknown"


def _extract_service(payload: Dict[str, Any]) -> str:
    entity_names = payload.get("entityNames") or payload.get("entity_names") or []
    if isinstance(entity_names, list) and entity_names:
        return str(entity_names[0])[:255]
    condition = payload.get("conditionName") or payload.get("condition_name") or ""
    if condition:
        return str(condition)[:255]
    return "unknown"


def _extract_title(payload: Dict[str, Any]) -> str:
    return (
        payload.get("title")
        or payload.get("issueTitle")
        or payload.get("issue_title")
        or payload.get("conditionName")
        or payload.get("condition_name")
        or "New Relic Issue"
    )


def _extract_issue_id(payload: Dict[str, Any]) -> str:
    return str(
        payload.get("issueId")
        or payload.get("issue_id")
        or payload.get("incidentId")
        or payload.get("incident_id")
        or ""
    )


def _safe_json_dump(data: Dict[str, Any]) -> str:
    try:
        return json.dumps(data, ensure_ascii=False)
    except Exception:
        return str(data)


def _build_alert_metadata(payload: Dict[str, Any]) -> Dict[str, Any]:
    meta: Dict[str, Any] = {}

    issue_id = _extract_issue_id(payload)
    if issue_id:
        meta["issueId"] = issue_id

    for key in ("conditionName", "condition_name"):
        if payload.get(key):
            meta["conditionName"] = payload[key]
            break

    for key in ("policyName", "policy_name"):
        if payload.get(key):
            meta["policyName"] = payload[key]
            break

    entity_names = payload.get("entityNames") or payload.get("entity_names")
    if entity_names:
        meta["entityNames"] = entity_names

    entity_guids = payload.get("entityGuids") or payload.get("entity_guids")
    if entity_guids:
        meta["entityGuids"] = entity_guids

    for key in ("priority", "state", "sources", "totalIncidents",
                "activatedAt", "closedAt", "isCorrelated", "mutingState",
                "acknowledgedBy", "description"):
        val = payload.get(key)
        if val is not None:
            meta[key] = val

    issue_url = payload.get("issueUrl") or payload.get("issue_url")
    if issue_url:
        meta["issueUrl"] = issue_url

    return meta


def build_newrelic_rca_prompt(
    payload: Dict[str, Any],
    providers: Optional[List[str]] = None,
    user_id: Optional[str] = None,
) -> str:
    """Build RCA prompt from a New Relic issue payload."""
    title = _extract_title(payload)
    priority = payload.get("priority") or "UNKNOWN"
    state = payload.get("state") or "ACTIVATED"
    condition = payload.get("conditionName") or payload.get("condition_name") or ""
    policy = payload.get("policyName") or payload.get("policy_name") or ""
    entity_names = payload.get("entityNames") or payload.get("entity_names") or []
    sources = payload.get("sources") or []
    description = payload.get("description") or ""

    alert_details = {
        "title": title,
        "status": f"{state} (priority={priority})",
        "message": description,
        "labels": {
            "condition": condition,
            "policy": policy,
            "sources": ", ".join(sources) if isinstance(sources, list) else str(sources),
        },
    }

    if entity_names:
        alert_details["entities"] = ", ".join(entity_names) if isinstance(entity_names, list) else str(entity_names)

    return build_rca_prompt("newrelic", alert_details, providers, user_id)


@celery_app.task(
    bind=True, max_retries=3, default_retry_delay=30, name="newrelic.process_issue"
)
def process_newrelic_issue(
    self,
    payload: Dict[str, Any],
    metadata: Optional[Dict[str, Any]] = None,
    user_id: Optional[str] = None,
) -> None:
    """Process a New Relic issue from webhook or polling."""
    title = _extract_title(payload)
    issue_id = _extract_issue_id(payload)
    logger.info("[NEWRELIC][WEBHOOK][USER:%s] %s (issue=%s)", user_id or "unknown", title, issue_id)
    logger.debug("[NEWRELIC][WEBHOOK] payload=%s", _safe_json_dump(payload))

    try:
        if not user_id:
            logger.warning("[NEWRELIC][WEBHOOK] Missing user_id; skipping persistence")
            return

        from utils.db.connection_pool import db_pool

        priority = payload.get("priority") or "UNKNOWN"
        state = payload.get("state") or "ACTIVATED"
        entity_names = payload.get("entityNames") or payload.get("entity_names") or []
        entity_names_str = ", ".join(entity_names) if isinstance(entity_names, list) else str(entity_names)

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                from utils.auth.stateless_auth import set_rls_context
                org_id = set_rls_context(cursor, conn, user_id, log_prefix="[NEWRELIC][WEBHOOK]")
                if not org_id:
                    return

                received_at = datetime.now(timezone.utc)

                cursor.execute(
                    """
                    INSERT INTO newrelic_events
                        (user_id, org_id, issue_id, issue_title, priority, state, entity_names, payload, received_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (org_id, issue_id) DO UPDATE
                        SET issue_title = EXCLUDED.issue_title,
                            priority = EXCLUDED.priority,
                            state = EXCLUDED.state,
                            entity_names = EXCLUDED.entity_names,
                            payload = EXCLUDED.payload,
                            received_at = EXCLUDED.received_at
                    RETURNING id
                    """,
                    (user_id, org_id, issue_id, title, priority, state,
                     entity_names_str, json.dumps(payload), received_at),
                )
                event_result = cursor.fetchone()
                event_id = event_result[0] if event_result else None
                conn.commit()

                if not event_id:
                    logger.error("[NEWRELIC][WEBHOOK] Failed to get event_id for user %s", user_id)
                    return

                logger.info("[NEWRELIC][WEBHOOK] Stored event %s for user %s", event_id, user_id)

                severity = _extract_severity_from_priority(priority)
                service = _extract_service(payload)
                alert_metadata = _build_alert_metadata(payload)

                try:
                    correlator = AlertCorrelator()
                    correlation_result = correlator.correlate(
                        cursor=cursor,
                        user_id=user_id,
                        source_type="newrelic",
                        source_alert_id=event_id,
                        alert_title=title,
                        alert_service=service,
                        alert_severity=severity,
                        alert_metadata=alert_metadata,
                        org_id=org_id,
                    )

                    if correlation_result.is_correlated:
                        handle_correlated_alert(
                            cursor=cursor,
                            user_id=user_id,
                            incident_id=correlation_result.incident_id,
                            source_type="newrelic",
                            source_alert_id=event_id,
                            alert_title=title,
                            alert_service=service,
                            alert_severity=severity,
                            correlation_result=correlation_result,
                            alert_metadata=alert_metadata,
                            raw_payload=payload,
                            org_id=org_id,
                        )
                        conn.commit()
                        return
                except Exception as corr_exc:
                    logger.warning(
                        "[NEWRELIC] Correlation check failed, proceeding with normal flow: %s",
                        corr_exc,
                    )

                cursor.execute(
                    """
                    INSERT INTO incidents
                    (user_id, org_id, source_type, source_alert_id, alert_title, alert_service,
                     severity, status, started_at, alert_metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (org_id, source_type, source_alert_id, user_id) DO UPDATE
                    SET updated_at = CURRENT_TIMESTAMP,
                        started_at = CASE
                            WHEN incidents.status != 'analyzed' THEN EXCLUDED.started_at
                            ELSE incidents.started_at
                        END,
                        alert_metadata = EXCLUDED.alert_metadata
                    RETURNING id
                    """,
                    (user_id, org_id, "newrelic", event_id, title, service,
                     severity, "investigating", received_at, json.dumps(alert_metadata)),
                )
                incident_row = cursor.fetchone()
                incident_id = incident_row[0] if incident_row else None
                conn.commit()

                try:
                    cursor.execute(
                        """INSERT INTO incident_alerts
                           (user_id, org_id, incident_id, source_type, source_alert_id, alert_title, alert_service,
                            alert_severity, correlation_strategy, correlation_score, alert_metadata)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (user_id, org_id, incident_id, "newrelic", event_id,
                         title, service, severity, "primary", 1.0,
                         json.dumps(alert_metadata)),
                    )
                    cursor.execute(
                        "UPDATE incidents SET affected_services = ARRAY[%s] WHERE id = %s",
                        (service, incident_id),
                    )
                    conn.commit()
                except Exception as e:
                    logger.warning("[NEWRELIC] Failed to record primary alert: %s", e)

                if incident_id:
                    logger.info(
                        "[NEWRELIC][WEBHOOK] Created incident %s for event %s",
                        incident_id, event_id,
                    )

                    try:
                        from routes.incidents_sse import broadcast_incident_update_to_user_connections
                        broadcast_incident_update_to_user_connections(
                            user_id,
                            {"type": "incident_update", "incident_id": str(incident_id), "source": "newrelic"},
                        )
                    except Exception as e:
                        logger.warning("[NEWRELIC][WEBHOOK] Failed to notify SSE: %s", e)

                    from chat.background.summarization import generate_incident_summary
                    generate_incident_summary.delay(
                        incident_id=str(incident_id),
                        user_id=user_id,
                        source_type="newrelic",
                        alert_title=title or "Unknown Issue",
                        severity=severity,
                        service=service,
                        raw_payload=payload,
                        alert_metadata=alert_metadata,
                    )

                    try:
                        from chat.background.task import (
                            run_background_chat,
                            create_background_chat_session,
                            is_background_chat_allowed,
                        )

                        if not is_background_chat_allowed(user_id):
                            logger.info("[NEWRELIC][WEBHOOK] Skipping background RCA - rate limited for user %s", user_id)
                        else:
                            session_id = create_background_chat_session(
                                user_id=user_id,
                                title=f"RCA: {title or 'New Relic Alert'}",
                                trigger_metadata={
                                    "source": "newrelic",
                                    "issue_id": issue_id,
                                    "priority": priority,
                                    "state": state,
                                },
                                incident_id=str(incident_id),
                            )

                            rca_prompt = build_newrelic_rca_prompt(payload, user_id=user_id)

                            task = run_background_chat.delay(
                                user_id=user_id,
                                session_id=session_id,
                                initial_message=rca_prompt,
                                trigger_metadata={
                                    "source": "newrelic",
                                    "issue_id": issue_id,
                                    "priority": priority,
                                    "state": state,
                                },
                                incident_id=str(incident_id),
                            )

                            cursor.execute(
                                "UPDATE incidents SET rca_celery_task_id = %s WHERE id = %s",
                                (task.id, str(incident_id)),
                            )
                            conn.commit()

                            logger.info(
                                "[NEWRELIC][WEBHOOK] Triggered background RCA for session %s (task_id=%s)",
                                session_id, task.id,
                            )
                    except Exception as e:
                        logger.error("[NEWRELIC][WEBHOOK] Failed to trigger RCA: %s", e)

    except Exception as exc:
        logger.exception("[NEWRELIC][WEBHOOK] Failed to process webhook payload")
        raise self.retry(exc=exc)


@celery_app.task(
    bind=True, max_retries=2, default_retry_delay=60, name="newrelic.poll_issues"
)
def poll_newrelic_issues(self, user_id: str) -> None:
    """Poll NerdGraph for active issues and process new ones."""
    logger.info("[NEWRELIC][POLL] Starting issue poll for user %s", user_id)

    try:
        from utils.auth.token_management import get_token_data
        creds = get_token_data(user_id, "newrelic")
        if not creds:
            logger.warning("[NEWRELIC][POLL] No credentials for user %s", user_id)
            return

        api_key = creds.get("api_key")
        account_id = creds.get("account_id")
        region = creds.get("region", "us")
        if not api_key or not account_id:
            logger.warning("[NEWRELIC][POLL] Incomplete credentials for user %s", user_id)
            return

        from connectors.newrelic_connector.client import NewRelicClient
        client = NewRelicClient(api_key=api_key, account_id=str(account_id), region=region)

        since_ms = int((datetime.now(timezone.utc) - timedelta(hours=24)).timestamp() * 1000)

        result = client.get_issues(
            states=["ACTIVATED", "CREATED"],
            since_epoch_ms=since_ms,
            page_size=50,
        )

        issues = result.get("issues", [])
        logger.info("[NEWRELIC][POLL] Found %d active issues for user %s", len(issues), user_id)

        for issue in issues:
            process_newrelic_issue.delay(issue, {"source": "poll"}, user_id)

    except Exception as exc:
        logger.exception("[NEWRELIC][POLL] Failed to poll issues for user %s", user_id)
        raise self.retry(exc=exc)
