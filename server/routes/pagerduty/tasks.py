"""Celery tasks for PagerDuty V3 webhook processing."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from celery_config import celery_app
from chat.background.rca_prompt_builder import build_pagerduty_rca_prompt
from services.correlation.alert_correlator import AlertCorrelator
from services.correlation import handle_correlated_alert

logger = logging.getLogger(__name__)

# Delay (in seconds) to wait for runbook custom field before starting RCA
RUNBOOK_WAIT_DELAY = 5


def _extract_severity(incident: Dict[str, Any]) -> str:
    priority = incident.get("priority", {})
    if priority:
        priority_name = priority.get("name", "").lower()
        if (
            "p1" in priority_name
            or "critical" in priority_name
            or "sev1" in priority_name
        ):
            return "critical"
        elif (
            "p2" in priority_name or "high" in priority_name or "sev2" in priority_name
        ):
            return "high"
        elif (
            "p3" in priority_name
            or "medium" in priority_name
            or "sev3" in priority_name
        ):
            return "medium"
        elif "p4" in priority_name or "low" in priority_name or "sev4" in priority_name:
            return "low"

    urgency = incident.get("urgency", "").lower()
    return "high" if urgency == "high" else "medium"


def _extract_service_name(incident: Dict[str, Any]) -> str:
    service = incident.get("service", {})
    if isinstance(service, dict):
        return service.get("summary") or service.get("name") or "unknown"
    return str(service)[:255] if service else "unknown"


def _normalize_incident_status(pagerduty_status: str) -> str:
    return "resolved" if pagerduty_status.lower() == "resolved" else "investigating"


def _should_trigger_background_chat(user_id: str, event_type: str) -> bool:
    """Check if automated RCA should be triggered for this event.

    Automated RCA is ENABLED BY DEFAULT. Users must explicitly deactivate it.
    """
    # from utils.auth.stateless_auth import get_user_preference
    # return (get_user_preference(user_id, "automated_rca_enabled", default=True) and
    #         event_type == "incident.triggered")
    return event_type == "incident.triggered"


def _retrieve_incident_number(user_id: str, incident_id: str) -> Optional[int]:
    """Retrieve incident number from PagerDuty API (fallback for V3 events missing number)."""
    from utils.auth.token_management import get_token_data
    from routes.pagerduty.pagerduty_routes import PagerDutyClient, PagerDutyAPIError

    token_data = get_token_data(user_id, "pagerduty")
    if not token_data:
        raise RuntimeError("user not connected to PagerDuty")

    client_kwargs = (
        {"oauth_token": token_data.get("access_token")}
        if token_data.get("auth_type") == "oauth"
        else {"api_token": token_data.get("api_token")}
    )
    client = PagerDutyClient(**client_kwargs)

    try:
        resp = client._request("GET", f"/incidents/{incident_id}").json()
        inc = resp.get("incident", {})
        return inc.get("number")
    except PagerDutyAPIError as e:
        raise RuntimeError(str(e))


def _process_custom_field_update(
    user_id: str,
    event_data: Dict[str, Any],
    raw_payload: Dict[str, Any],
    received_at: datetime,
) -> None:
    """Process custom field update event and merge into existing incident.

    V3 custom field events have structure:
    {
        "data": {
            "incident": {"id": "Q0I0...", "type": "incident_reference", ...},
            "custom_fields": [{"id": "P6WTW3X", "name": "runbook_link", "value": "https://...", ...}],
            "type": "incident_field_values"
        }
    }
    """
    from utils.db.connection_pool import db_pool

    data = event_data.get("data", {})
    incident_ref = data.get("incident", {})
    incident_id = incident_ref.get("id")
    custom_fields = data.get("custom_fields", [])

    if not incident_id or not custom_fields:
        return

    # Extract custom fields into a dict
    custom_field_data = {}
    for field in custom_fields:
        field_name = field.get("name")
        field_value = field.get("value")
        if field_name and field_value:
            custom_field_data[field_name] = field_value

    if not custom_field_data:
        return

    # Store the custom field event in pagerduty_events table
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO pagerduty_events 
                (user_id, event_type, incident_id, incident_title, incident_status, 
                 incident_urgency, service_name, service_id, payload, received_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    user_id,
                    "incident.custom_field_values.updated",
                    incident_id,
                    None,  # No title in custom field event
                    None,  # No status
                    None,  # No urgency
                    None,  # No service
                    None,
                    json.dumps(raw_payload),
                    received_at,
                ),
            )
            conn.commit()

            # Find the incident in our DB by incident_id (from the most recent event)
            cursor.execute(
                """
                SELECT id, alert_metadata 
                FROM incidents 
                WHERE user_id = %s 
                  AND source_type = 'pagerduty'
                  AND alert_metadata->>'incidentId' = %s
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (user_id, incident_id),
            )

            result = cursor.fetchone()
            if not result:
                logger.info(
                    "[PAGERDUTY] No matching incident found for custom field update: %s",
                    incident_id,
                )
                return

            incident_db_id, existing_metadata = result
            existing_metadata = existing_metadata or {}

            # Merge custom fields into metadata
            if "customFields" not in existing_metadata:
                existing_metadata["customFields"] = {}
            existing_metadata["customFields"].update(custom_field_data)

            # Update the incident with merged metadata
            cursor.execute(
                """
                UPDATE incidents
                SET alert_metadata = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (json.dumps(existing_metadata), incident_db_id),
            )
            conn.commit()

            # Log runbook updates for debugging
            if "runbook_link" in custom_field_data:
                logger.info(
                    "[PAGERDUTY][RUNBOOK] Runbook URL updated for incident %s, delayed RCA task will pick it up if not yet executed",
                    incident_id,
                )

            # Broadcast update to frontend
            try:
                from routes.incidents_sse import (
                    broadcast_incident_update_to_user_connections,
                )

                broadcast_incident_update_to_user_connections(
                    user_id,
                    {
                        "type": "incident_update",
                        "incident_id": str(incident_db_id),
                        "source": "pagerduty",
                        "custom_fields_updated": True,
                    },
                )
            except Exception as e:
                logger.warning(
                    "[PAGERDUTY] Failed to broadcast custom field update: %s", e
                )


@celery_app.task(
    bind=True,
    max_retries=2,
    default_retry_delay=10,
    name="pagerduty.trigger_delayed_rca",
)
def trigger_delayed_rca(
    self,
    incident_db_id: str,
    user_id: str,
    incident_id: str,
    incident_title: str,
    incident_number: int,
    incident_urgency: str,
) -> None:
    """
    Delayed RCA trigger task.

    This task is scheduled with a delay after incident.triggered to allow time
    for custom field updates (like runbook) to arrive. After the delay, it checks
    if a runbook was added and starts RCA with or without it.
    """
    from utils.db.connection_pool import db_pool
    from routes.pagerduty.runbook_utils import (
        extract_runbook_url,
        fetch_runbook_content,
    )
    from chat.background.task import (
        run_background_chat,
        create_background_chat_session,
        is_background_chat_allowed,
    )

    logger.info(
        "[PAGERDUTY][RCA-DELAYED] Starting delayed RCA check for incident %s (db_id=%s)",
        incident_id,
        incident_db_id,
    )

    try:
        # Check if RCA was already triggered (shouldn't happen, but safety check)
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM chat_sessions
                    WHERE user_id = %s 
                      AND ui_state->'triggerMetadata'->>'incident_id' = %s
                      AND ui_state->'triggerMetadata'->>'source' = 'pagerduty'
                    """,
                    (user_id, incident_id),
                )
                existing_session_count = cursor.fetchone()[0]

                if existing_session_count > 0:
                    logger.info(
                        "[PAGERDUTY][RCA-DELAYED] RCA already exists for incident %s, skipping",
                        incident_id,
                    )
                    return

                # Check if user preferences and limits allow background chat
                if not is_background_chat_allowed(user_id):
                    logger.info(
                        "[PAGERDUTY][RCA-DELAYED] Background chat not allowed for user %s",
                        user_id,
                    )
                    return

                # Fetch latest incident metadata to check for runbook
                cursor.execute(
                    """
                    SELECT alert_metadata FROM incidents WHERE id = %s
                    """,
                    (incident_db_id,),
                )
                result = cursor.fetchone()
                if not result:
                    logger.warning(
                        "[PAGERDUTY][RCA-DELAYED] Incident %s not found", incident_db_id
                    )
                    return

                metadata = result[0] or {}

                # Check for runbook in custom fields
                custom_fields = metadata.get("customFields", {})
                runbook_url = custom_fields.get("runbook_link")

                # Fetch and consolidate ALL events for full incident data
                from routes.pagerduty.runbook_utils import (
                    fetch_and_consolidate_pagerduty_events,
                )

                consolidated_payload = fetch_and_consolidate_pagerduty_events(
                    user_id, incident_id, cursor
                )

                # Build RCA prompt from consolidated data
                if consolidated_payload:
                    try:
                        if isinstance(consolidated_payload, str):
                            consolidated_payload = json.loads(consolidated_payload)

                        event_data = consolidated_payload.get("event", {})
                        incident_data = event_data.get("data", {})
                        rca_prompt = build_pagerduty_rca_prompt(
                            incident_data, user_id=user_id
                        )
                    except (json.JSONDecodeError, KeyError, TypeError) as e:
                        rca_prompt = (
                            f"PagerDuty incident #{incident_number}: {incident_title}"
                        )
                        logger.warning(
                            "[PAGERDUTY][RCA-DELAYED] Failed to parse consolidated payload: %s",
                            e,
                        )
                else:
                    rca_prompt = (
                        f"PagerDuty incident #{incident_number}: {incident_title}"
                    )

                # Try to fetch and attach runbook if available
                if runbook_url:
                    runbook_content = fetch_runbook_content(runbook_url)
                    if runbook_content:
                        rca_prompt = f"=== RUNBOOK ===\n{runbook_content}\n\n=== INCIDENT DETAILS ===\n{rca_prompt}"
                        logger.info(
                            "[PAGERDUTY][RCA-DELAYED] Runbook attached to RCA for incident %s",
                            incident_id,
                        )
                    else:
                        logger.info(
                            "[PAGERDUTY][RCA-DELAYED] Runbook URL provided but content unavailable, proceeding anyway"
                        )
                else:
                    logger.info(
                        "[PAGERDUTY][RCA-DELAYED] No runbook found after delay, proceeding with RCA without runbook"
                    )

                # Create and trigger RCA session
                session_id = create_background_chat_session(
                    user_id=user_id,
                    title=f"RCA: {incident_title}",
                    trigger_metadata={
                        "source": "pagerduty",
                        "incident_id": incident_id,
                        "incident_number": incident_number,
                        "urgency": incident_urgency,
                    },
                )
                run_background_chat.delay(
                    user_id=user_id,
                    session_id=session_id,
                    initial_message=rca_prompt,
                    trigger_metadata={
                        "source": "pagerduty",
                        "incident_id": incident_id,
                        "incident_number": incident_number,
                    },
                    incident_id=incident_db_id,
                )
                logger.info(
                    "[PAGERDUTY][RCA-DELAYED] Successfully triggered RCA for incident %s",
                    incident_id,
                )

    except Exception as exc:
        logger.exception(
            "[PAGERDUTY][RCA-DELAYED] Failed to trigger delayed RCA for incident %s",
            incident_id,
        )
        raise self.retry(exc=exc)


@celery_app.task(
    bind=True, max_retries=3, default_retry_delay=30, name="pagerduty.process_event"
)
def process_pagerduty_event(
    self,
    raw_payload: Dict[str, Any],
    event_data: Dict[str, Any],
    metadata: Optional[Dict[str, Any]] = None,
    user_id: Optional[str] = None,
) -> None:
    """Background processor for PagerDuty V3 webhook events.

    Args:
        raw_payload: Complete V3 webhook payload (stored as-is for display)
        event_data: V3 event object from payload["event"]
        metadata: Request metadata (headers, IP, etc.)
        user_id: Aurora user ID
    """
    received_at = datetime.now(timezone.utc)

    try:
        if not user_id:
            return

        from utils.db.connection_pool import db_pool

        # Extract V3 event type and incident data
        event_type = event_data.get("event_type", "")

        # Handle custom field updates separately
        if event_type == "incident.custom_field_values.updated":
            _process_custom_field_update(user_id, event_data, raw_payload, received_at)
            return

        # In V3, event_data["data"] IS the incident object
        incident = event_data.get("data", {})

        incident_id = incident.get("id")
        incident_number = incident.get("number")

        if not incident_number and incident_id:
            try:
                incident_number = _retrieve_incident_number(user_id, incident_id)
            except Exception as e:
                logger.error(
                    "Failed to retrieve incident number for PagerDuty incident",
                    extra={
                        "user_id": user_id,
                        "incident_id": incident_id,
                        "error": str(e),
                        "error_type": type(e).__name__,
                    },
                    exc_info=True,
                )

        if not incident_number:
            return

        # Extract V3 incident fields
        incident_title = incident.get("title", "Untitled Incident")
        incident_status = incident.get("status", "unknown")
        incident_urgency = incident.get("urgency", "unknown")
        service = incident.get("service", {})
        service_name = _extract_service_name(incident)
        service_id = service.get("id") if isinstance(service, dict) else None
        
        # Store the complete V3 webhook payload
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO pagerduty_events 
                    (user_id, event_type, incident_id, incident_title, incident_status, 
                     incident_urgency, service_name, service_id, payload, received_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        user_id,
                        event_type,
                        incident_id,
                        incident_title,
                        incident_status,
                        incident_urgency,
                        service_name,
                        service_id,
                        json.dumps(raw_payload),  # Store complete original webhook
                        received_at,
                    ),
                )
                event_result = cursor.fetchone()
                event_db_id = event_result[0] if event_result else None
                conn.commit()

                if not event_db_id:
                    return

                severity = _extract_severity(incident)
                aurora_status = _normalize_incident_status(incident_status)

                # Check if incident already exists to preserve custom fields
                cursor.execute(
                    """
                    SELECT alert_metadata FROM incidents
                    WHERE user_id = %s AND source_type = 'pagerduty' AND source_alert_id = %s
                    """,
                    (user_id, incident_number),
                )
                existing_result = cursor.fetchone()
                existing_metadata = {}
                if existing_result and existing_result[0]:
                    try:
                        existing_metadata = (
                            existing_result[0]
                            if isinstance(existing_result[0], dict)
                            else json.loads(existing_result[0])
                        )
                    except (json.JSONDecodeError, TypeError):
                        existing_metadata = {}

                # Build new metadata, preserving existing custom fields
                alert_metadata = {
                    "incidentId": incident_id,  # Store incident ID for matching custom field updates
                    "incidentUrl": incident.get("html_url"),
                    "urgency": incident_urgency,
                }
                if incident_key := incident.get("incident_key"):
                    alert_metadata["incidentKey"] = incident_key
                if priority := incident.get("priority"):
                    alert_metadata["priority"] = (
                        priority.get("summary")
                        if isinstance(priority, dict)
                        else str(priority)
                    )
                if body := incident.get("body", {}).get("details"):
                    alert_metadata["description"] = body
                
                # Preserve existing custom fields (e.g., runbook_link from custom field updates)
                if "customFields" in existing_metadata:
                    alert_metadata["customFields"] = existing_metadata["customFields"]

                if event_type == "incident.triggered":
                    try:
                        correlator = AlertCorrelator()
                        correlation_result = correlator.correlate(
                            cursor=cursor,
                            user_id=user_id,
                            source_type="pagerduty",
                            source_alert_id=event_db_id,
                            alert_title=incident_title,
                            alert_service=service_name,
                            alert_severity=severity,
                            alert_metadata=alert_metadata,
                        )

                        if correlation_result.is_correlated:
                            handle_correlated_alert(
                                cursor=cursor,
                                user_id=user_id,
                                incident_id=correlation_result.incident_id,
                                source_type="pagerduty",
                                source_alert_id=event_db_id,
                                alert_title=incident_title,
                                alert_service=service_name,
                                alert_severity=severity,
                                correlation_result=correlation_result,
                                alert_metadata=alert_metadata,
                                raw_payload=raw_payload,
                            )
                            conn.commit()
                            return
                    except Exception as corr_exc:
                        logger.warning(
                            "[PAGERDUTY] Correlation check failed, proceeding with normal flow: %s",
                            corr_exc,
                        )

                # Insert or update incident
                cursor.execute(
                    """
                    INSERT INTO incidents 
                    (user_id, source_type, source_alert_id, alert_title, alert_service, 
                     severity, status, started_at, alert_metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (source_type, source_alert_id, user_id) DO UPDATE
                    SET updated_at = CURRENT_TIMESTAMP,
                        status = EXCLUDED.status,
                        severity = EXCLUDED.severity,
                        started_at = CASE 
                            WHEN incidents.status = 'resolved' AND EXCLUDED.status != 'resolved' 
                            THEN EXCLUDED.started_at
                            ELSE incidents.started_at
                        END,
                        alert_metadata = EXCLUDED.alert_metadata
                    RETURNING id
                    """,
                    (
                        user_id,
                        "pagerduty",
                        incident_number,
                        incident_title,
                        service_name,
                        severity,
                        aurora_status,
                        received_at,
                        json.dumps(alert_metadata),
                    ),
                )
                incident_row = cursor.fetchone()
                incident_db_id = incident_row[0] if incident_row else None
                conn.commit()

                if event_type == "incident.triggered":
                    try:
                        cursor.execute(
                            """INSERT INTO incident_alerts
                               (user_id, incident_id, source_type, source_alert_id, alert_title, alert_service,
                                alert_severity, correlation_strategy, correlation_score, alert_metadata)
                               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                            (
                                user_id,
                                incident_db_id,
                                "pagerduty",
                                event_db_id,
                                incident_title,
                                service_name,
                                severity,
                                "primary",
                                1.0,
                                json.dumps(alert_metadata),
                            ),
                        )
                        cursor.execute(
                            "UPDATE incidents SET affected_services = ARRAY[%s] WHERE id = %s AND affected_services = '{}'",
                            (service_name, incident_db_id),
                        )
                        conn.commit()
                    except Exception as e:
                        logger.warning(
                            "[PAGERDUTY] Failed to record primary alert: %s", e
                        )

                if incident_db_id:
                    if event_type == "incident.triggered":
                        logger.info(
                            "[PAGERDUTY][WEBHOOK] Created/updated incident %s for triggered event %s",
                            incident_db_id,
                            event_db_id,
                        )
                    else:
                        logger.info(
                            "[PAGERDUTY][WEBHOOK] Updated incident %s for event %s (type=%s)",
                            incident_db_id,
                            event_db_id,
                            event_type,
                        )

                    # Notify SSE connections about incident update
                    try:
                        from routes.incidents_sse import (
                            broadcast_incident_update_to_user_connections,
                        )

                        broadcast_incident_update_to_user_connections(
                            user_id,
                            {
                                "type": "incident_update",
                                "incident_id": str(incident_db_id),
                                "source": "pagerduty",
                            },
                        )
                    except Exception as e:
                        logger.warning(
                            f"[PAGERDUTY][WEBHOOK] Failed to notify SSE: {e}"
                        )

                    # Only trigger summary generation and RCA for new incidents (incident.triggered)
                    # For acknowledged/resolved events, we just update the status without regenerating summaries
                    if event_type == "incident.triggered":
                        # Trigger summary generation only for new incidents
                        from chat.background.summarization import (
                            generate_incident_summary,
                        )

                        generate_incident_summary.delay(
                            incident_id=str(incident_db_id),
                            user_id=user_id,
                            source_type="pagerduty",
                            alert_title=incident_title or "Unknown Incident",
                            severity=severity,
                            service=service_name,
                            raw_payload=raw_payload,
                            alert_metadata=alert_metadata,
                        )
                        
                        # Schedule delayed RCA trigger to wait for potential runbook custom field update
                        if _should_trigger_background_chat(user_id, event_type):
                            logger.info(
                                "[PAGERDUTY][RCA-DELAYED] Scheduling RCA for incident %s with %d second delay to wait for runbook",
                                incident_id,
                                RUNBOOK_WAIT_DELAY,
                            )
                            trigger_delayed_rca.apply_async(
                                kwargs={
                                    "incident_db_id": str(incident_db_id),
                                    "user_id": user_id,
                                    "incident_id": incident_id,
                                    "incident_title": incident_title,
                                    "incident_number": incident_number,
                                    "incident_urgency": incident_urgency,
                                },
                                countdown=RUNBOOK_WAIT_DELAY,
                            )
                    else:
                        logger.debug(
                            "[PAGERDUTY][WEBHOOK] Skipping summary generation and RCA for event type %s (only triggered events create new incidents)",
                            event_type,
                        )

    except Exception as exc:
        raise self.retry(exc=exc)
